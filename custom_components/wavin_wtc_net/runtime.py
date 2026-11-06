"""Build the transport + read client for one endpoint (shared by setup, flows, tests).

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping

from .client import DEFAULT_MAX_GAP, UnitConfig, WtcReadClient
from .protocol import REG_MODE
from .pymodbus_backend import PymodbusBackend
from .transport import Clock, GatewayTransport, ModbusReadBackend, TransportSettings


def unit_configs(units: Mapping[int, int]) -> list[UnitConfig]:
    return [UnitConfig(unit, tuple(range(1, zones + 1))) for unit, zones in sorted(units.items())]


def build_client(
    host: str,
    port: int,
    units: Mapping[int, int],
    *,
    max_gap: int = DEFAULT_MAX_GAP,
    settings: TransportSettings | None = None,
    backend: ModbusReadBackend | None = None,
    clock: Clock | None = None,
) -> tuple[GatewayTransport, WtcReadClient]:
    """One backend, one transport/queue, one client for all unit IDs of the endpoint.

    ``backend`` and ``clock`` are injection points for tests (virtual time, mock device).
    """
    settings = settings or TransportSettings()
    backend = backend or PymodbusBackend(host, port, timeout=settings.request_timeout)
    transport = GatewayTransport(backend, settings, clock)
    client = WtcReadClient(transport, unit_configs(units), max_gap=max_gap, clock=clock)
    return transport, client


async def probe_endpoint(host: str, port: int, unit_ids: list[int]) -> None:
    """Read-only connectivity check: one register (controller mode) per unit, paced.

    Raises the integration's own errors. Never writes.
    """
    transport, _ = build_client(host, port, {unit: 1 for unit in unit_ids})
    try:
        for unit in unit_ids:
            await transport.read_holding_registers(unit, REG_MODE, 1)
    finally:
        await transport.close()
