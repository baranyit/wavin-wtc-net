"""Adapter tests against a fake ``pymodbus`` module (both keyword generations).

Real-library integration is in ``test_pymodbus_integration.py`` (localhost server).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from custom_components.wavin_wtc_net.errors import WtcModbusExceptionError
from custom_components.wavin_wtc_net.pymodbus_backend import PymodbusBackend


class FakeModbusException(Exception):
    pass


class FakeResponse:
    def __init__(
        self,
        registers: list[int] | None = None,
        bits: list[bool] | None = None,
        error_code: int | None = None,
    ) -> None:
        self.registers = registers or []
        self.bits = bits or []
        self.exception_code = error_code

    def isError(self) -> bool:
        return self.exception_code is not None


def _make_client_class(keyword: str) -> type:
    class FakeClient:
        instances: ClassVar[list[FakeClient]] = []

        def __init__(self, host: str, **kwargs: Any) -> None:
            self.host, self.kwargs = host, kwargs
            self.connected = False
            self.calls: list[tuple[str, int, dict[str, Any]]] = []
            self.next_error: int | None = None
            self.raise_io = False
            FakeClient.instances.append(self)

        async def connect(self) -> bool:
            self.connected = True
            return True

        def close(self) -> None:
            self.connected = False

    async def read_holding_registers(
        self: Any, address: int, *, count: int = 1, **kw: Any
    ) -> FakeResponse:
        self.calls.append(("read_holding_registers", address, {"count": count, **kw}))
        if self.raise_io:
            raise FakeModbusException("io")
        if self.next_error is not None:
            return FakeResponse(error_code=self.next_error)
        return FakeResponse(registers=list(range(count)))

    async def read_coils(self: Any, address: int, *, count: int = 1, **kw: Any) -> FakeResponse:
        self.calls.append(("read_coils", address, {"count": count, **kw}))
        return FakeResponse(bits=[True] * count + [False] * (8 - count % 8))

    # Build real keyword-only signatures so inspect.signature sees them.
    namespace: dict[str, Any] = {}
    exec(
        f"async def rhr(self, address, *, count=1, {keyword}=1):\n"
        f"    return await _rhr(self, address, count=count, {keyword}={keyword})\n"
        f"async def rc(self, address, *, count=1, {keyword}=1):\n"
        f"    return await _rc(self, address, count=count, {keyword}={keyword})\n",
        {"_rhr": read_holding_registers, "_rc": read_coils},
        namespace,
    )
    FakeClient.read_holding_registers = namespace["rhr"]  # type: ignore[attr-defined]
    FakeClient.read_coils = namespace["rc"]  # type: ignore[attr-defined]
    return FakeClient


@pytest.fixture(params=["slave", "device_id"])
def fake_pymodbus(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, type]]:
    keyword: str = request.param
    client_class = _make_client_class(keyword)
    root = types.ModuleType("pymodbus")
    client_mod = types.ModuleType("pymodbus.client")
    exc_mod = types.ModuleType("pymodbus.exceptions")
    client_mod.AsyncModbusTcpClient = client_class  # type: ignore[attr-defined]
    exc_mod.ModbusException = FakeModbusException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymodbus", root)
    monkeypatch.setitem(sys.modules, "pymodbus.client", client_mod)
    monkeypatch.setitem(sys.modules, "pymodbus.exceptions", exc_mod)
    yield keyword, client_class


async def test_adapter_disables_library_retries_and_maps_unit_keyword(
    fake_pymodbus: tuple[str, Any],
) -> None:
    keyword, client_class = fake_pymodbus
    backend = PymodbusBackend("192.0.2.10", 502, timeout=3)
    await backend.connect()
    assert backend.connected
    client = client_class.instances[-1]
    assert client.kwargs["retries"] == 0 and client.kwargs["reconnect_delay"] == 0
    assert await backend.read_holding_registers(2, 4121, 3) == [0, 1, 2]
    assert await backend.read_coils(1, 4120, 5) == [True] * 5
    assert client.calls[0] == ("read_holding_registers", 4121, {"count": 3, keyword: 2})
    await backend.close()
    assert not backend.connected


async def test_adapter_maps_errors(fake_pymodbus: tuple[str, Any]) -> None:
    _, client_class = fake_pymodbus
    backend = PymodbusBackend("192.0.2.10")
    await backend.connect()
    client = client_class.instances[-1]
    client.next_error = 2
    with pytest.raises(WtcModbusExceptionError):
        await backend.read_holding_registers(1, 4121, 1)
    client.next_error = None
    client.raise_io = True
    with pytest.raises(ConnectionError):
        await backend.read_holding_registers(1, 4121, 1)
