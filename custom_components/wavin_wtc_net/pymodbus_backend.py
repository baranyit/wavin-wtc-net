"""PyModbus adapter implementing the read-only backend contract.

PyModbus' own retries and automatic reconnect are disabled: retry, pacing and
backoff policy belong to ``GatewayTransport`` only. Supports both the
``slave=`` (3.8/3.9) and ``device_id=`` (3.10+) keyword of the read calls.
Pure Python: no Home Assistant imports. ``pymodbus`` is imported lazily so the
protocol core can be tested without it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any

from .errors import WtcModbusExceptionError


def _unit_keyword(method: Callable[..., Any]) -> str:
    parameters = inspect.signature(method).parameters
    if "device_id" in parameters:
        return "device_id"
    if "slave" in parameters:
        return "slave"
    raise TypeError("unsupported pymodbus read signature (no device_id/slave keyword)")


class PymodbusBackend:
    def __init__(self, host: str, port: int = 502, *, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._client: Any = None

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(self._client.connected)

    async def connect(self) -> None:
        from pymodbus.client import AsyncModbusTcpClient

        await self.close()
        client = AsyncModbusTcpClient(
            self._host,
            port=self._port,
            timeout=self._timeout,
            retries=0,
            reconnect_delay=0,
        )
        self._client = client
        if not await client.connect():
            await self.close()
            raise ConnectionError(f"cannot connect to {self._host}:{self._port}")

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            result = client.close()
            if inspect.isawaitable(result):
                await result

    async def read_holding_registers(self, unit_id: int, address: int, count: int) -> Sequence[int]:
        response = await self._call("read_holding_registers", unit_id, address, count)
        return list(response.registers)

    async def read_coils(self, unit_id: int, address: int, count: int) -> Sequence[bool]:
        response = await self._call("read_coils", unit_id, address, count)
        # Coil responses are padded to whole bytes; keep only what was requested.
        return [bool(bit) for bit in list(response.bits)[:count]]

    async def _call(self, name: str, unit_id: int, address: int, count: int) -> Any:
        from pymodbus.exceptions import ModbusException

        if self._client is None:
            raise ConnectionError("not connected")
        method = getattr(self._client, name)
        kwargs: dict[str, Any] = {"count": count, _unit_keyword(method): unit_id}
        try:
            response = await method(address, **kwargs)
        except ModbusException as err:
            raise ConnectionError(str(err)) from err
        if response.isError():
            raise WtcModbusExceptionError(getattr(response, "exception_code", None))
        return response
