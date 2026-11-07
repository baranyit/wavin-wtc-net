"""Integration tests: real pymodbus client <-> real pymodbus TCP server on localhost.

``PymodbusBackend`` and ``GatewayTransport`` talk to an in-process pymodbus server
bound to 127.0.0.1 on an ephemeral port, filled with the 6+4 reference profile from
``mock_wtc``. Never pointed at real hardware. Skipped when pymodbus is not installed.

The transport uses ``VirtualClock`` for pacing, so the 1 req/s limiter is exercised
without real sleeping; only the network I/O is real. Every request PDU the server
receives is recorded so the tests can prove that no write function code is ever sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import socket
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("pymodbus")

from pymodbus import datastore as pm_datastore
from pymodbus.server import ModbusTcpServer

from custom_components.wavin_wtc_net import protocol as p
from custom_components.wavin_wtc_net.client import UnitConfig, WtcReadClient
from custom_components.wavin_wtc_net.errors import (
    WtcBackoffError,
    WtcConnectionError,
    WtcModbusExceptionError,
    WtcTimeoutError,
)
from custom_components.wavin_wtc_net.models import ControllerMode
from custom_components.wavin_wtc_net.pymodbus_backend import PymodbusBackend
from custom_components.wavin_wtc_net.transport import GatewayTransport, TransportSettings

from .mock_wtc import REAL_PROFILE, MockWtcUnit, VirtualClock

# Real loopback sockets only (127.0.0.1); pytest-socket blocks them by default.
pytestmark = pytest.mark.usefixtures("socket_enabled")

HOST = "127.0.0.1"
REGISTER_SPAN = range(4100, 4720)
COIL_SPAN = range(4100, 4680)
READ_FUNCTION_CODES = {1, 2, 3, 4}  # read coils / discrete inputs / holding / input
ILLEGAL_DATA_ADDRESS = 2
MIN_REQUEST_INTERVAL = 1.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        port: int = sock.getsockname()[1]
        return port


def _block(start: int, values: Sequence[Any]) -> Any:
    # pymodbus server data stores shift protocol addresses by +1 internally
    # (zero_mode=False semantics), so a block for protocol address N starts at N + 1.
    return pm_datastore.ModbusSequentialDataBlock(start + 1, list(values))


def _device_context(unit: MockWtcUnit) -> Any:
    registers = [unit.registers.get(a, 0) for a in REGISTER_SPAN]
    coils = [unit.coils.get(a, False) for a in COIL_SPAN]
    blocks = {
        "hr": _block(REGISTER_SPAN.start, registers),
        "co": _block(COIL_SPAN.start, coils),
    }
    # pymodbus >= 3.10 renamed slave -> device.
    if hasattr(pm_datastore, "ModbusDeviceContext"):
        return pm_datastore.ModbusDeviceContext(**blocks)
    return pm_datastore.ModbusSlaveContext(**blocks)


def _server_context(units: Sequence[MockWtcUnit]) -> Any:
    devices = {unit.unit_id: _device_context(unit) for unit in units}
    if hasattr(pm_datastore, "ModbusDeviceContext"):
        return pm_datastore.ModbusServerContext(devices=devices, single=False)
    return pm_datastore.ModbusServerContext(slaves=devices, single=False)


@dataclass
class LocalServer:
    port: int
    server: Any
    task: asyncio.Task[Any]
    function_codes: list[int] = field(default_factory=list)

    @property
    def write_codes(self) -> list[int]:
        return [code for code in self.function_codes if code not in READ_FUNCTION_CODES]

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.server.shutdown()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


async def _wait_listening(port: int) -> None:
    for _ in range(200):
        try:
            _, writer = await asyncio.open_connection(HOST, port)
        except OSError:
            await asyncio.sleep(0.01)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise RuntimeError("local pymodbus server did not start")


@contextlib.asynccontextmanager
async def local_server(
    profile: dict[int, dict[int, str]] = REAL_PROFILE,
) -> AsyncIterator[LocalServer]:
    units = [MockWtcUnit.build(uid, zones) for uid, zones in profile.items()]
    port = _free_port()
    codes: list[int] = []

    def trace_pdu(sending: bool, pdu: Any) -> Any:
        if not sending:
            codes.append(int(pdu.function_code))
        return pdu

    server = ModbusTcpServer(_server_context(units), address=(HOST, port), trace_pdu=trace_pdu)
    task = asyncio.create_task(server.serve_forever())
    handle = LocalServer(port, server, task, codes)
    try:
        await _wait_listening(port)
        yield handle
    finally:
        await handle.stop()


def make_transport(
    port: int, clock: VirtualClock, **settings: Any
) -> tuple[PymodbusBackend, GatewayTransport]:
    backend = PymodbusBackend(HOST, port, timeout=2.0)
    defaults: dict[str, Any] = {"request_timeout": 2.0, "connect_timeout": 2.0}
    transport = GatewayTransport(backend, TransportSettings(**(defaults | settings)), clock)
    return backend, transport


async def test_backend_reads_registers_and_coils_over_tcp() -> None:
    async with local_server() as server:
        backend = PymodbusBackend(HOST, server.port, timeout=2.0)
        await backend.connect()
        try:
            assert backend.connected
            registers = await backend.read_holding_registers(1, p.REG_WATER_TEMPERATURE, 1)
            assert list(registers) == [315]
            mode = await backend.read_holding_registers(2, p.REG_MODE, 1)
            assert list(mode) == [1]
            coils = await backend.read_coils(1, p.COIL_GLOBAL_ON_OFF, 3)
            assert list(coils) == [True, True, False]
            # Coil replies are byte padded; the adapter returns exactly `count` bits.
            assert len(await backend.read_coils(1, p.COIL_GLOBAL_ON_OFF, 11)) == 11
        finally:
            await backend.close()
        assert not backend.connected
        assert server.write_codes == []


async def test_full_snapshot_of_six_plus_four_profile_without_writes() -> None:
    async with local_server() as server:
        clock = VirtualClock()
        _, transport = make_transport(server.port, clock)
        units = [UnitConfig(uid, tuple(zones)) for uid, zones in REAL_PROFILE.items()]
        client = WtcReadClient(transport, units, clock=clock)
        try:
            snapshot = await client.read_snapshot()
        finally:
            await transport.close()

        unit1, unit2 = snapshot.unit(1), snapshot.unit(2)
        assert unit1 is not None and unit2 is not None
        assert unit1.available and unit2.available
        assert [z.zone for z in unit1.zones] == [1, 2, 3, 4, 5, 6]
        assert [z.zone for z in unit2.zones] == [1, 2, 3, 4]
        assert unit1.controller is not None
        assert unit1.controller.water_temperature == 31.5
        assert unit1.controller.mode is ControllerMode.HEAT
        zone = unit2.zone(3)
        assert zone is not None
        assert zone.temperature == 21.7
        assert zone.wheel_code == 5 and zone.live is True and zone.valve_open is True

        # One cycle = exactly the planned requests, all reads, all over one connection.
        assert len(server.function_codes) == client.first_cycle_requests
        assert transport.stats.requests_started == client.first_cycle_requests
        assert set(server.function_codes) <= {1, 3}
        assert server.write_codes == []


async def test_pacing_holds_one_request_per_second_across_units() -> None:
    async with local_server() as server:
        clock = VirtualClock()
        starts: list[float] = []
        _, transport = make_transport(server.port, clock)
        try:
            for unit_id in (1, 2, 1, 2):
                await transport.read_holding_registers(unit_id, p.REG_WATER_TEMPERATURE, 15)
                starts.append(clock.monotonic())
        finally:
            await transport.close()
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert all(gap >= MIN_REQUEST_INTERVAL for gap in gaps)
        assert server.write_codes == []


async def test_modbus_exception_keeps_connection_usable() -> None:
    async with local_server() as server:
        clock = VirtualClock()
        backend, transport = make_transport(server.port, clock)
        try:
            with pytest.raises(WtcModbusExceptionError) as excinfo:
                await transport.read_holding_registers(1, 9000, 1)
            assert excinfo.value.exception_code == ILLEGAL_DATA_ADDRESS
            assert backend.connected
            assert transport.stats.backoff_until is None
            values = await transport.read_holding_registers(1, p.REG_MODE, 1)
            assert values == (1,)
        finally:
            await transport.close()
        assert server.write_codes == []


async def test_unknown_unit_id_fails_bounded_and_other_unit_recovers() -> None:
    # Server behaviour for an absent unit differs by pymodbus version: newer servers
    # answer with a Modbus exception (gateway path unavailable), older ones stay
    # silent. Either way the failure must be bounded and must not break unit 1.
    async with local_server({1: REAL_PROFILE[1]}) as server:
        clock = VirtualClock()
        _, transport = make_transport(server.port, clock, request_timeout=0.05)
        try:
            with pytest.raises((WtcModbusExceptionError, WtcTimeoutError)):
                await transport.read_holding_registers(2, p.REG_MODE, 1)
            backoff_until = transport.stats.backoff_until
            if backoff_until is not None:  # timeout path: wait out the backoff
                clock.now = backoff_until
            assert await transport.read_holding_registers(1, p.REG_MODE, 1) == (1,)
        finally:
            await transport.close()
        assert server.write_codes == []


async def test_server_loss_becomes_connection_error_then_backoff() -> None:
    clock = VirtualClock()
    async with local_server() as server:
        # Short timeout: depending on the pymodbus version, shutdown either drops the
        # open connection (connection error) or leaves it half-open (timeout).
        backend, transport = make_transport(server.port, clock, request_timeout=0.05)
        assert await transport.read_holding_registers(1, p.REG_MODE, 1) == (1,)
        await server.stop()
        with pytest.raises((WtcConnectionError, WtcTimeoutError)):
            await transport.read_holding_registers(1, p.REG_MODE, 1)
        assert not backend.connected
        assert transport.stats.backoff_until is not None
        # While in backoff the transport fails fast without touching the network.
        with pytest.raises(WtcBackoffError):
            await transport.read_holding_registers(1, p.REG_MODE, 1)
        await transport.close()
        assert server.write_codes == []


async def test_connect_to_closed_port_fails_cleanly() -> None:
    clock = VirtualClock()
    backend, transport = make_transport(_free_port(), clock)
    with pytest.raises(WtcConnectionError):
        await transport.read_holding_registers(1, p.REG_MODE, 1)
    assert not backend.connected
    assert transport.stats.backoff_until is not None
    await transport.close()


async def test_wheel_write_is_exactly_one_function_06_and_reads_back() -> None:
    from custom_components.wavin_wtc_net.write_backend import PymodbusWriteBackend
    from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

    async with local_server() as server:
        clock = VirtualClock()
        backend = PymodbusWriteBackend(HOST, server.port, timeout=2.0)
        transport = WritableGatewayTransport(
            backend, TransportSettings(request_timeout=2.0, connect_timeout=2.0), clock
        )
        try:
            address = p.zone_wheel_code_register(2)
            before = await transport.read_holding_registers(2, address, 1)
            await transport.write_wheel_code(2, address, 6)
            after = await transport.read_holding_registers(2, address, 1)
            assert before != after and after == (6,)
            # other zone registers of the same unit are untouched
            neighbours = await transport.read_holding_registers(2, p.zone_wheel_code_register(1), 3)
            assert neighbours[0] == 6 and neighbours[2] == 5  # zones 1 and 3 untouched
        finally:
            await transport.close()
        assert server.write_codes == [6]  # exactly one write, function code 06


async def test_lock_write_is_exactly_one_function_05_and_reads_back() -> None:
    from custom_components.wavin_wtc_net.write_backend import PymodbusWriteBackend
    from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

    async with local_server() as server:
        clock = VirtualClock()
        backend = PymodbusWriteBackend(HOST, server.port, timeout=2.0)
        transport = WritableGatewayTransport(
            backend, TransportSettings(request_timeout=2.0, connect_timeout=2.0), clock
        )
        try:
            address = p.zone_lock_command_coil(3)
            assert await transport.read_coils(1, address, 1) == (False,)
            await transport.write_lock_coil(1, address, True)
            assert await transport.read_coils(1, address - 1, 3) == (False, True, False)
        finally:
            await transport.close()
        assert server.write_codes == [5]
