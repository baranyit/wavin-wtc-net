"""Command layer: permissions, validation, debounced dispatch, optimistic overlay, verification.

Flow (docs/04-architecture.md, AGENTS.md section 4):

1. ``validate_wheel_target`` validates a requested comfort target (heating or cooling) against
   the permissions and the latest snapshot and maps it to exactly one raw wheel code. No I/O.
2. ``CommandQueue.submit`` stores it keyed by ``(unit, zone, kind)``; a newer command supersedes
   the older one. ``CommandExecutor.notify_submitted`` (re)starts one endpoint-wide debounce: the
   queue is flushed when 2 s passed without a new command, or 10 s after the first waiting one.
3. ``CommandExecutor.flush`` writes what is queued, one register/coil per command, in a fixed
   order, through the shared one-request-per-second transport (between the requests of a running
   poll cycle if need be). There is no targeted read-back: the next cycle confirms.
4. Until then the entities show the requested value (the *optimistic overlay*, ``optimistic``).
   The overlay ends when the command is final or, for a wheel target, at the first poll cycle that
   started after the write, so a real (e.g. dial) value is never hidden for long. A cycle that was
   already running at write time is ignored for that command (its reads may predate the write).
   There is no replay: a command whose connection changed or whose expiry passed is dropped, a
   command is never written twice.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import math
from collections import deque
from collections.abc import Callable, Iterable
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
#: Quiet time after the last command before the queue is written (restarts on every command).
DEFAULT_DEBOUNCE = 2.0
#: Upper bound from the first waiting command, so a steady stream cannot starve the write.
DEFAULT_MAX_WAIT = 10.0

_LOGGER = logging.getLogger(__name__)


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
    #: Poll cycles numbered up to this one may predate the write and are ignored (None = no fence).
    sent_cycle: int | None = None
    overlay: bool = True


@dataclass(slots=True)
class CommandQueue:
    """Latest-value-wins queue plus the verification bookkeeping. Holds no I/O."""

    clock: Clock
    ttl: float
    verify_cycles: int = DEFAULT_VERIFY_CYCLES
    _next_id: int = 1
    _pending: dict[CommandKey, Command] = field(default_factory=dict)
    _sending: dict[CommandKey, Command] = field(default_factory=dict)
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
        """Freeze the pending set for one flush (sorted for a deterministic order).

        The commands stay visible to ``optimistic`` while they are being sent.
        """
        commands = sorted(self._pending.values(), key=lambda c: (c.unit_id, c.zone, c.kind.value))
        for command in commands:
            self._sending[command.key] = command
        self._pending.clear()
        return commands

    def mark(
        self,
        command: Command,
        status: CommandStatus,
        detail: str | None = None,
        observed_code: int | None = None,
        *,
        sent_cycle: int | None = None,
    ) -> CommandRecord:
        key = command.key
        sending = self._sending.get(key)
        if sending is not None and sending.command_id == command.command_id:
            del self._sending[key]
        if status is CommandStatus.SENT:
            newer = self._pending.get(key)
            if newer is not None and newer.command_id > command.command_id:
                # Written, but the user already replaced it: the newer command owns the key.
                return self._record(
                    command,
                    CommandStatus.SUPERSEDED,
                    "written, replaced by a newer command before the read-back",
                    record_latest=False,
                )
            cycles = {
                CommandKind.LOCK: LOCK_VERIFY_CYCLES,
                CommandKind.ZONE_POWER: MODE_VERIFY_CYCLES,
                CommandKind.ZONE_PRESET: MODE_VERIFY_CYCLES,
            }.get(command.kind, self.verify_cycles)
            self._awaiting[key] = _Awaiting(command, cycles, sent_cycle)
        return self._record(command, status, detail, observed_code)

    def drop_unsent(self, detail: str) -> list[CommandRecord]:
        """Final-mark every command that was waiting or being sent (unload, shutdown)."""
        commands = [*self._pending.values(), *self._sending.values()]
        self._pending.clear()
        self._sending.clear()
        return [self._record(c, CommandStatus.TIMEOUT, detail) for c in commands]

    def optimistic(
        self, unit_id: int, zone: int, kind: CommandKind = CommandKind.WHEEL_TARGET
    ) -> Command | None:
        """The command whose requested value the entity should show until a read says otherwise."""
        key = CommandKey(unit_id, zone, kind)
        if (command := self._pending.get(key)) is not None:
            return command
        if (command := self._sending.get(key)) is not None:
            return command
        waiting = self._awaiting.get(key)
        return waiting.command if waiting is not None and waiting.overlay else None

    def verify(
        self, snapshot: GatewaySnapshot, cycle_index: int | None = None
    ) -> list[CommandRecord]:
        """Compare a snapshot (poll cycle ``cycle_index``) with the written commands."""
        done: list[CommandRecord] = []
        for key, waiting in list(self._awaiting.items()):
            command = waiting.command
            if (
                cycle_index is not None
                and waiting.sent_cycle is not None
                and cycle_index <= waiting.sent_cycle
            ):
                continue  # this cycle was already running at write time: its reads may be older
            unit = snapshot.unit(command.unit_id)
            zone = unit.zone(command.zone) if unit and unit.available else None
            observed = _observed(command, zone)
            if observed == command.raw_value:
                del self._awaiting[key]
                done.append(self._record(command, CommandStatus.CONFIRMED, None, observed))
                continue
            if command.kind is CommandKind.WHEEL_TARGET:
                waiting.overlay = False  # the first post-write cycle shows the real value
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
        *,
        record_latest: bool = True,
    ) -> CommandRecord:
        record = CommandRecord(command, status, detail, self.clock.monotonic(), observed_code)
        if record_latest:
            self._latest[command.key] = record
        self.history.append(record)
        return record


class CommandExecutor:
    """Debounced dispatch of explicit user commands, and their verification by later reads.

    Nothing here runs unless a command was submitted: polling alone never writes.
    """

    def __init__(
        self,
        transport: WritableGatewayTransport,
        queue: CommandQueue,
        permissions: CommandPermissions,
        *,
        debounce: float = DEFAULT_DEBOUNCE,
        max_wait: float = DEFAULT_MAX_WAIT,
        cycle_index: Callable[[], int] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        if not (debounce > 0 and max_wait >= debounce):
            raise ValueError("need 0 < debounce <= max_wait")
        self._transport = transport
        self._queue = queue
        self._permissions = permissions
        self._clock = queue.clock
        self._debounce = debounce
        self._max_wait = max_wait
        #: Number of the poll cycle that started last (fences stale reads); None disables it.
        self.cycle_index = cycle_index
        #: Called after a flush changed command states (the coordinator refreshes listeners).
        self.on_change = on_change
        self._first_submit: float | None = None
        self._last_submit = 0.0
        self._worker: asyncio.Task[None] | None = None

    @property
    def queue(self) -> CommandQueue:
        return self._queue

    @property
    def permissions(self) -> CommandPermissions:
        return self._permissions

    @property
    def connection_generation(self) -> int:
        return self._transport.connection_generation

    def notify_submitted(self) -> None:
        """A command was queued: (re)start the quiet period. Needs a running event loop."""
        now = self._clock.monotonic()
        self._last_submit = now
        if self._first_submit is None:
            self._first_submit = now
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def flush(self) -> None:
        """Write everything queued now, one request per command, in a fixed order."""
        commands = self._queue.take_pending()
        for command in commands:
            await self._send(command)
        if commands and self.on_change is not None:
            self.on_change()

    async def close(self) -> None:
        """Stop dispatching and drop what has not been written (no replay after a reload)."""
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._first_submit = None
        self._queue.drop_unsent("dropped on unload")

    def verify(self, snapshot: GatewaySnapshot) -> None:
        index = self.cycle_index() if self.cycle_index is not None else None
        self._queue.verify(snapshot, index)

    async def _run(self) -> None:
        try:
            while True:
                while True:
                    first = self._first_submit if self._first_submit is not None else 0.0
                    deadline = min(self._last_submit + self._debounce, first + self._max_wait)
                    wait = deadline - self._clock.monotonic()
                    if wait <= 0:
                        break
                    await self._clock.sleep(wait)
                self._first_submit = None  # commands arriving during the flush start a new window
                await self.flush()
                if self._queue.pending_count == 0:
                    return
        except Exception:
            _LOGGER.exception("command dispatch failed")
            raise

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
            sent_cycle = self.cycle_index() if self.cycle_index is not None else None
            queue.mark(command, CommandStatus.SENT, sent_cycle=sent_cycle)


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
