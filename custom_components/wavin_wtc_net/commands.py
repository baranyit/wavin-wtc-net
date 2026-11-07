"""Command layer: permissions, validation, latest-value-wins queue, expiry, read-back verification.

Flow (docs/04-architecture.md, AGENTS.md section 4):

1. ``validate_wheel_target`` validates a requested comfort target (heating or cooling) against
   the permissions and the latest snapshot and maps it to exactly one raw wheel code. No I/O.
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
from .protocol import (
    WheelCalibration,
    zone_lock_command_coil,
    zone_power_control_coil,
    zone_preset_control_coil,
    zone_wheel_code_register,
)
from .transport import Clock
from .write_transport import WritableGatewayTransport

DEFAULT_VERIFY_CYCLES = 2
#: The lock status lags the command by an unknown delay, so it gets more verification cycles.
LOCK_VERIFY_CYCLES = 4
MODE_VERIFY_CYCLES = 3
HISTORY_SIZE = 20


class CommandKind(enum.Enum):
    WHEEL_TARGET = "wheel_target"
    LOCK = "lock"
    ZONE_POWER = "zone_power"
    ZONE_PRESET = "zone_preset"


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
    zones: frozenset[tuple[int, int]] = frozenset()  # wheel target writes
    lock_zones: frozenset[tuple[int, int]] = frozenset()  # lock writes, a separate opt-in
    mode_zones: frozenset[tuple[int, int]] = frozenset()  # zone on/off and preset, separate opt-in

    def allows(self, unit_id: int, zone: int) -> bool:
        return self.write_enabled and (unit_id, zone) in self.zones

    def allows_lock(self, unit_id: int, zone: int) -> bool:
        return self.write_enabled and (unit_id, zone) in self.lock_zones

    def allows_mode(self, unit_id: int, zone: int) -> bool:
        return self.write_enabled and (unit_id, zone) in self.mode_zones

    def allows_kind(self, kind: CommandKind, unit_id: int, zone: int) -> bool:
        if kind is CommandKind.LOCK:
            return self.allows_lock(unit_id, zone)
        if kind in (CommandKind.ZONE_POWER, CommandKind.ZONE_PRESET):
            return self.allows_mode(unit_id, zone)
        return self.allows(unit_id, zone)


@dataclass(frozen=True, slots=True)
class CommandKey:
    unit_id: int
    zone: int
    kind: CommandKind


@dataclass(frozen=True, slots=True)
class Command:
    """One explicit user command: a wheel code (0..12) or a lock state (0/1)."""

    command_id: int
    kind: CommandKind
    unit_id: int
    zone: int
    target: float | bool  # the user's request: a temperature, or True = locked
    raw_value: int  # the one raw value that is written
    generation: int  # transport connection generation when it was created
    created_at: float
    expires_at: float

    @property
    def key(self) -> CommandKey:
        return CommandKey(self.unit_id, self.zone, self.kind)

    @property
    def address(self) -> int:
        if self.kind is CommandKind.LOCK:
            return zone_lock_command_coil(self.zone)
        if self.kind is CommandKind.ZONE_POWER:
            return zone_power_control_coil(self.zone)
        if self.kind is CommandKind.ZONE_PRESET:
            return zone_preset_control_coil(self.zone)
        return zone_wheel_code_register(self.zone)

    @property
    def raw_code(self) -> int:
        return self.raw_value


WheelCommand = Command  # kept for readability where only wheel commands are meant


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command: Command
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
    cooling = _check_zone_state(state)
    try:
        return calibration.comfort_code(target, cooling=cooling)
    except ValueError as err:
        raise CommandRejectedError(RejectReason.INVALID_TARGET, str(err)) from err


def validate_lock_target(
    *,
    unit_id: int,
    zone: int,
    snapshot: GatewaySnapshot | None,
    permissions: CommandPermissions,
) -> None:
    """Raise ``CommandRejectedError`` unless a lock command for this zone is allowed now."""
    if not permissions.write_enabled:
        raise CommandRejectedError(RejectReason.WRITE_DISABLED, "write mode is off")
    if not permissions.allows_lock(unit_id, zone):
        raise CommandRejectedError(
            RejectReason.ZONE_NOT_PERMITTED, f"unit {unit_id} zone {zone} is not enabled for lock"
        )
    unit = snapshot.unit(unit_id) if snapshot else None
    if unit is None:
        raise CommandRejectedError(RejectReason.NO_DATA, "no snapshot for this unit yet")
    if not unit.available:
        raise CommandRejectedError(RejectReason.UNIT_UNAVAILABLE, "controller is unavailable")
    state = unit.zone(zone)
    if state is None or state.lock_status is None:
        raise CommandRejectedError(RejectReason.NO_DATA, "lock status is not known")
    if state.live is not True:
        raise CommandRejectedError(RejectReason.ZONE_NOT_LIVE, "thermostat is not connected")


def validate_zone_mode(
    *,
    unit_id: int,
    zone: int,
    kind: CommandKind,
    snapshot: GatewaySnapshot | None,
    permissions: CommandPermissions,
) -> None:
    """Raise unless a zone on/off or preset command is allowed now (no heat/cool, no global)."""
    if kind not in (CommandKind.ZONE_POWER, CommandKind.ZONE_PRESET):
        raise CommandRejectedError(RejectReason.INVALID_TARGET, f"{kind.value} is not a zone mode")
    if not permissions.write_enabled:
        raise CommandRejectedError(RejectReason.WRITE_DISABLED, "write mode is off")
    if not permissions.allows_mode(unit_id, zone):
        raise CommandRejectedError(
            RejectReason.ZONE_NOT_PERMITTED, f"unit {unit_id} zone {zone} is not enabled for mode"
        )
    unit = snapshot.unit(unit_id) if snapshot else None
    if unit is None:
        raise CommandRejectedError(RejectReason.NO_DATA, "no snapshot for this unit yet")
    if not unit.available:
        raise CommandRejectedError(RejectReason.UNIT_UNAVAILABLE, "controller is unavailable")
    state = unit.zone(zone)
    if state is None:
        raise CommandRejectedError(RejectReason.NO_DATA, "zone not in the snapshot")
    if state.live is not True:
        raise CommandRejectedError(RejectReason.ZONE_NOT_LIVE, "thermostat is not connected")
    for status, control in (
        (state.on_off, state.control_on_off),
        (state.comfort_economy, state.control_comfort_economy),
    ):
        if status is not None and control is not None and status != control:
            raise CommandRejectedError(
                RejectReason.CONFLICTING_BITS, "zone status and control bits disagree"
            )


def _check_zone_state(state: ZoneSnapshot) -> bool:
    """Raise unless the zone is on, live, in comfort and unambiguous; return True if cooling."""
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
    if preset != COMFORT:
        raise CommandRejectedError(
            RejectReason.NOT_COMFORT, "in economy the wheel has no effect on the target"
        )
    return cooling == COOLING


def _pick(status: bool | None, control: bool | None) -> bool | None:
    return status if status is not None else control


def _observed(command: Command, zone: ZoneSnapshot | None) -> int | None:
    """The value to compare with ``command.raw_value`` in the read-back."""
    if zone is None:
        return None
    if command.kind is CommandKind.LOCK:
        return None if zone.lock_status is None else int(zone.lock_status)
    if command.kind is CommandKind.ZONE_POWER:
        value = zone.on_off if zone.on_off is not None else zone.control_on_off
        return None if value is None else int(value)
    if command.kind is CommandKind.ZONE_PRESET:
        value = (
            zone.comfort_economy
            if zone.comfort_economy is not None
            else zone.control_comfort_economy
        )
        return None if value is None else int(value)
    return zone.wheel_code


@dataclass(slots=True)
class _Awaiting:
    command: Command
    cycles_left: int


@dataclass(slots=True)
class CommandQueue:
    """Latest-value-wins queue plus the verification bookkeeping. Holds no I/O."""

    clock: Clock
    ttl: float
    verify_cycles: int = DEFAULT_VERIFY_CYCLES
    _next_id: int = 1
    _pending: dict[CommandKey, Command] = field(default_factory=dict)
    _awaiting: dict[CommandKey, _Awaiting] = field(default_factory=dict)
    _latest: dict[CommandKey, CommandRecord] = field(default_factory=dict)
    history: deque[CommandRecord] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))

    def create(
        self,
        kind: CommandKind,
        unit_id: int,
        zone: int,
        target: float | bool,
        raw_value: int,
        generation: int,
    ) -> Command:
        now = self.clock.monotonic()
        command = Command(
            command_id=self._next_id,
            kind=kind,
            unit_id=unit_id,
            zone=zone,
            target=target,
            raw_value=raw_value,
            generation=generation,
            created_at=now,
            expires_at=now + self.ttl,
        )
        self._next_id += 1
        return command

    def submit(self, command: Command) -> CommandRecord:
        key = command.key
        old = self._pending.pop(key, None)
        if old is not None:
            self._record(old, CommandStatus.SUPERSEDED, "replaced by a newer command")
        waiting = self._awaiting.pop(key, None)
        if waiting is not None:
            self._record(waiting.command, CommandStatus.SUPERSEDED, "replaced by a newer command")
        self._pending[key] = command
        return self._record(command, CommandStatus.QUEUED, None)

    def take_pending(self) -> list[Command]:
        """Freeze the pending set for this cycle (sorted for a deterministic order)."""
        commands = sorted(self._pending.values(), key=lambda c: (c.unit_id, c.zone, c.kind.value))
        self._pending.clear()
        return commands

    def mark(
        self,
        command: Command,
        status: CommandStatus,
        detail: str | None = None,
        observed_code: int | None = None,
    ) -> CommandRecord:
        if status is CommandStatus.SENT:
            cycles = {
                CommandKind.LOCK: LOCK_VERIFY_CYCLES,
                CommandKind.ZONE_POWER: MODE_VERIFY_CYCLES,
                CommandKind.ZONE_PRESET: MODE_VERIFY_CYCLES,
            }.get(command.kind, self.verify_cycles)
            self._awaiting[command.key] = _Awaiting(command, cycles)
        return self._record(command, status, detail, observed_code)

    def verify(self, snapshot: GatewaySnapshot) -> list[CommandRecord]:
        done: list[CommandRecord] = []
        for key, waiting in list(self._awaiting.items()):
            command = waiting.command
            unit = snapshot.unit(command.unit_id)
            zone = unit.zone(command.zone) if unit and unit.available else None
            observed = _observed(command, zone)
            if observed == command.raw_value:
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

    def latest(
        self, unit_id: int, zone: int, kind: CommandKind = CommandKind.WHEEL_TARGET
    ) -> CommandRecord | None:
        return self._latest.get(CommandKey(unit_id, zone, kind))

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _record(
        self,
        command: Command,
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

    async def _send(self, command: Command) -> None:
        queue = self._queue
        if self._clock.monotonic() >= command.expires_at:
            queue.mark(command, CommandStatus.TIMEOUT, RejectReason.EXPIRED.value)
            return
        stats = self._transport.stats
        if not stats.connected or self._transport.connection_generation != command.generation:
            queue.mark(command, CommandStatus.TIMEOUT, RejectReason.CONNECTION_CHANGED.value)
            return
        if not self._permissions.allows_kind(command.kind, command.unit_id, command.zone):
            queue.mark(command, CommandStatus.REJECTED, RejectReason.WRITE_DISABLED.value)
            return
        try:
            if command.kind is CommandKind.LOCK:
                await self._transport.write_lock_coil(
                    command.unit_id, command.address, bool(command.raw_value)
                )
            elif command.kind in (CommandKind.ZONE_POWER, CommandKind.ZONE_PRESET):
                await self._transport.write_zone_mode_coil(
                    command.unit_id, command.address, bool(command.raw_value)
                )
            else:
                await self._transport.write_wheel_code(
                    command.unit_id, command.address, command.raw_value
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
            "kind": r.command.kind.value,
            "unit": r.command.unit_id,
            "zone": r.command.zone,
            "target": r.command.target,
            "raw_value": r.command.raw_value,
            "status": r.status.value,
            "detail": r.detail,
            "observed_code": r.observed_code,
        }
        for r in records
    ]


__all__ = [
    "Command",
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
    "validate_lock_target",
    "validate_wheel_target",
    "validate_zone_mode",
]
