from __future__ import annotations

import itertools

import pytest

from custom_components.wavin_wtc_net import protocol as p
from custom_components.wavin_wtc_net.client import UnitConfig, WtcReadClient, minimum_poll_interval
from custom_components.wavin_wtc_net.models import ControllerMode
from custom_components.wavin_wtc_net.transport import GatewayTransport, TransportSettings

from .mock_wtc import REAL_PROFILE, Fault, MockWtcBackend, VirtualClock


def make() -> tuple[VirtualClock, MockWtcBackend, WtcReadClient]:
    clock = VirtualClock()
    backend = MockWtcBackend.for_profile(clock)
    transport = GatewayTransport(backend, TransportSettings(), clock)
    units = [UnitConfig(uid, tuple(zones)) for uid, zones in REAL_PROFILE.items()]
    return clock, backend, WtcReadClient(transport, units, clock=clock)


async def test_six_plus_four_snapshot_decodes_ten_zones_without_writes() -> None:
    _, backend, client = make()
    snapshot = await client.read_snapshot()
    unit1, unit2 = snapshot.unit(1), snapshot.unit(2)
    assert unit1 is not None and unit2 is not None
    assert unit1.available and unit2.available
    assert [z.zone for z in unit1.zones] == [1, 2, 3, 4, 5, 6]
    assert [z.zone for z in unit2.zones] == [1, 2, 3, 4]
    assert unit1.controller is not None
    assert unit1.controller.water_temperature == 31.5
    assert unit1.controller.mode is ControllerMode.HEAT
    assert unit1.controller.serial == (0x12 << 16) | 0x5679
    zone = unit2.zone(3)
    assert zone is not None
    assert zone.temperature == 21.7  # 20.0 + 3*0.5 + 2*0.1
    assert zone.comfort_heat_setpoint == 22.0 and zone.comfort_cool_setpoint == 26.0
    assert zone.wheel_code == 5 and zone.live is True and zone.valve_open is True
    assert zone.wheel_offset == -0.5
    assert zone.packed_wheel_word is not None
    assert p.packed_wheel_code(zone.packed_wheel_word, 3, p.WheelByteOrder.LOW_BYTE_FIRST) == 5
    assert unit1.failed_optional_reads == ()
    assert backend.writes == []
    assert len(backend.records) == client.first_cycle_requests == 29


async def test_snapshot_only_keeps_requested_addresses() -> None:
    _, _, client = make()
    snapshot = await client.read_snapshot()
    unit = snapshot.unit(1)
    assert unit is not None and unit.raw is not None
    wanted = {pt.address for pt in p.unit_points(range(1, 7)) if pt.table is p.Table.COIL}
    assert set(unit.raw.coils) == wanted


async def test_every_request_respects_caps() -> None:
    _, backend, client = make()
    await client.read_snapshot()
    for record in backend.records:
        cap = 15 if record.op == "read_holding_registers" else 16
        assert 1 <= record.count <= cap


async def test_optional_read_failure_keeps_unit_available() -> None:
    _, backend, client = make()
    backend.unreadable_registers.add(p.REG_FIRMWARE)
    snapshot = await client.read_snapshot()
    unit = snapshot.unit(1)
    assert unit is not None and unit.available
    assert unit.controller is not None and unit.controller.firmware is None
    assert unit.controller.serial is None
    assert any("4500" in item for item in unit.failed_optional_reads)


async def test_required_read_failure_marks_only_that_unit_unavailable() -> None:
    _, backend, client = make()
    backend.faults.append(
        Fault("exception", unit_id=2, address=p.REG_WATER_TEMPERATURE, remaining=99)
    )
    snapshot = await client.read_snapshot()
    unit1, unit2 = snapshot.unit(1), snapshot.unit(2)
    assert unit1 is not None and unit1.available
    assert unit2 is not None and not unit2.available
    assert unit2.zones == () and unit2.controller is None and unit2.error


async def test_offline_unit_does_not_affect_other_unit() -> None:
    _, backend, client = make()
    backend.offline_units.add(1)
    snapshot = await client.read_snapshot()
    assert snapshot.unit(1) is not None and not snapshot.unit(1).available  # type: ignore[union-attr]
    assert snapshot.unit(2) is not None and snapshot.unit(2).available  # type: ignore[union-attr]


async def test_connection_loss_marks_units_unavailable_not_stale() -> None:
    clock, backend, client = make()
    first = await client.read_snapshot()
    assert all(unit.available for unit in first.units)
    backend.outages.append((clock.now, clock.now + 30))
    during = await client.read_snapshot()
    assert not any(unit.available for unit in during.units)
    assert all(unit.zones == () for unit in during.units)
    clock.now += 400  # past outage and any backoff
    after = await client.read_snapshot()
    assert all(unit.available for unit in after.units)
    assert backend.writes == []


def test_unit_config_validation() -> None:
    with pytest.raises(ValueError):
        UnitConfig(1, ())
    with pytest.raises(ValueError):
        UnitConfig(1, (1, 1))
    with pytest.raises(ValueError):
        UnitConfig(1, (8,))
    with pytest.raises(ValueError):
        UnitConfig(0, (1,))


# -- decision 2: static points on a slow cadence ------------------------------------------


def _static_reads(backend: MockWtcBackend) -> int:
    return sum(r.address == p.REG_FIRMWARE for r in backend.records)


async def test_static_points_are_read_first_then_hourly_and_cached() -> None:
    clock, backend, client = make()
    assert (client.requests_per_cycle, client.first_cycle_requests) == (27, 29)
    first = await client.read_snapshot()
    assert len(backend.records) == 29 and _static_reads(backend) == 2

    backend.records.clear()
    second = await client.read_snapshot()
    assert len(backend.records) == 27 and _static_reads(backend) == 0
    unit1 = second.unit(1)
    assert unit1 is not None and unit1.controller is not None
    assert unit1.controller.firmware == 1008 or unit1.controller.firmware is not None
    assert unit1.controller.serial == (0x12 << 16) | 0x5679
    first_unit = first.unit(1)
    assert first_unit is not None and first_unit.controller is not None
    assert unit1.controller.serial == first_unit.controller.serial

    backend.records.clear()
    clock.now += 3600
    await client.read_snapshot()
    assert len(backend.records) == 29 and _static_reads(backend) == 2
    assert backend.writes == []


async def test_static_points_are_read_again_after_reconnect() -> None:
    clock, backend, client = make()
    await client.read_snapshot()
    backend.records.clear()
    backend.faults.append(Fault("disconnect", unit_id=1, remaining=1))
    await client.read_snapshot()  # unit 1 fails on its first request; connection drops
    clock.now += 10  # past the reconnect backoff
    backend.records.clear()
    snapshot = await client.read_snapshot()
    assert all(u.available for u in snapshot.units)
    assert _static_reads(backend) == 2  # re-read for both units on the new connection


async def test_static_read_failure_keeps_unit_available_and_keeps_last_values() -> None:
    clock, backend, client = make()
    first = await client.read_snapshot()
    serial = first.unit(1).controller.serial  # type: ignore[union-attr]
    backend.unreadable_registers.add(p.REG_FIRMWARE)
    clock.now += 3600
    snapshot = await client.read_snapshot()
    unit = snapshot.unit(1)
    assert unit is not None and unit.available and unit.controller is not None
    assert unit.controller.serial == serial  # last known static value survives
    assert any("4500" in item for item in unit.failed_optional_reads)


def test_minimum_poll_interval() -> None:
    assert minimum_poll_interval(29, 1.0) == 44.0
    assert minimum_poll_interval(5, 1.0) == 30.0
    assert minimum_poll_interval(29, 2.0) == 87.0
    with pytest.raises(ValueError):
        minimum_poll_interval(0, 1.0)
    _, _, client = make()
    assert client.minimum_poll_interval == 44.0


# -- decision 3: max_gap ------------------------------------------------------------------


@pytest.mark.parametrize("gap", range(0, 9))
async def test_every_gap_respects_caps_and_never_writes(gap: int) -> None:
    clock = VirtualClock()
    backend = MockWtcBackend.for_profile(clock)
    transport = GatewayTransport(backend, TransportSettings(), clock)
    units = [UnitConfig(uid, tuple(zones)) for uid, zones in REAL_PROFILE.items()]
    client = WtcReadClient(transport, units, max_gap=gap, clock=clock)
    snapshot = await client.read_snapshot()
    assert all(u.available for u in snapshot.units)
    for record in backend.records:
        assert 1 <= record.count <= (15 if record.op == "read_holding_registers" else 16)
    assert backend.writes == []
    if gap == 0:
        assert client.first_cycle_requests == 41
    if gap == 8:
        assert client.first_cycle_requests == 29


def test_max_gap_is_bounded() -> None:
    clock = VirtualClock()
    transport = GatewayTransport(MockWtcBackend.for_profile(clock), TransportSettings(), clock)
    units = [UnitConfig(1, (1,))]
    for bad in (-1, 9):
        with pytest.raises(ValueError):
            WtcReadClient(transport, units, max_gap=bad)


# -- decision 4: fallback split -----------------------------------------------------------


async def test_reference_only_failure_falls_back_and_is_remembered() -> None:
    _, backend, client = make()
    await client.read_snapshot()
    backend.records.clear()
    # a reference-only address inside a required request (zone 1 illuminance, B+5)
    bad = p.zone_register_base(1) + 5
    backend.faults.append(
        Fault(
            "exception",
            unit_id=1,
            table=p.Table.HOLDING_REGISTER,
            address=bad,
            remaining=99,
        )
    )
    snapshot = await client.read_snapshot()
    unit = snapshot.unit(1)
    assert unit is not None and unit.available and unit.controller is not None
    assert unit.zone(1) is not None and unit.zone(1).temperature is not None  # type: ignore[union-attr]
    assert unit.zone(1).illuminance_raw is None  # type: ignore[union-attr]
    assert any("illuminance" in item for item in unit.excluded_points)
    assert unit.failed_optional_reads
    assert len(backend.records) > 27  # extra requests only in the failure cycle

    backend.records.clear()
    again = await client.read_snapshot()
    assert all(u.available for u in again.units)
    assert len(backend.records) <= 30
    assert all(
        not (r.address <= bad < r.address + r.count)
        for r in backend.records
        if r.unit_id == 1 and r.op == "read_holding_registers"
    )
    assert backend.writes == []


async def test_fallback_respects_pacing_and_caps() -> None:
    _, backend, client = make()
    bad = p.zone_register_base(1) + 5
    backend.faults.append(
        Fault(
            "exception",
            unit_id=1,
            table=p.Table.HOLDING_REGISTER,
            address=bad,
            remaining=99,
        )
    )
    await client.read_snapshot()
    starts = [r.start for r in backend.records]
    assert all(b - a >= 1.0 - 1e-9 for a, b in itertools.pairwise(starts))
    for record in backend.records:
        assert 1 <= record.count <= (15 if record.op == "read_holding_registers" else 16)


async def test_exclusions_are_cleared_on_reconnect() -> None:
    clock, backend, client = make()
    bad = p.zone_register_base(1) + 5
    backend.faults.append(
        Fault(
            "exception",
            unit_id=1,
            table=p.Table.HOLDING_REGISTER,
            address=bad,
            remaining=99,
        )
    )
    await client.read_snapshot()
    backend.faults.clear()
    backend.faults.append(Fault("disconnect", unit_id=1, remaining=1))
    await client.read_snapshot()  # connection drops
    clock.now += 10  # past the reconnect backoff
    snapshot = await client.read_snapshot()  # reconnected, address is healthy again
    unit = snapshot.unit(1)
    assert unit is not None and unit.available
    assert unit.excluded_points == ()
    assert unit.zone(1).illuminance_raw is not None  # type: ignore[union-attr]


async def test_hole_failure_is_recovered_without_hole_reads_and_logged() -> None:
    _, backend, client = make()
    # first request of unit 1 covers 4121..4135 incl. undocumented 4129..4131; a hole read fails
    backend.faults.append(
        Fault("exception", unit_id=1, table=p.Table.HOLDING_REGISTER, address=4130, remaining=99)
    )
    snapshot = await client.read_snapshot()
    unit1 = snapshot.unit(1)
    assert unit1 is not None and unit1.available
    assert unit1.excluded_points == ()  # nothing was lost, only the hole read was dropped
    assert client.hole_failures and all(count >= 1 for count in client.hole_failures.values())
    assert backend.writes == []
