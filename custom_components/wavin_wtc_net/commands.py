"""Command layer: permissions, validation, latest-value-wins queue, expiry, read-back verification.

Flow (docs/04-architecture.md, AGENTS.md section 4):

1. ``build_wheel_command`` validates a requested comfort target against the permissions and the
   latest snapshot and maps it to exactly one raw wheel code. It performs no I/O.
2. ``CommandQueue.submit`` stores it keyed by ``(unit, zone, kind)``; a newer command supersedes
   the older one. Nothing is sent.
3. At the start of the next scheduled cycle ``CommandExecutor.before_reads`` sends what is queued,
   one register per command, through the shared one-request-per-second transport.
4. After that cycle's reads ``CommandExecutor.verify`` compares the read-back wheel code. Entity
   state only ever comes from reads. There is no optimistic state and no replay: a command whose
   connection changed or whose expiry passed is dropped, a command never written twice.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import enum
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from .errors import WtcError, WtcModbusExceptionError, WtcRequestRejectedError, WtcTransportError
from .models import GatewaySnapshot, ZoneSnapshot
from .polarity import COMFORT, COOLING, ON_OFF_ON
from .protocol import WheelCalibration, zone_wheel_code_register
from .transport import Clock
from .write_transport import WritableGatewayTransport

DEFAULT_VERIFY_CYCLES = 2
HISTORY_SIZE = 20


class CommandKind(enum.Enum):
    WHEEL_TARGET = "wheel_target"


class CommandStatus(enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    TRANSPORT_ERROR = "transport_error"


FINAL_STATUSES = frozenset(CommandStatus) - {CommandStatus.QUEUED, CommandStatus.SENT}


class RejectReason(enum.Enum):
    WRITE_DISABLED = "write_disabled"
    ZONE_NOT_PERMITTED = "zone_not_permitted"
    NO_DATA = "no_data"
    UNIT_UNAVAILABLE = "unit_unavailable"
    ZONE_NOT_LIVE = "zone_not_live"
    ZONE_OFF = "zone_off"
    MODE_UNKNOWN = "mode_unknown"
    NOT_COMFORT = "not_comfort"
    COOLING_UNVALIDATED = "cooling_unvalidated"
    CONFLICTING_BITS = "conflicting_bits"
    INVALID_TARGET = "invalid_target"
    EXPIRED = "expired"
    CONNECTION_CHANGED = "connection_changed"
    DEVICE_REJECTED = "device_rejected"


class CommandRejectedError(WtcError):
    """A command failed local validation; nothing was queued or sent."""

    def __init__(self, reason: RejectReason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CommandPermissions:
    """Write mode is off by default. Zones are separate opt-ins ``(unit_id, zone)``."""

    write_enabled: bool = False
    zones: frozenset[tuple[int, int]] = frozenset()

    def allows(self, unit_id: int, zone: int) -> bool:
        return self.write_enabled and (unit_id, zone) in self.zones


@dataclass(frozen=True, slots=True)
class CommandKey:
    unit_id: int
    zone: int
    kind: CommandKind


@dataclass(frozen=True, slots=True)
class WheelCommand:
    command_id: int
    unit_id: int
    zone: int
    target: float
    raw_code: int
    generation: int  # transport connection generation when it was created
    created_at: float
    expires_at: float

    @property
    def key(self) -> CommandKey:
        return CommandKey(self.unit_id, self.zone, CommandKind.WHEEL_TARGET)

    @property
    def register(self) -> int:
        return zone_wheel_code_register(self.zone)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command: WheelCommand
    status: CommandStatus
    detail: str | None
    updated_at: float
    observed_code: int | None = None


def validate_wheel_target(
    *,
    unit_id: int,
    zone: int,
    target: float,
    snapshot: GatewaySnapshot | None,
    calibration: WheelCalibration,
    permissions: CommandPermissions,
) -> int:
    """Return the one raw wheel code for ``target`` or raise ``CommandRejectedError``."""
    if not permissions.write_enabled:
        raise CommandRejectedError(RejectReason.WRITE_DISABLED, "write mode is off")
    if not permissions.allows(unit_id, zone):
        raise CommandRejectedError(
            RejectReason.ZONE_NOT_PERMITTED, f"unit {unit_id} zone {zone} is not enabled for writes"
        )
    unit = snapshot.unit(unit_id) if snapshot else None
    if unit is None:
        raise CommandRejectedError(RejectReason.NO_DATA, "no snapshot for this unit yet")
    if not unit.available:
        raise CommandRejectedError(RejectReason.UNIT_UNAVAILABLE, "controller is unavailable")
    state = unit.zone(zone)
    if state is None:
        raise CommandRejectedError(RejectReason.NO_DATA, "zone not in the snapshot")
    if not math.isfinite(target):
        raise CommandRejectedError(RejectReason.INVALID_TARGET, "target is not a number")
    _check_zone_state(state)
    try:
        return calibration.comfort_code(target, cooling=False)
    except ValueError as err:
        raise CommandRejectedError(RejectReason.INVALID_TARGET, str(err)) from err


def _check_zone_state(state: ZoneSnapshot) -> None:
    if state.live is not True:
        raise CommandRejectedError(RejectReason.ZONE_NOT_LIVE, "thermostat is not connected")
    on = _pick(state.on_off, state.control_on_off)
    preset = _pick(state.comfort_economy, state.control_comfort_economy)
    cooling = _pick(state.heat_cool, state.control_heat_cool)
    # Status bits and writable control bits mirror each other (vendor doc, hardware run). A
    # disagreement means the zone is in a state we do not understand: block and report.
    for status, control in (
        (state.on_off, state.control_on_off),
        (state.comfort_economy, state.control_comfort_economy),
    ):
        if status is not None and control is not None and status != control:
            raise CommandRejectedError(
                RejectReason.CONFLICTING_BITS, "zone status and control bits disagree"
            )
    if on is None or preset is None or cooling is None:
        raise CommandRejectedError(RejectReason.MODE_UNKNOWN, "zone mode or preset is unknown")
    if on != ON_OFF_ON:
        raise CommandRejectedError(RejectReason.ZONE_OFF, "zone is switched off")
    if cooling == COOLING:
        raise CommandRejectedError(
            RejectReason.COOLING_UNVALIDATED,
            "cooling targets are not written until validated on hardware",
        )
    if preset != COMFORT:
        raise CommandRejectedError(
            RejectReason.NOT_COMFORT, "in economy the wheel has no effect on the target"
        )


def _pick(status: bool | None, control: bool | None) -> bool | None:
    return status if status is not None else control


@dataclass(slots=True)
class _Awaiting:
    command: WheelCommand
    cycles_left: int


@dataclass(slots=True)
class CommandQueue:
    """Latest-value-wins queue plus the verification bookkeeping. Holds no I/O."""

    clock: Clock
    ttl: float
    verify_cycles: int = DEFAULT_VERIFY_CYCLES
    _next_id: int = 1
    _pending: dict[CommandKey, WheelCommand] = field(default_factory=dict)
    _awaiting: dict[CommandKey, _Awaiting] = field(default_factory=dict)
    _latest: dict[CommandKey, CommandRecord] = field(default_factory=dict)
    history: deque[CommandRecord] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))

    def create(
        self, unit_id: int, zone: int, target: float, raw_code: int, generation: int
    ) -> WheelCommand:
        now = self.clock.monotonic()
        command = WheelCommand(
            command_id=self._next_id,
            unit_id=unit_id,
            zone=zone,
            target=target,
            raw_code=raw_code,
            generation=generation,
            created_at=now,
            expires_at=now + self.ttl,
        )
        self._next_id += 1
        return command

    def submit(self, command: WheelCommand) -> CommandRecord:
        key = command.key
        old = self._pending.pop(key, None)
        if old is not None:
            self._record(old, CommandStatus.SUPERSEDED, "replaced by a newer command")
        waiting = self._awaiting.pop(key, None)
        if waiting is not None:
            self._record(waiting.command, CommandStatus.SUPERSEDED, "replaced by a newer command")
        self._pending[key] = command
        return self._record(command, CommandStatus.QUEUED, None)

    def take_pending(self) -> list[WheelCommand]:
        """Freeze the pending set for this cycle (sorted for a deterministic order)."""
        commands = sorted(self._pending.values(), key=lambda c: (c.unit_id, c.zone))
        self._pending.clear()
        return commands

    def mark(
        self,
        command: WheelCommand,
        status: CommandStatus,
        detail: str | None = None,
        observed_code: int | None = None,
    ) -> CommandRecord:
        if status is CommandStatus.SENT:
            self._awaiting[command.key] = _Awaiting(command, self.verify_cycles)
        return self._record(command, status, detail, observed_code)

    def verify(self, snapshot: GatewaySnapshot) -> list[CommandRecord]:
        done: list[CommandRecord] = []
        for key, waiting in list(self._awaiting.items()):
            command = waiting.command
            unit = snapshot.unit(command.unit_id)
            zone = unit.zone(command.zone) if unit and unit.available else None
            observed = zone.wheel_code if zone else None
            if observed == command.raw_code:
                del self._awaiting[key]
                done.append(self._record(command, CommandStatus.CONFIRMED, None, observed))
                continue
            waiting.cycles_left -= 1
            if waiting.cycles_left <= 0:
                del self._awaiting[key]
                detail = (
                    "read-back did not match in time"
                    if observed is not None
                    else "no read-back available in time"
                )
                done.append(self._record(command, CommandStatus.TIMEOUT, detail, observed))
        return done

    def latest(self, unit_id: int, zone: int) -> CommandRecord | None:
        return self._latest.get(CommandKey(unit_id, zone, CommandKind.WHEEL_TARGET))

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _record(
        self,
        command: WheelCommand,
        status: CommandStatus,
        detail: str | None,
        observed_code: int | None = None,
    ) -> CommandRecord:
        record = CommandRecord(command, status, detail, self.clock.monotonic(), observed_code)
        self._latest[command.key] = record
        self.history.append(record)
        return record


class CommandExecutor:
    """Poller hooks: send the frozen queue before the reads, verify after them."""

    def __init__(
        self,
        transport: WritableGatewayTransport,
        queue: CommandQueue,
        permissions: CommandPermissions,
    ) -> None:
        self._transport = transport
        self._queue = queue
        self._permissions = permissions
        self._clock = queue.clock

    @property
    def queue(self) -> CommandQueue:
        return self._queue

    @property
    def permissions(self) -> CommandPermissions:
        return self._permissions

    @property
    def connection_generation(self) -> int:
        return self._transport.connection_generation

    async def before_reads(self) -> None:
        for command in self._queue.take_pending():
            await self._send(command)

    def verify(self, snapshot: GatewaySnapshot) -> None:
        self._queue.verify(snapshot)

    async def _send(self, command: WheelCommand) -> None:
        queue = self._queue
        if self._clock.monotonic() >= command.expires_at:
            queue.mark(command, CommandStatus.TIMEOUT, RejectReason.EXPIRED.value)
            return
        stats = self._transport.stats
        if not stats.connected or self._transport.connection_generation != command.generation:
            queue.mark(command, CommandStatus.TIMEOUT, RejectReason.CONNECTION_CHANGED.value)
            return
        if not self._permissions.allows(command.unit_id, command.zone):
            queue.mark(command, CommandStatus.REJECTED, RejectReason.WRITE_DISABLED.value)
            return
        try:
            await self._transport.write_wheel_code(
                command.unit_id, command.register, command.raw_code
            )
        except WtcRequestRejectedError as err:
            queue.mark(command, CommandStatus.REJECTED, str(err))
        except WtcModbusExceptionError as err:
            queue.mark(
                command, CommandStatus.REJECTED, f"{RejectReason.DEVICE_REJECTED.value}: {err}"
            )
        except WtcTransportError as err:
            queue.mark(command, CommandStatus.TRANSPORT_ERROR, f"{type(err).__name__}: {err}")
        else:
            queue.mark(command, CommandStatus.SENT)


def records_summary(records: Iterable[CommandRecord]) -> list[dict[str, object]]:
    """Plain, redaction-free summary for diagnostics (no addresses of the endpoint)."""
    return [
        {
            "id": r.command.command_id,
            "unit": r.command.unit_id,
            "zone": r.command.zone,
            "target": r.command.target,
            "raw_code": r.command.raw_code,
            "status": r.status.value,
            "detail": r.detail,
            "observed_code": r.observed_code,
        }
        for r in records
    ]


__all__ = [
    "CommandExecutor",
    "CommandKind",
    "CommandPermissions",
    "CommandQueue",
    "CommandRecord",
    "CommandRejectedError",
    "CommandStatus",
    "RejectReason",
    "WheelCommand",
    "records_summary",
    "validate_wheel_target",
]
