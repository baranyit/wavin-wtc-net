"""Write-capable transport: a separate class so the read path has no route to a write.

``GatewayTransport`` (read-only) is what setup, polling, validation and diagnostics use. Only when
the user enabled write mode does the integration construct this subclass. It shares the same lock,
one-request-per-second limiter, timeout and backoff as reads (docs/04-architecture.md).

The only write it can ever perform is *one wheel-code register* (4604..4610, values 0..12) with
Modbus function 06. There is no coil write, no multi-register write, and no other address.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .errors import (
    WtcBackoffError,
    WtcConnectionError,
    WtcModbusExceptionError,
    WtcRequestRejectedError,
    WtcTimeoutError,
)
from .protocol import MAX_ZONES, zone_wheel_code_register
from .transport import Clock, GatewayTransport, ModbusReadBackend, TransportSettings

WHEEL_CODE_MIN = 0
WHEEL_CODE_MAX = 12
WRITABLE_REGISTERS = frozenset(zone_wheel_code_register(z) for z in range(1, MAX_ZONES + 1))


class ModbusWheelBackend(ModbusReadBackend, Protocol):
    """Read backend plus the single register write used for wheel commands."""

    async def write_register(self, unit_id: int, address: int, value: int) -> None: ...


class WritableGatewayTransport(GatewayTransport):
    def __init__(
        self,
        backend: ModbusWheelBackend,
        settings: TransportSettings | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(backend, settings, clock)
        self._wheel_backend: ModbusWheelBackend = backend

    async def write_wheel_code(self, unit_id: int, address: int, code: int) -> None:
        """Write one wheel-code register. Raises ``WtcRequestRejectedError`` before any traffic."""
        if address not in WRITABLE_REGISTERS:
            raise WtcRequestRejectedError(f"register {address} is not a writable wheel register")
        if isinstance(code, bool) or not WHEEL_CODE_MIN <= code <= WHEEL_CODE_MAX:
            raise WtcRequestRejectedError(
                f"wheel code {code!r} outside {WHEEL_CODE_MIN}..{WHEEL_CODE_MAX}"
            )
        wire_address = self._validate_wire(unit_id, address)
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
                    await self._wheel_backend.write_register(unit_id, wire_address, code)
            except WtcModbusExceptionError:
                self._failures += 1
                raise
            except TimeoutError as err:
                await self._connection_failed()
                raise WtcTimeoutError(f"write timed out (register {address})") from err
            except Exception as err:
                await self._connection_failed()
                raise WtcConnectionError(f"write failed: {err!r}") from err
            self._consecutive_connection_failures = 0
            self._backoff_until = None
            self._last_success = self._clock.monotonic()
            self._last_latency = self._last_success - started

    def _validate_wire(self, unit_id: int, address: int) -> int:
        # Same unit/address validation as a one-register read.
        return self._validate_request(unit_id, address, 1)
