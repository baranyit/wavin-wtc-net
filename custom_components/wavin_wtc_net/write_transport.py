"""Write-capable transport: a separate class so the read path has no route to a write.

``GatewayTransport`` (read-only) is what setup, polling, validation and diagnostics use. Only when
the user enabled write mode does the integration construct this subclass. It shares the same lock,
one-request-per-second limiter, timeout and backoff as reads.

It can perform exactly three kinds of write, each with a hard allow-list:

* one wheel-code register (4604..4610, values 0..12), Modbus function 06;
* one zone lock coil (4656..4662), Modbus function 05;
* one zone on/off or comfort/economy control coil (offsets 0 and 1 of 4604+8(z-1)), function 05.

There is no multi-register or multi-coil write and no other address (in particular never the
global coils 4127-4129, never the zone heat/cool coil and never a setpoint register).

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from .errors import (
    WtcBackoffError,
    WtcConnectionError,
    WtcModbusExceptionError,
    WtcRequestRejectedError,
    WtcTimeoutError,
)
from .protocol import (
    MAX_ZONES,
    zone_lock_command_coil,
    zone_power_control_coil,
    zone_preset_control_coil,
    zone_wheel_code_register,
)
from .transport import Clock, GatewayTransport, ModbusReadBackend, TransportSettings

WHEEL_CODE_MIN = 0
WHEEL_CODE_MAX = 12
WRITABLE_REGISTERS = frozenset(zone_wheel_code_register(z) for z in range(1, MAX_ZONES + 1))
WRITABLE_COILS = frozenset(zone_lock_command_coil(z) for z in range(1, MAX_ZONES + 1))
WRITABLE_MODE_COILS = frozenset(
    fn(z)
    for z in range(1, MAX_ZONES + 1)
    for fn in (zone_power_control_coil, zone_preset_control_coil)
)


class ModbusWriteBackend(ModbusReadBackend, Protocol):
    """Read backend plus the two single-item writes used by the command layer."""

    async def write_register(self, unit_id: int, address: int, value: int) -> None: ...

    async def write_coil(self, unit_id: int, address: int, value: bool) -> None: ...


class WritableGatewayTransport(GatewayTransport):
    def __init__(
        self,
        backend: ModbusWriteBackend,
        settings: TransportSettings | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(backend, settings, clock)
        self._write_backend: ModbusWriteBackend = backend

    async def write_wheel_code(self, unit_id: int, address: int, code: int) -> None:
        """Write one wheel-code register. Raises ``WtcRequestRejectedError`` before any traffic."""
        if address not in WRITABLE_REGISTERS:
            raise WtcRequestRejectedError(f"register {address} is not a writable wheel register")
        if isinstance(code, bool) or not WHEEL_CODE_MIN <= code <= WHEEL_CODE_MAX:
            raise WtcRequestRejectedError(
                f"wheel code {code!r} outside {WHEEL_CODE_MIN}..{WHEEL_CODE_MAX}"
            )
        wire = self._validate_request(unit_id, address, 1)
        await self._send_single(
            f"register {address}", lambda: self._write_backend.write_register(unit_id, wire, code)
        )

    async def write_lock_coil(self, unit_id: int, address: int, locked: bool) -> None:
        """Write one zone lock coil. Raises ``WtcRequestRejectedError`` before any traffic."""
        if address not in WRITABLE_COILS:
            raise WtcRequestRejectedError(f"coil {address} is not a writable lock coil")
        if not isinstance(locked, bool):
            raise WtcRequestRejectedError("lock value must be a bool")
        wire = self._validate_request(unit_id, address, 1)
        await self._send_single(
            f"coil {address}", lambda: self._write_backend.write_coil(unit_id, wire, locked)
        )

    async def write_zone_mode_coil(self, unit_id: int, address: int, value: bool) -> None:
        """Write a zone on/off or comfort/economy control coil (never heat/cool, never global)."""
        if address not in WRITABLE_MODE_COILS:
            raise WtcRequestRejectedError(f"coil {address} is not a writable zone mode coil")
        if not isinstance(value, bool):
            raise WtcRequestRejectedError("zone mode value must be a bool")
        wire = self._validate_request(unit_id, address, 1)
        await self._send_single(
            f"coil {address}", lambda: self._write_backend.write_coil(unit_id, wire, value)
        )

    async def _send_single(self, what: str, call: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            now = self._clock.monotonic()
            if self._backoff_until is not None and now < self._backoff_until:
                raise WtcBackoffError(self._backoff_until - now)
            if not self._backend.connected:
                await self._connect()
            await self._wait_for_slot()
            started = self._clock.monotonic()
            self._last_request_start = started
            self._requests_started += 1
            try:
                async with asyncio.timeout(self._settings.request_timeout):
                    await call()
            except WtcModbusExceptionError:
                self._failures += 1
                raise
            except TimeoutError as err:
                await self._connection_failed()
                raise WtcTimeoutError(f"write timed out ({what})") from err
            except Exception as err:
                await self._connection_failed()
                raise WtcConnectionError(f"write failed: {err!r}") from err
            self._consecutive_connection_failures = 0
            self._backoff_until = None
            self._last_success = self._clock.monotonic()
            self._last_latency = self._last_success - started
