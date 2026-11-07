"""PyModbus adapter with the single-item writes used by the command layer.

Only constructed when write mode is enabled. Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from typing import Any

from .errors import WtcModbusExceptionError
from .pymodbus_backend import PymodbusBackend, _unit_keyword


class PymodbusWriteBackend(PymodbusBackend):
    async def write_register(self, unit_id: int, address: int, value: int) -> None:
        if self._client is None:
            raise ConnectionError("not connected")
        await self._write(self._client.write_register, unit_id, address, value)

    async def write_coil(self, unit_id: int, address: int, value: bool) -> None:
        if self._client is None:
            raise ConnectionError("not connected")
        await self._write(self._client.write_coil, unit_id, address, value)

    @staticmethod
    async def _write(method: Any, unit_id: int, address: int, value: Any) -> None:
        from pymodbus.exceptions import ModbusException

        kwargs: dict[str, Any] = {_unit_keyword(method): unit_id}
        try:
            response = await method(address, value, **kwargs)
        except ModbusException as err:
            raise ConnectionError(str(err)) from err
        if response.isError():
            raise WtcModbusExceptionError(getattr(response, "exception_code", None))
