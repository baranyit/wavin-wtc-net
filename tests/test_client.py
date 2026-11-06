from __future__ import annotations

import pytest

from custom_components.wavin_wtc_net import protocol as p
from custom_components.wavin_wtc_net.client import UnitConfig, WtcReadClient
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
    assert len(backend.records) == client.requests_per_cycle


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
    backend.faults.append(Fault("exception", unit_id=2, address=p.REG_WATER_TEMPERATURE))
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
