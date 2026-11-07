"""PyModbus adapter with the single register write used for wheel commands.

Only constructed when write mode is enabled. Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from typing import Any

from .errors import WtcModbusExceptionError
from .pymodbus_backend import PymodbusBackend, _unit_keyword


class PymodbusWheelBackend(PymodbusBackend):
    async def write_register(self, unit_id: int, address: int, value: int) -> None:
        from pymodbus.exceptions import ModbusException

        if self._client is None:
            raise ConnectionError("not connected")
        method = self._client.write_register
        kwargs: dict[str, Any] = {_unit_keyword(method): unit_id}
        try:
            response = await method(address, value, **kwargs)
        except ModbusException as err:
            raise ConnectionError(str(err)) from err
        if response.isError():
            raise WtcModbusExceptionError(getattr(response, "exception_code", None))
