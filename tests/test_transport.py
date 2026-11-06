from __future__ import annotations

import asyncio
import itertools

import pytest

from custom_components.wavin_wtc_net.errors import (
    WtcBackoffError,
    WtcConnectionError,
    WtcMalformedResponseError,
    WtcModbusExceptionError,
    WtcRequestRejectedError,
    WtcTimeoutError,
)
from custom_components.wavin_wtc_net.protocol import Table
from custom_components.wavin_wtc_net.transport import GatewayTransport, TransportSettings

from .mock_wtc import Fault, MockWtcBackend, VirtualClock


def make(
    settings: TransportSettings | None = None,
) -> tuple[VirtualClock, MockWtcBackend, GatewayTransport]:
    clock = VirtualClock()
    backend = MockWtcBackend.for_profile(clock)
    return clock, backend, GatewayTransport(backend, settings, clock)


def gaps(backend: MockWtcBackend) -> list[float]:
    starts = [r.start for r in backend.records]
    return [b - a for a, b in itertools.pairwise(starts)]


async def test_sequential_requests_start_at_least_one_second_apart() -> None:
    _, backend, transport = make()
    for _ in range(5):
        await transport.read_holding_registers(1, 4121, 15)
        await transport.read_coils(2, 4120, 16)
    assert len(backend.records) == 10
    assert min(gaps(backend)) >= 1.0


async def test_concurrent_callers_are_serialized_and_paced() -> None:
    _, backend, transport = make()
    await asyncio.gather(
        *(transport.read_holding_registers(unit, 4136, 15) for unit in (1, 2) for _ in range(4))
    )
    assert backend.max_in_flight == 1
    assert min(gaps(backend)) >= 1.0


@pytest.mark.parametrize(
    ("table", "count"),
    [(Table.HOLDING_REGISTER, 16), (Table.COIL, 17), (Table.HOLDING_REGISTER, 0)],
)
async def test_oversized_or_empty_requests_never_reach_the_wire(table: Table, count: int) -> None:
    _, backend, transport = make()
    with pytest.raises(WtcRequestRejectedError):
        if table is Table.HOLDING_REGISTER:
            await transport.read_holding_registers(1, 4121, count)
        else:
            await transport.read_coils(1, 4120, count)
    assert backend.records == [] and backend.connect_attempts == 0


@pytest.mark.parametrize("unit_id", [0, 248])
async def test_invalid_unit_id_rejected_locally(unit_id: int) -> None:
    _, backend, transport = make()
    with pytest.raises(WtcRequestRejectedError):
        await transport.read_coils(unit_id, 4120, 1)
    assert backend.records == []


def test_settings_bounds() -> None:
    with pytest.raises(ValueError):
        TransportSettings(min_request_interval=0.1)
    with pytest.raises(ValueError):
        TransportSettings(max_registers_per_request=31)
    with pytest.raises(ValueError):
        TransportSettings(max_coils_per_request=65)
    with pytest.raises(ValueError):
        TransportSettings(backoff_initial=10, backoff_max=5)


async def test_address_offset_is_applied_on_the_wire() -> None:
    _, backend, transport = make(TransportSettings(address_offset=-1))
    await transport.read_holding_registers(1, 4122, 2)
    assert backend.records[0].address == 4121


async def test_modbus_exception_keeps_connection() -> None:
    _, backend, transport = make()
    backend.faults.append(Fault("exception", exception_code=2))
    with pytest.raises(WtcModbusExceptionError) as info:
        await transport.read_holding_registers(1, 4121, 1)
    assert info.value.exception_code == 2
    assert backend.connected
    await transport.read_holding_registers(1, 4121, 1)
    assert backend.connect_attempts == 1


@pytest.mark.parametrize("kind", ["short", "garbage"])
@pytest.mark.parametrize("table", [Table.HOLDING_REGISTER, Table.COIL])
async def test_malformed_responses_are_rejected(kind: str, table: Table) -> None:
    _, backend, transport = make()
    backend.faults.append(Fault(kind))
    with pytest.raises(WtcMalformedResponseError):
        if table is Table.HOLDING_REGISTER:
            await transport.read_holding_registers(1, 4121, 4)
        else:
            await transport.read_coils(1, 4120, 4)
    assert transport.stats.failures == 1


async def test_disconnect_triggers_bounded_exponential_backoff_without_traffic() -> None:
    clock, backend, transport = make(TransportSettings(backoff_initial=5, backoff_max=20))
    backend.outages.append((clock.now + 0.5, clock.now + 10_000))
    await transport.read_holding_registers(1, 4121, 1)
    delays = []
    for _ in range(5):
        with pytest.raises(WtcConnectionError):
            await transport.read_holding_registers(1, 4121, 1)
        until = transport.stats.backoff_until
        assert until is not None
        delays.append(round(until - clock.now, 2))
        traffic = (len(backend.records), backend.connect_attempts)
        with pytest.raises(WtcBackoffError):
            await transport.read_holding_registers(1, 4121, 1)
        assert (len(backend.records), backend.connect_attempts) == traffic
        clock.now = until
    assert delays == [5.0, 10.0, 20.0, 20.0, 20.0]


async def test_recovers_after_outage_and_resets_backoff() -> None:
    clock, backend, transport = make()
    backend.outages.append((clock.now, clock.now + 7))
    with pytest.raises(WtcConnectionError):
        await transport.read_coils(1, 4120, 16)
    clock.now += 4.0
    with pytest.raises(WtcBackoffError):
        await transport.read_coils(1, 4120, 16)
    clock.now += 20.0
    assert len(await transport.read_coils(1, 4120, 16)) == 16
    stats = transport.stats
    assert stats.connected and stats.backoff_until is None
    assert stats.consecutive_connection_failures == 0


async def test_timeout_closes_connection() -> None:
    _, backend, transport = make(TransportSettings(request_timeout=0.05))
    backend.faults.append(Fault("timeout"))
    with pytest.raises(WtcTimeoutError):
        await transport.read_holding_registers(1, 4121, 1)
    assert not backend.connected
    assert transport.stats.backoff_until is not None


def test_transport_has_no_write_api() -> None:
    assert not [name for name in dir(GatewayTransport) if "write" in name.lower()]
