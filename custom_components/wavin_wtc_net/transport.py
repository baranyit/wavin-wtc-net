"""Serialized, rate-limited, read-only Modbus transport for one TCP endpoint.

One ``GatewayTransport`` owns one backend connection and one queue shared by all
unit IDs on that endpoint. Guarantees:

* requests are strictly serialized (one in flight at a time);
* at most one request *starts* per ``min_request_interval`` (default 1 s);
* a request never exceeds the hard caps (default 15 registers / 16 coils) and an
  oversized request is rejected locally, before any traffic;
* bounded per-request timeout and bounded exponential reconnect backoff; while in
  backoff, requests fail fast without touching the network.

This milestone is read-only: the transport has no write API at all.
Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .errors import (
    WtcBackoffError,
    WtcConnectionError,
    WtcMalformedResponseError,
    WtcModbusExceptionError,
    WtcRequestRejectedError,
    WtcTimeoutError,
)
from .protocol import Table, validate_unit_id

#: Hard safety ceilings for advanced settings. Defaults are far below these.
ABSOLUTE_MIN_REQUEST_INTERVAL = 0.5
ABSOLUTE_MAX_REGISTERS = 30
ABSOLUTE_MAX_COILS = 64
MAX_PROTOCOL_ADDRESS = 0xFFFF


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class AsyncioClock:
    """Production clock based on the running event loop."""

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ModbusReadBackend(Protocol):
    """Minimal read-only backend contract.

    Implementations raise ``WtcModbusExceptionError`` for Modbus exception responses
    and ``OSError``/``ConnectionError``/``TimeoutError`` for connection problems.
    They must not retry or reconnect on their own.
    """

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def read_holding_registers(
        self, unit_id: int, address: int, count: int
    ) -> Sequence[int]: ...

    async def read_coils(self, unit_id: int, address: int, count: int) -> Sequence[bool]: ...


@dataclass(frozen=True, slots=True)
class TransportSettings:
    min_request_interval: float = 1.0
    max_registers_per_request: int = 15
    max_coils_per_request: int = 16
    request_timeout: float = 5.0
    connect_timeout: float = 5.0
    backoff_initial: float = 5.0
    backoff_max: float = 300.0
    address_offset: int = 0

    def __post_init__(self) -> None:
        if self.min_request_interval < ABSOLUTE_MIN_REQUEST_INTERVAL:
            raise ValueError(f"min_request_interval must be >= {ABSOLUTE_MIN_REQUEST_INTERVAL} s")
        if not 1 <= self.max_registers_per_request <= ABSOLUTE_MAX_REGISTERS:
            raise ValueError(f"max_registers_per_request must be 1..{ABSOLUTE_MAX_REGISTERS}")
        if not 1 <= self.max_coils_per_request <= ABSOLUTE_MAX_COILS:
            raise ValueError(f"max_coils_per_request must be 1..{ABSOLUTE_MAX_COILS}")
        if self.request_timeout <= 0 or self.connect_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not 0 < self.backoff_initial <= self.backoff_max:
            raise ValueError("backoff must satisfy 0 < initial <= max")
        if not -MAX_PROTOCOL_ADDRESS <= self.address_offset <= MAX_PROTOCOL_ADDRESS:
            raise ValueError("address_offset out of range")

    def cap_for(self, table: Table) -> int:
        if table is Table.HOLDING_REGISTER:
            return self.max_registers_per_request
        return self.max_coils_per_request


@dataclass(frozen=True, slots=True)
class TransportStats:
    connected: bool
    requests_started: int
    failures: int
    consecutive_connection_failures: int
    last_success: float | None
    last_latency: float | None
    backoff_until: float | None


class GatewayTransport:
    def __init__(
        self,
        backend: ModbusReadBackend,
        settings: TransportSettings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._backend = backend
        self._settings = settings or TransportSettings()
        self._clock: Clock = clock or AsyncioClock()
        self._lock = asyncio.Lock()
        self._last_request_start: float | None = None
        self._backoff_until: float | None = None
        self._consecutive_connection_failures = 0
        self._requests_started = 0
        self._failures = 0
        self._last_success: float | None = None
        self._last_latency: float | None = None
        self._connection_generation = 0

    @property
    def connection_generation(self) -> int:
        """Incremented on every successful (re)connect."""
        return self._connection_generation

    @property
    def settings(self) -> TransportSettings:
        return self._settings

    @property
    def stats(self) -> TransportStats:
        return TransportStats(
            connected=self._backend.connected,
            requests_started=self._requests_started,
            failures=self._failures,
            consecutive_connection_failures=self._consecutive_connection_failures,
            last_success=self._last_success,
            last_latency=self._last_latency,
            backoff_until=self._backoff_until,
        )

    async def read_holding_registers(
        self, unit_id: int, address: int, count: int
    ) -> tuple[int, ...]:
        values = await self._execute(Table.HOLDING_REGISTER, unit_id, address, count)
        result: list[int] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF:
                self._failures += 1
                raise WtcMalformedResponseError(f"invalid register value {value!r}")
            result.append(value)
        return tuple(result)

    async def read_coils(self, unit_id: int, address: int, count: int) -> tuple[bool, ...]:
        values = await self._execute(Table.COIL, unit_id, address, count)
        result: list[bool] = []
        for value in values:
            if isinstance(value, bool):
                result.append(value)
            elif isinstance(value, int) and value in (0, 1):
                result.append(bool(value))
            else:
                self._failures += 1
                raise WtcMalformedResponseError(f"invalid coil value {value!r}")
        return tuple(result)

    async def close(self) -> None:
        async with self._lock:
            await self._safe_close()

    # -- internals ------------------------------------------------------------------

    def _validate(self, table: Table, unit_id: int, address: int, count: int) -> int:
        try:
            validate_unit_id(unit_id)
        except ValueError as err:
            raise WtcRequestRejectedError(str(err)) from err
        cap = self._settings.cap_for(table)
        if not 1 <= count <= cap:
            raise WtcRequestRejectedError(f"{table.value} count {count} outside 1..{cap}")
        wire_address = address + self._settings.address_offset
        if wire_address < 0 or wire_address + count - 1 > MAX_PROTOCOL_ADDRESS:
            raise WtcRequestRejectedError(f"address range {wire_address}+{count} out of range")
        return wire_address

    async def _execute(
        self, table: Table, unit_id: int, address: int, count: int
    ) -> Sequence[object]:
        wire_address = self._validate(table, unit_id, address, count)
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
                    if table is Table.HOLDING_REGISTER:
                        values: Sequence[object] = await self._backend.read_holding_registers(
                            unit_id, wire_address, count
                        )
                    else:
                        values = await self._backend.read_coils(unit_id, wire_address, count)
            except WtcModbusExceptionError:
                self._failures += 1
                raise
            except TimeoutError as err:
                await self._connection_failed()
                raise WtcTimeoutError(
                    f"request timed out ({table.value} {address}+{count})"
                ) from err
            except Exception as err:
                await self._connection_failed()
                raise WtcConnectionError(f"request failed: {err!r}") from err
            if len(values) != count:
                self._failures += 1
                raise WtcMalformedResponseError(
                    f"expected {count} values, got {len(values)} ({table.value} {address})"
                )
            self._consecutive_connection_failures = 0
            self._backoff_until = None
            self._last_success = self._clock.monotonic()
            self._last_latency = self._last_success - started
            return values

    async def _wait_for_slot(self) -> None:
        if self._last_request_start is None:
            return
        wait = self._last_request_start + self._settings.min_request_interval
        wait -= self._clock.monotonic()
        if wait > 0:
            await self._clock.sleep(wait)

    async def _connect(self) -> None:
        try:
            async with asyncio.timeout(self._settings.connect_timeout):
                await self._backend.connect()
        except Exception as err:
            await self._connection_failed()
            raise WtcConnectionError(f"connect failed: {err!r}") from err
        if not self._backend.connected:
            await self._connection_failed()
            raise WtcConnectionError("connect failed: backend reports not connected")
        self._connection_generation += 1

    async def _connection_failed(self) -> None:
        self._failures += 1
        self._consecutive_connection_failures += 1
        exponent = min(self._consecutive_connection_failures - 1, 30)
        delay = min(self._settings.backoff_initial * (2**exponent), self._settings.backoff_max)
        self._backoff_until = self._clock.monotonic() + delay
        await self._safe_close()

    async def _safe_close(self) -> None:
        with contextlib.suppress(Exception):  # closing must never raise
            await self._backend.close()
