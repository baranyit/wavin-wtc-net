"""Typed immutable snapshots produced by the read path.

Pure Python: no Home Assistant imports. Boolean coil fields keep the neutral
names from the specification (``on_off``, ``comfort_economy``, ``heat_cool``)
because their polarity has not been confirmed on hardware yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType


class ControllerMode(IntEnum):
    """Register 4135 (corroborated by both sources)."""

    INIT = 0
    HEAT = 1
    COOL = 2
    CHANGEOVER = 3


@dataclass(frozen=True, slots=True)
class RawImage:
    """Raw values read in one unit cycle, keyed by protocol address (offset not applied)."""

    registers: Mapping[int, int]
    coils: Mapping[int, bool]

    @classmethod
    def build(cls, registers: Mapping[int, int], coils: Mapping[int, bool]) -> RawImage:
        return cls(MappingProxyType(dict(registers)), MappingProxyType(dict(coils)))


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    """Controller-level values. ``None`` means not read, not supported or invalid."""

    water_temperature: float | None
    room_temperature: float | None
    humidity: float | None
    pi_output: float | None
    dew_point: float | None
    program_code: int | None
    mode: ControllerMode | None
    mode_raw: int | None
    firmware: int | None
    serial: int | None
    global_on_off: bool | None
    global_comfort_economy: bool | None
    global_heat_cool: bool | None
    heating_output: bool | None
    cooling_output: bool | None
    emergency_water_temperature: bool | None
    water_sensor_fault: bool | None


@dataclass(frozen=True, slots=True)
class ZoneSnapshot:
    """Zone-level values. Setpoints are decoded without any compensation."""

    zone: int
    temperature: float | None
    humidity: float | None
    dew_point: float | None
    wheel_offset: float | None
    dew_point_resistance_raw: int | None
    illuminance_raw: int | None
    wheel_code: int | None
    packed_wheel_word: int | None
    comfort_cool_setpoint: float | None
    comfort_heat_setpoint: float | None
    economy_heat_setpoint: float | None
    economy_cool_setpoint: float | None
    dry: bool | None
    valve_open: bool | None
    condensation: bool | None
    dew_point_input: bool | None
    on_off: bool | None
    comfort_economy: bool | None
    heat_cool: bool | None
    live: bool | None
    control_on_off: bool | None
    control_comfort_economy: bool | None
    control_heat_cool: bool | None
    lock_status: bool | None


@dataclass(frozen=True, slots=True)
class UnitSnapshot:
    """One controller (Modbus unit ID). Unavailable units carry no data, never stale data."""

    unit_id: int
    available: bool
    controller: ControllerSnapshot | None
    zones: tuple[ZoneSnapshot, ...]
    error: str | None
    failed_optional_reads: tuple[str, ...]
    raw: RawImage | None
    read_at: float
    #: Optional points currently skipped on this connection after a failed read.
    excluded_points: tuple[str, ...] = ()

    def zone(self, zone: int) -> ZoneSnapshot | None:
        for item in self.zones:
            if item.zone == zone:
                return item
        return None


@dataclass(frozen=True, slots=True)
class GatewaySnapshot:
    """Result of one complete read cycle over every configured unit of an endpoint."""

    units: tuple[UnitSnapshot, ...]
    cycle_started: float
    cycle_finished: float

    def unit(self, unit_id: int) -> UnitSnapshot | None:
        for item in self.units:
            if item.unit_id == unit_id:
                return item
        return None
