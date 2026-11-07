"""In-process WTC-NET simulator and test doubles.

* ``VirtualClock``: deterministic monotonic time; ``sleep`` advances time instantly.
* ``MockWtcUnit``: register/coil memory of one controller, filled with plausible data.
* ``MockWtcBackend``: implements the read backend contract against several units on
  one endpoint, records every request (and any write attempt), tracks concurrency,
  and injects faults (timeouts, disconnects, exception responses, malformed data,
  outages, offline units).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from custom_components.wavin_wtc_net import protocol as p

#: Real installation profile (test fixture only, not a default).
REAL_PROFILE: dict[int, dict[int, str]] = {
    1: {1: "Konyha", 2: "Nappali", 3: "Balu", 4: "Gábor", 5: "Luca", 6: "Fürdő"},
    2: {1: "Garázs", 2: "Dolgozó", 3: "Háló", 4: "Zuhany"},
}

READABLE_REGISTER_RANGES: tuple[range, ...] = (
    range(4100, 4251),
    range(4500, 4511),
    range(4600, 4621),
    range(4700, 4711),
)
READABLE_COIL_RANGES: tuple[range, ...] = (range(4100, 4251), range(4600, 4671))

ILLEGAL_DATA_ADDRESS = 2
GATEWAY_TARGET_FAILED = 11


class VirtualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.now += seconds
        await asyncio.sleep(0)


def tenths(value: float) -> int:
    return round(value * 10) & 0xFFFF


@dataclass
class MockWtcUnit:
    unit_id: int
    zones: tuple[int, ...]
    registers: dict[int, int] = field(default_factory=dict)
    coils: dict[int, bool] = field(default_factory=dict)

    @classmethod
    def build(cls, unit_id: int, zones: Iterable[int]) -> MockWtcUnit:
        unit = cls(unit_id, tuple(sorted(zones)))
        r, c = unit.registers, unit.coils
        r[p.REG_WATER_TEMPERATURE] = tenths(31.5)
        r[p.REG_CONTROLLER_HUMIDITY] = tenths(45.2)
        r[p.REG_CONTROLLER_TEMPERATURE] = tenths(21.8)
        r[p.REG_PI_OUTPUT] = tenths(37.5)
        r[p.REG_CONTROLLER_DEW_POINT] = tenths(9.1)
        r[p.REG_PROGRAM_CODE] = 3
        r[p.REG_MODE] = 1
        r[p.REG_FIRMWARE] = 214
        r[p.REG_SERIAL_LOW] = 0x5678 + unit_id
        r[p.REG_SERIAL_HIGH] = 0x0012
        c[p.COIL_HEATING_OUTPUT] = True
        c[p.COIL_GLOBAL_ON_OFF] = True
        c[p.COIL_GLOBAL_COMFORT_ECONOMY] = True
        c[p.COIL_GLOBAL_HEAT_COOL] = False
        for zone in unit.zones:
            base = p.zone_register_base(zone)
            r[base] = tenths(20.0 + zone * 0.5 + unit_id * 0.1)
            r[base + 1] = tenths(40.0 + zone)
            r[base + 2] = tenths(8.0 + zone * 0.2)
            r[base + 3] = tenths(-0.5 if zone % 2 else 0.5)
            r[base + 4] = 1000 + zone
            r[base + 5] = 300 + zone
            sp = p.zone_setpoint_base(zone)
            r[sp], r[sp + 1], r[sp + 2] = tenths(26.0), tenths(22.0), tenths(19.0)
            r[p.zone_economy_cool_register(zone)] = tenths(28.0)
            code = 6 + (zone % 3) - 1
            r[p.zone_wheel_code_register(zone)] = code
            word_address = p.zone_packed_wheel_register(zone)
            word = r.get(word_address, 0)
            word = (word & 0xFF00) | code if zone % 2 else (word & 0x00FF) | (code << 8)
            r[word_address] = word
            status = p.zone_status_coil_base(zone)
            c[status + 1] = zone % 2 == 1  # valve open on odd zones
            c[status + 4] = True
            c[status + 5] = True
            c[status + 6] = False
            c[status + 7] = True  # live
            control = p.zone_control_coil_base(zone)
            c[control], c[control + 1] = True, True
        return unit


@dataclass
class Fault:
    kind: str  # timeout | disconnect | exception | short | garbage
    unit_id: int | None = None
    table: p.Table | None = None
    address: int | None = None
    remaining: int = 1
    exception_code: int = ILLEGAL_DATA_ADDRESS

    def matches(self, unit_id: int, table: p.Table, address: int, count: int) -> bool:
        if self.remaining <= 0:
            return False
        if self.unit_id is not None and self.unit_id != unit_id:
            return False
        if self.table is not None and self.table is not table:
            return False
        return self.address is None or address <= self.address < address + count


@dataclass(frozen=True)
class RequestRecord:
    start: float
    end: float
    op: str
    unit_id: int
    address: int
    count: int
    outcome: str


class MockWtcBackend:
    def __init__(
        self,
        clock: VirtualClock,
        units: Sequence[MockWtcUnit],
        *,
        latency: float = 0.04,
    ) -> None:
        self.clock = clock
        self.units = {unit.unit_id: unit for unit in units}
        self.latency = latency
        self.records: list[RequestRecord] = []
        self.writes: list[tuple[str, int, int, object]] = []
        self.faults: list[Fault] = []
        self.outages: list[tuple[float, float]] = []
        self.offline_units: set[int] = set()
        self.unreadable_registers: set[int] = set()
        self.connect_attempts = 0
        self.ignore_writes = False  # device accepts the write but the read-back never changes
        self.max_in_flight = 0
        self._in_flight = 0
        self._connected = False

    @classmethod
    def for_profile(
        cls, clock: VirtualClock, profile: dict[int, dict[int, str]] = REAL_PROFILE
    ) -> MockWtcBackend:
        return cls(clock, [MockWtcUnit.build(uid, zones) for uid, zones in profile.items()])

    # -- backend contract ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_attempts += 1
        await self.clock.sleep(0.01)
        if self.in_outage():
            raise ConnectionRefusedError("mock outage")
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def read_holding_registers(self, unit_id: int, address: int, count: int) -> Sequence[int]:
        return await self._read(
            "read_holding_registers", p.Table.HOLDING_REGISTER, unit_id, address, count
        )  # type: ignore[return-value]

    async def read_coils(self, unit_id: int, address: int, count: int) -> Sequence[bool]:
        return await self._read("read_coils", p.Table.COIL, unit_id, address, count)  # type: ignore[return-value]

    # -- writes: recorded by the spy; applied like a device unless ``ignore_writes`` -----------

    async def write_register(self, unit_id: int, address: int, value: int) -> None:
        start = self.clock.now
        outcome = "ok"
        try:
            if not self._connected:
                outcome = "not_connected"
                raise ConnectionError("mock: not connected")
            if self.in_outage():
                self._connected = False
                outcome = "reset"
                raise ConnectionResetError("mock outage")
            await self.clock.sleep(self.latency)
            self.writes.append(("write_register", unit_id, address, value))
            fault = next(
                (
                    f
                    for f in self.faults
                    if f.matches(unit_id, p.Table.HOLDING_REGISTER, address, 1)
                ),
                None,
            )
            if fault is not None:
                fault.remaining -= 1
                outcome = fault.kind
                await self._apply_fault(fault, p.Table.HOLDING_REGISTER, 1)
                return
            if unit_id not in self.units or unit_id in self.offline_units:
                outcome = "offline"
                raise p_exception(GATEWAY_TARGET_FAILED)
            if not self.ignore_writes:
                self.units[unit_id].registers[address] = value
        finally:
            self.records.append(
                RequestRecord(start, self.clock.now, "write_register", unit_id, address, 1, outcome)
            )

    async def write_coil(self, unit_id: int, address: int, value: bool) -> None:
        self.writes.append(("write_coil", unit_id, address, value))

    # -- helpers --------------------------------------------------------------------------

    def in_outage(self) -> bool:
        return any(start <= self.clock.now < end for start, end in self.outages)

    @property
    def reads(self) -> list[RequestRecord]:
        return [r for r in self.records if r.op.startswith("read")]

    async def _read(
        self, op: str, table: p.Table, unit_id: int, address: int, count: int
    ) -> Sequence[object]:
        start = self.clock.now
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        outcome = "ok"
        try:
            if not self._connected:
                outcome = "not_connected"
                raise ConnectionError("mock: not connected")
            if self.in_outage():
                self._connected = False
                outcome = "reset"
                raise ConnectionResetError("mock outage")
            await self.clock.sleep(self.latency)
            fault = next(
                (f for f in self.faults if f.matches(unit_id, table, address, count)), None
            )
            if fault is not None:
                fault.remaining -= 1
                outcome = fault.kind
                return await self._apply_fault(fault, table, count)
            if unit_id in self.offline_units or unit_id not in self.units:
                outcome = "offline"
                raise p_exception(GATEWAY_TARGET_FAILED)
            unit = self.units[unit_id]
            ranges = (
                READABLE_REGISTER_RANGES
                if table is p.Table.HOLDING_REGISTER
                else (READABLE_COIL_RANGES)
            )
            addresses = range(address, address + count)
            blocked = table is p.Table.HOLDING_REGISTER and any(
                a in self.unreadable_registers for a in addresses
            )
            if blocked or not any(addresses[0] in rg and addresses[-1] in rg for rg in ranges):
                outcome = "illegal_address"
                raise p_exception(ILLEGAL_DATA_ADDRESS)
            if table is p.Table.HOLDING_REGISTER:
                return [unit.registers.get(a, 0) for a in addresses]
            return [unit.coils.get(a, False) for a in addresses]
        finally:
            self._in_flight -= 1
            self.records.append(
                RequestRecord(start, self.clock.now, op, unit_id, address, count, outcome)
            )

    async def _apply_fault(self, fault: Fault, table: p.Table, count: int) -> Sequence[object]:
        if fault.kind == "timeout":
            await asyncio.Event().wait()  # hang until the transport's timeout fires
        if fault.kind == "disconnect":
            self._connected = False
            raise ConnectionResetError("mock disconnect")
        if fault.kind == "exception":
            raise p_exception(fault.exception_code)
        if fault.kind == "short":
            return [0] * (count - 1)
        if fault.kind == "garbage":
            return [70000] * count if table is p.Table.HOLDING_REGISTER else ["x"] * count
        raise AssertionError(f"unknown fault {fault.kind}")


def p_exception(code: int) -> Exception:
    from custom_components.wavin_wtc_net.errors import WtcModbusExceptionError

    return WtcModbusExceptionError(code)
