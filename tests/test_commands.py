"""Command layer: permissions, validation, coalescing, ordering, verification, safety."""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from custom_components.wavin_wtc_net import protocol as p
from custom_components.wavin_wtc_net.commands import (
    CommandExecutor,
    CommandKind,
    CommandPermissions,
    CommandQueue,
    CommandRejectedError,
    CommandStatus,
    RejectReason,
    validate_lock_target,
    validate_wheel_target,
    validate_zone_mode,
)
from custom_components.wavin_wtc_net.errors import WtcRequestRejectedError
from custom_components.wavin_wtc_net.models import GatewaySnapshot
from custom_components.wavin_wtc_net.poller import GatewayPoller
from custom_components.wavin_wtc_net.runtime import build_client
from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

from .mock_wtc import REAL_PROFILE, Fault, MockWtcBackend, VirtualClock

CAL = p.WheelCalibration()
UNITS = {uid: len(zones) for uid, zones in REAL_PROFILE.items()}
ALLOW_2_2 = CommandPermissions(True, frozenset({(2, 2)}))
ALLOW_ALL = CommandPermissions(
    True, frozenset((u, z) for u, n in UNITS.items() for z in range(1, n + 1))
)
CODE_REG = p.zone_wheel_code_register


class Rig:
    def __init__(self, permissions: CommandPermissions = ALLOW_2_2, *, ttl: float = 120.0) -> None:
        self.clock = VirtualClock()
        self.backend = MockWtcBackend.for_profile(self.clock)
        transport, self.client = build_client(
            "192.0.2.1", 502, UNITS, backend=self.backend, clock=self.clock, writable=True
        )
        assert isinstance(transport, WritableGatewayTransport)
        self.transport = transport
        self.queue = CommandQueue(self.clock, ttl)
        self.permissions = permissions
        self.executor = CommandExecutor(transport, self.queue, permissions)
        self.poller = GatewayPoller(
            self.client,
            interval=60,
            clock=self.clock,
            before_reads=self.executor.before_reads,
            on_snapshot=self.executor.verify,
        )

    async def cycle(self) -> GatewaySnapshot:
        snapshot = await self.poller.run_cycle()
        assert snapshot is not None
        return snapshot

    def submit(self, unit: int, zone: int, target: float, snapshot: GatewaySnapshot):  # type: ignore[no-untyped-def]
        code = validate_wheel_target(
            unit_id=unit,
            zone=zone,
            target=target,
            snapshot=snapshot,
            calibration=CAL,
            permissions=self.permissions,
        )
        command = self.queue.create(
            CommandKind.WHEEL_TARGET, unit, zone, target, code, self.transport.connection_generation
        )
        return self.queue.submit(command)


# -- validation (no I/O) -------------------------------------------------------------------


async def test_validation_maps_target_to_exactly_one_code() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    for target, code in ((24.0, 10), (21.5, 5), (19.0, 0), (25.0, 12)):
        assert (
            validate_wheel_target(
                unit_id=2,
                zone=2,
                target=target,
                snapshot=snapshot,
                calibration=CAL,
                permissions=ALLOW_2_2,
            )
            == code
        )


@pytest.mark.parametrize(
    ("permissions", "unit", "zone", "reason"),
    [
        (CommandPermissions(), 2, 2, RejectReason.WRITE_DISABLED),
        (CommandPermissions(True, frozenset()), 2, 2, RejectReason.ZONE_NOT_PERMITTED),
        (ALLOW_2_2, 2, 3, RejectReason.ZONE_NOT_PERMITTED),
    ],
)
async def test_permission_tiers(
    permissions: CommandPermissions, unit: int, zone: int, reason: RejectReason
) -> None:
    snapshot = await Rig().cycle()
    with pytest.raises(CommandRejectedError) as err:
        validate_wheel_target(
            unit_id=unit,
            zone=zone,
            target=22.0,
            snapshot=snapshot,
            calibration=CAL,
            permissions=permissions,
        )
    assert err.value.reason is reason


@pytest.mark.parametrize("target", [21.75, 22.2, 18.5, 25.5, float("nan")])
async def test_inexact_or_out_of_range_targets_are_rejected_never_rounded(target: float) -> None:
    snapshot = await Rig().cycle()
    with pytest.raises(CommandRejectedError) as err:
        validate_wheel_target(
            unit_id=2,
            zone=2,
            target=target,
            snapshot=snapshot,
            calibration=CAL,
            permissions=ALLOW_2_2,
        )
    assert err.value.reason is RejectReason.INVALID_TARGET


def _with_zone(
    snapshot: GatewaySnapshot, unit: int, zone: int, **changes: object
) -> GatewaySnapshot:
    units = []
    for u in snapshot.units:
        if u.unit_id == unit:
            zones = tuple(replace(z, **changes) if z.zone == zone else z for z in u.zones)  # type: ignore[arg-type]
            u = replace(u, zones=zones)
        units.append(u)
    return replace(snapshot, units=tuple(units))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"on_off": False, "control_on_off": False}, RejectReason.ZONE_OFF),
        ({"comfort_economy": False, "control_comfort_economy": False}, RejectReason.NOT_COMFORT),
        ({"live": False}, RejectReason.ZONE_NOT_LIVE),
        ({"on_off": True, "control_on_off": False}, RejectReason.CONFLICTING_BITS),
        (
            {"comfort_economy": None, "control_comfort_economy": None},
            RejectReason.MODE_UNKNOWN,
        ),
    ],
)
async def test_zone_state_blocks_the_target(
    changes: dict[str, object], reason: RejectReason
) -> None:
    snapshot = _with_zone(await Rig().cycle(), 2, 2, **changes)
    with pytest.raises(CommandRejectedError) as err:
        validate_wheel_target(
            unit_id=2,
            zone=2,
            target=22.0,
            snapshot=snapshot,
            calibration=CAL,
            permissions=ALLOW_2_2,
        )
    assert err.value.reason is reason


async def test_global_master_bits_do_not_block_the_target() -> None:
    """Real install: master comfort/economy is False while zones are in comfort (hardware run)."""
    rig = Rig()
    snapshot = await rig.cycle()
    unit = snapshot.unit(2)
    assert unit is not None and unit.controller is not None
    snapshot = replace(
        snapshot,
        units=tuple(
            replace(u, controller=replace(u.controller, global_comfort_economy=False))  # type: ignore[arg-type]
            if u.unit_id == 2
            else u
            for u in snapshot.units
        ),
    )
    assert (
        validate_wheel_target(
            unit_id=2,
            zone=2,
            target=22.0,
            snapshot=snapshot,
            calibration=CAL,
            permissions=ALLOW_2_2,
        )
        == 6
    )


# -- queue and cycle ordering -----------------------------------------------------------------


async def test_submit_causes_no_traffic_and_state_is_not_optimistic() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    before = len(rig.backend.records)
    record = rig.submit(2, 2, 22.0, snapshot)
    assert record.status is CommandStatus.QUEUED
    assert len(rig.backend.records) == before  # nothing sent, no focused re-read
    assert rig.backend.writes == []
    assert snapshot.unit(2).zone(2).wheel_code == 7  # type: ignore[union-attr]


async def test_write_is_flushed_before_the_reads_of_the_next_cycle_and_confirmed() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    rig.clock.now += 60
    rig.submit(2, 2, 22.0, snapshot)
    after = await rig.cycle()
    cycle_ops = rig.backend.records[-(len(rig.backend.records) - 29) :]  # cycle 2 only
    assert cycle_ops[0].op == "write_register"
    assert all(r.op != "write_register" for r in cycle_ops[1:])
    assert rig.backend.writes == [("write_register", 2, CODE_REG(2), 6)]
    assert after.unit(2).zone(2).wheel_code == 6  # type: ignore[union-attr]
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.CONFIRMED
    # not written again in later cycles (no replay)
    rig.clock.now += 60
    await rig.cycle()
    assert len(rig.backend.writes) == 1


async def test_latest_value_wins_and_only_one_zone_register_is_written() -> None:
    rig = Rig(ALLOW_ALL)
    snapshot = await rig.cycle()
    first = rig.submit(2, 2, 23.0, snapshot)
    rig.submit(2, 2, 21.0, snapshot)
    rig.submit(1, 3, 22.5, snapshot)
    assert rig.queue.pending_count == 2
    await rig.cycle()
    assert rig.backend.writes == [
        ("write_register", 1, CODE_REG(3), 7),
        ("write_register", 2, CODE_REG(2), 4),
    ]
    assert first.command.command_id != rig.queue.latest(2, 2).command.command_id  # type: ignore[union-attr]
    assert any(
        r.command.command_id == first.command.command_id and r.status is CommandStatus.SUPERSEDED
        for r in rig.queue.history
    )
    assert not [w for w in rig.backend.writes if w[0] == "write_coil"]


async def test_writes_respect_the_one_request_per_second_limit() -> None:
    rig = Rig(ALLOW_ALL)
    snapshot = await rig.cycle()
    for zone in range(1, 5):
        rig.submit(2, zone, 22.0, snapshot)
    await rig.cycle()
    starts = [r.start for r in rig.backend.records]
    assert all(b - a >= 1.0 - 1e-9 for a, b in itertools.pairwise(starts))


async def test_read_back_lag_then_timeout_without_rewrite() -> None:
    rig = Rig()
    rig.backend.ignore_writes = True  # device accepts the write but the value never changes
    snapshot = await rig.cycle()
    rig.submit(2, 2, 22.0, snapshot)
    await rig.cycle()
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.SENT  # still waiting
    await rig.cycle()
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.TIMEOUT
    assert record.observed_code == 7
    assert len(rig.backend.writes) == 1  # never resent


async def test_physical_dial_change_wins_over_stale_command_state() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    rig.submit(2, 2, 22.0, snapshot)
    await rig.cycle()  # confirmed at code 6
    rig.backend.units[2].registers[CODE_REG(2)] = 9  # somebody turns the physical dial
    after = await rig.cycle()
    assert after.unit(2).zone(2).wheel_code == 9  # type: ignore[union-attr]
    assert len(rig.backend.writes) == 1  # polling never undoes it


async def test_expired_command_is_dropped_without_traffic() -> None:
    rig = Rig(ttl=30.0)
    snapshot = await rig.cycle()
    rig.submit(2, 2, 22.0, snapshot)
    rig.clock.now += 60  # next tick is later than the expiry
    await rig.cycle()
    assert rig.backend.writes == []
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.TIMEOUT
    assert record.detail == RejectReason.EXPIRED.value


async def test_no_replay_after_reconnect() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    rig.submit(2, 2, 22.0, snapshot)
    rig.backend._connected = False  # connection lost between submit and flush
    rig.clock.now += 5
    await rig.cycle()
    assert rig.backend.writes == []
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.TIMEOUT
    assert record.detail == RejectReason.CONNECTION_CHANGED.value


async def test_device_exception_is_rejected_and_reads_continue() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    rig.backend.faults.append(
        Fault("exception", unit_id=2, table=p.Table.HOLDING_REGISTER, address=CODE_REG(2))
    )
    rig.submit(2, 2, 22.0, snapshot)
    after = await rig.cycle()
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.REJECTED
    assert all(u.available for u in after.units)  # unrelated reads unaffected


async def test_transport_failure_marks_transport_error() -> None:
    rig = Rig()
    snapshot = await rig.cycle()
    rig.backend.faults.append(
        Fault("disconnect", unit_id=2, table=p.Table.HOLDING_REGISTER, address=CODE_REG(2))
    )
    rig.submit(2, 2, 22.0, snapshot)
    await rig.cycle()
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.TRANSPORT_ERROR
    assert len(rig.backend.writes) == 1  # attempted once, never replayed
    rig.clock.now += 600
    await rig.cycle()
    await rig.cycle()
    assert len(rig.backend.writes) == 1


async def test_empty_queue_cycles_make_zero_writes() -> None:
    rig = Rig(ALLOW_ALL)
    for _ in range(5):
        await rig.cycle()
        rig.clock.now += 60
    assert rig.backend.writes == []
    assert {r.op for r in rig.backend.records} <= {"read_holding_registers", "read_coils"}


# -- transport hard limits ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "code"),
    [(4127, 5), (4611, 5), (4603, 5), (CODE_REG(1), 13), (CODE_REG(1), -1)],
)
async def test_transport_refuses_anything_but_one_wheel_register_in_range(
    address: int, code: int
) -> None:
    rig = Rig()
    with pytest.raises(WtcRequestRejectedError):
        await rig.transport.write_wheel_code(1, address, code)
    assert rig.backend.writes == [] and rig.backend.records == []


async def test_read_only_transport_has_no_write_route() -> None:
    clock = VirtualClock()
    transport, _ = build_client("192.0.2.1", 502, UNITS, backend=MockWtcBackend.for_profile(clock))
    assert not isinstance(transport, WritableGatewayTransport)
    assert not hasattr(transport, "write_wheel_code")


# -- cooling comfort targets (decision 11: same rule as heating, cooling neutral and step) -------


def _make_cooling(rig: Rig, unit: int, zone: int) -> None:
    mem = rig.backend.units[unit].coils
    mem[p.zone_status_coil_base(zone) + 6] = True
    mem[p.zone_control_coil_base(zone) + 2] = True


async def test_cooling_target_uses_the_cooling_neutral_and_step() -> None:
    rig = Rig()
    _make_cooling(rig, 2, 2)
    snapshot = await rig.cycle()
    for target, code in ((26.0, 6), (26.5, 7), (24.5, 3), (29.0, 12)):
        assert (
            validate_wheel_target(
                unit_id=2,
                zone=2,
                target=target,
                snapshot=snapshot,
                calibration=CAL,
                permissions=ALLOW_2_2,
            )
            == code
        )
    with pytest.raises(CommandRejectedError) as err:  # 22 C is a heating value, out of range
        validate_wheel_target(
            unit_id=2,
            zone=2,
            target=22.0,
            snapshot=snapshot,
            calibration=CAL,
            permissions=ALLOW_2_2,
        )
    assert err.value.reason is RejectReason.INVALID_TARGET


async def test_cooling_target_is_written_and_confirmed() -> None:
    rig = Rig()
    _make_cooling(rig, 2, 2)
    snapshot = await rig.cycle()
    rig.clock.now += 60
    rig.submit(2, 2, 26.5, snapshot)
    await rig.cycle()
    assert rig.backend.writes == [("write_register", 2, CODE_REG(2), 7)]
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.CONFIRMED
    assert not [w for w in rig.backend.writes if w[0] == "write_coil"]


async def test_cooling_economy_is_still_rejected() -> None:
    rig = Rig()
    _make_cooling(rig, 2, 2)
    snapshot = _with_zone(
        await rig.cycle(), 2, 2, comfort_economy=False, control_comfort_economy=False
    )
    with pytest.raises(CommandRejectedError) as err:
        validate_wheel_target(
            unit_id=2,
            zone=2,
            target=26.0,
            snapshot=snapshot,
            calibration=CAL,
            permissions=ALLOW_2_2,
        )
    assert err.value.reason is RejectReason.NOT_COMFORT


# -- lock commands (decision 13): separate opt-in, status lags the command ---------------------

LOCK_COIL = p.zone_lock_command_coil
LOCK_RIG = CommandPermissions(True, frozenset(), frozenset({(1, 4)}))


def _submit_lock(rig: Rig, unit: int, zone: int, locked: bool, snapshot: GatewaySnapshot) -> None:
    validate_lock_target(unit_id=unit, zone=zone, snapshot=snapshot, permissions=rig.permissions)
    rig.queue.submit(
        rig.queue.create(
            CommandKind.LOCK, unit, zone, locked, int(locked), rig.transport.connection_generation
        )
    )


async def test_lock_permission_is_separate_from_wheel_permission() -> None:
    rig = Rig(LOCK_RIG)
    snapshot = await rig.cycle()
    _submit_lock(rig, 1, 4, True, snapshot)  # allowed
    with pytest.raises(CommandRejectedError) as err:
        _submit_lock(rig, 1, 3, True, snapshot)
    assert err.value.reason is RejectReason.ZONE_NOT_PERMITTED
    with pytest.raises(CommandRejectedError) as wheel_err:  # no wheel permission for this zone
        validate_wheel_target(
            unit_id=1,
            zone=4,
            target=22.0,
            snapshot=snapshot,
            calibration=CAL,
            permissions=LOCK_RIG,
        )
    assert wheel_err.value.reason is RejectReason.ZONE_NOT_PERMITTED
    off = CommandPermissions(False, frozenset(), frozenset({(1, 4)}))
    with pytest.raises(CommandRejectedError) as off_err:
        validate_lock_target(unit_id=1, zone=4, snapshot=snapshot, permissions=off)
    assert off_err.value.reason is RejectReason.WRITE_DISABLED


async def test_lock_is_queued_then_written_as_a_single_coil_before_the_reads() -> None:
    rig = Rig(LOCK_RIG)
    snapshot = await rig.cycle()
    rig.clock.now += 60
    before = len(rig.backend.records)
    _submit_lock(rig, 1, 4, True, snapshot)
    assert len(rig.backend.records) == before and rig.backend.writes == []
    after = await rig.cycle()
    cycle_ops = rig.backend.records[before:]
    assert cycle_ops[0].op == "write_coil" and all(r.op != "write_coil" for r in cycle_ops[1:])
    assert rig.backend.writes == [("write_coil", 1, LOCK_COIL(4), True)]
    assert after.unit(1).zone(4).lock_status is True  # type: ignore[union-attr]
    record = rig.queue.latest(1, 4, CommandKind.LOCK)
    assert record is not None and record.status is CommandStatus.CONFIRMED
    # no global coil and no other zone's coil
    assert not [w for w in rig.backend.writes if w[2] in (4127, 4128, 4129)]


async def test_lagging_lock_status_is_confirmed_by_a_later_cycle_without_rewrite() -> None:
    rig = Rig(LOCK_RIG)
    rig.backend.lock_delay = 100.0
    snapshot = await rig.cycle()
    rig.clock.now += 60
    _submit_lock(rig, 1, 4, True, snapshot)
    await rig.cycle()
    record = rig.queue.latest(1, 4, CommandKind.LOCK)
    assert record is not None and record.status is CommandStatus.SENT  # status still lags
    for _ in range(3):  # confirmed within the verification window, never rewritten
        rig.clock.now += 60
        await rig.cycle()
        record = rig.queue.latest(1, 4, CommandKind.LOCK)
        assert record is not None
        if record.status is CommandStatus.CONFIRMED:
            break
    assert record.status is CommandStatus.CONFIRMED
    assert len(rig.backend.writes) == 1


async def test_lock_that_never_shows_up_times_out_after_four_cycles_with_one_write() -> None:
    rig = Rig(LOCK_RIG)
    rig.backend.ignore_writes = True
    snapshot = await rig.cycle()
    _submit_lock(rig, 1, 4, True, snapshot)
    for _ in range(3):
        rig.clock.now += 60
        await rig.cycle()
        record = rig.queue.latest(1, 4, CommandKind.LOCK)
        assert record is not None and record.status is CommandStatus.SENT
    rig.clock.now += 60
    await rig.cycle()
    record = rig.queue.latest(1, 4, CommandKind.LOCK)
    assert record is not None and record.status is CommandStatus.TIMEOUT
    assert len(rig.backend.writes) == 1


@pytest.mark.parametrize("address", [4127, 4128, 4129, 4604, 4655, 4663])
async def test_transport_refuses_any_coil_but_the_lock_coils(address: int) -> None:
    rig = Rig(LOCK_RIG)
    with pytest.raises(WtcRequestRejectedError):
        await rig.transport.write_lock_coil(1, address, True)
    assert rig.backend.writes == [] and rig.backend.records == []


# -- zone on/off and preset (decision 16): separate opt-in, control coils 0 and 1 only ----------

MODE_RIG = CommandPermissions(True, frozenset(), frozenset(), frozenset({(2, 2)}))
POWER = CommandKind.ZONE_POWER
PRESET = CommandKind.ZONE_PRESET


def _submit_mode(rig: Rig, kind: CommandKind, value: bool, snapshot: GatewaySnapshot) -> None:
    validate_zone_mode(unit_id=2, zone=2, kind=kind, snapshot=snapshot, permissions=rig.permissions)
    rig.queue.submit(
        rig.queue.create(kind, 2, 2, value, int(value), rig.transport.connection_generation)
    )


@pytest.mark.parametrize(("kind", "coil_offset"), [(POWER, 0), (PRESET, 1)])
async def test_zone_mode_writes_one_control_coil_before_the_reads_and_confirms(
    kind: CommandKind, coil_offset: int
) -> None:
    rig = Rig(MODE_RIG)
    snapshot = await rig.cycle()
    rig.clock.now += 60
    before = len(rig.backend.records)
    _submit_mode(rig, kind, False, snapshot)
    assert len(rig.backend.records) == before and rig.backend.writes == []
    after = await rig.cycle()
    cycle_ops = rig.backend.records[before:]
    assert cycle_ops[0].op == "write_coil" and all(r.op != "write_coil" for r in cycle_ops[1:])
    address = p.zone_control_coil_base(2) + coil_offset
    assert rig.backend.writes == [("write_coil", 2, address, False)]
    zone = after.unit(2).zone(2)  # type: ignore[union-attr]
    assert zone is not None
    assert (zone.on_off if kind is POWER else zone.comfort_economy) is False
    record = rig.queue.latest(2, 2, kind)
    assert record is not None and record.status is CommandStatus.CONFIRMED


async def test_zone_mode_permission_is_separate_from_wheel_and_lock() -> None:
    snapshot = await Rig().cycle()
    for permissions in (ALLOW_2_2, LOCK_RIG, CommandPermissions(True, frozenset({(2, 2)}))):
        with pytest.raises(CommandRejectedError) as err:
            validate_zone_mode(
                unit_id=2, zone=2, kind=POWER, snapshot=snapshot, permissions=permissions
            )
        assert err.value.reason is RejectReason.ZONE_NOT_PERMITTED
    off = CommandPermissions(False, frozenset(), frozenset(), frozenset({(2, 2)}))
    with pytest.raises(CommandRejectedError) as off_err:
        validate_zone_mode(unit_id=2, zone=2, kind=POWER, snapshot=snapshot, permissions=off)
    assert off_err.value.reason is RejectReason.WRITE_DISABLED


async def test_zone_mode_is_blocked_when_status_and_control_disagree_or_zone_not_live() -> None:
    rig = Rig(MODE_RIG)
    snapshot = await rig.cycle()
    for changes, reason in (
        ({"on_off": True, "control_on_off": False}, RejectReason.CONFLICTING_BITS),
        ({"live": False}, RejectReason.ZONE_NOT_LIVE),
    ):
        with pytest.raises(CommandRejectedError) as err:
            validate_zone_mode(
                unit_id=2,
                zone=2,
                kind=PRESET,
                snapshot=_with_zone(snapshot, 2, 2, **changes),
                permissions=MODE_RIG,
            )
        assert err.value.reason is reason


async def test_zone_mode_that_never_shows_up_times_out_after_three_cycles_with_one_write() -> None:
    rig = Rig(MODE_RIG)
    rig.backend.ignore_writes = True
    snapshot = await rig.cycle()
    _submit_mode(rig, POWER, False, snapshot)
    for _ in range(2):
        rig.clock.now += 60
        await rig.cycle()
        record = rig.queue.latest(2, 2, POWER)
        assert record is not None and record.status is CommandStatus.SENT
    rig.clock.now += 60
    await rig.cycle()
    record = rig.queue.latest(2, 2, POWER)
    assert record is not None and record.status is CommandStatus.TIMEOUT
    assert len(rig.backend.writes) == 1


@pytest.mark.parametrize("offset", [2, 3, 4, 5, 6, 7])
async def test_transport_refuses_the_heat_cool_and_other_zone_control_coils(offset: int) -> None:
    rig = Rig(MODE_RIG)
    with pytest.raises(WtcRequestRejectedError):
        await rig.transport.write_zone_mode_coil(2, p.zone_control_coil_base(2) + offset, True)
    for address in (4127, 4128, 4129):
        with pytest.raises(WtcRequestRejectedError):
            await rig.transport.write_zone_mode_coil(2, address, True)
    assert rig.backend.writes == [] and rig.backend.records == []
