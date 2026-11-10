"""Pure WTC-NET protocol definitions: address map, codecs, wheel math and read planning.

No I/O and no Home Assistant imports. Addresses are Modbus protocol addresses (offset 0),
sent on the wire unchanged.

Confidence levels: ``BOTH`` is corroborated by independent sources (vendor document or
hardware), ``REFERENCE_ONLY`` has a single source and still needs hardware or vendor
confirmation. Nothing here writes; there is no write planning in this module on purpose.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from .models import ControllerMode, ControllerSnapshot, RawImage, ZoneSnapshot

MAX_ZONES = 7
MIN_UNIT_ID = 1
MAX_UNIT_ID = 247
INVALID_REGISTER_VALUES = frozenset({0xFFFF, 0xFFFE})


class Table(Enum):
    HOLDING_REGISTER = "holding_register"
    COIL = "coil"


class Confidence(Enum):
    BOTH = "both"
    REFERENCE_ONLY = "reference_only"


class WheelByteOrder(Enum):
    """Packing of two zone wheel codes in registers 4600-4603 (unverified)."""

    LOW_BYTE_FIRST = "low_byte_first"
    HIGH_BYTE_FIRST = "high_byte_first"


# --- Controller holding registers -------------------------------------------------
REG_WATER_TEMPERATURE = 4121
REG_CONTROLLER_HUMIDITY = 4127
REG_CONTROLLER_TEMPERATURE = 4128
REG_PI_OUTPUT = 4132
REG_CONTROLLER_DEW_POINT = 4133
REG_PROGRAM_CODE = 4134
REG_MODE = 4135
REG_FIRMWARE = 4500
REG_SERIAL_LOW = 4501
REG_SERIAL_HIGH = 4502

# --- Zone holding registers --------------------------------------------------------
REG_ZONE_BASE = 4136
REG_ZONE_STRIDE = 6
REG_SETPOINT_BASE = 4178
REG_SETPOINT_STRIDE = 3
REG_ECONOMY_COOL_BASE = 4700
REG_PACKED_WHEEL_BASE = 4600
REG_WHEEL_CODE_BASE = 4604

# --- Controller coils ---------------------------------------------------------------
COIL_HEATING_OUTPUT = 4120
COIL_COOLING_OUTPUT = 4121
COIL_EMERGENCY_WATER_TEMPERATURE = 4125
COIL_WATER_SENSOR_FAULT = 4126
COIL_GLOBAL_ON_OFF = 4127
COIL_GLOBAL_COMFORT_ECONOMY = 4128
COIL_GLOBAL_HEAT_COOL = 4129

# --- Zone coils ------------------------------------------------------------------------
COIL_ZONE_STATUS_BASE = 4130
COIL_ZONE_STATUS_STRIDE = 8
COIL_ZONE_CONTROL_BASE = 4604
COIL_ZONE_CONTROL_STRIDE = 8
COIL_LOCK_STATUS_BASE = 4186
COIL_LOCK_COMMAND_BASE = 4656  # written only by the lock command (write_transport allow-list)

GLOBAL_COILS = frozenset({COIL_GLOBAL_ON_OFF, COIL_GLOBAL_COMFORT_ECONOMY, COIL_GLOBAL_HEAT_COOL})


def validate_zone(zone: int) -> int:
    if not 1 <= zone <= MAX_ZONES:
        raise ValueError(f"zone must be 1..{MAX_ZONES}, got {zone}")
    return zone


def validate_unit_id(unit_id: int) -> int:
    if not MIN_UNIT_ID <= unit_id <= MAX_UNIT_ID:
        raise ValueError(f"unit id must be {MIN_UNIT_ID}..{MAX_UNIT_ID}, got {unit_id}")
    return unit_id


def zone_register_base(zone: int) -> int:
    return REG_ZONE_BASE + REG_ZONE_STRIDE * (validate_zone(zone) - 1)


def zone_setpoint_base(zone: int) -> int:
    return REG_SETPOINT_BASE + REG_SETPOINT_STRIDE * (validate_zone(zone) - 1)


def zone_economy_cool_register(zone: int) -> int:
    return REG_ECONOMY_COOL_BASE + validate_zone(zone) - 1


def zone_packed_wheel_register(zone: int) -> int:
    return REG_PACKED_WHEEL_BASE + (validate_zone(zone) - 1) // 2


def zone_wheel_code_register(zone: int) -> int:
    return REG_WHEEL_CODE_BASE + validate_zone(zone) - 1


def zone_status_coil_base(zone: int) -> int:
    return COIL_ZONE_STATUS_BASE + COIL_ZONE_STATUS_STRIDE * (validate_zone(zone) - 1)


def zone_control_coil_base(zone: int) -> int:
    return COIL_ZONE_CONTROL_BASE + COIL_ZONE_CONTROL_STRIDE * (validate_zone(zone) - 1)


def zone_power_control_coil(zone: int) -> int:
    return zone_control_coil_base(zone)


def zone_preset_control_coil(zone: int) -> int:
    return zone_control_coil_base(zone) + 1


def zone_lock_command_coil(zone: int) -> int:
    return COIL_LOCK_COMMAND_BASE + validate_zone(zone) - 1


def zone_lock_status_coil(zone: int) -> int:
    return COIL_LOCK_STATUS_BASE + validate_zone(zone) - 1


# --- Read points -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Point:
    table: Table
    address: int
    name: str
    confidence: Confidence
    #: Never changes at runtime (firmware, serial): read after (re)connect, then rarely.
    static: bool = False


_B = Confidence.BOTH
_R = Confidence.REFERENCE_ONLY
_H = Table.HOLDING_REGISTER
_C = Table.COIL


def controller_points() -> tuple[Point, ...]:
    return (
        Point(_H, REG_WATER_TEMPERATURE, "water_temperature", _B),
        Point(_H, REG_CONTROLLER_HUMIDITY, "humidity", _R),
        Point(_H, REG_CONTROLLER_TEMPERATURE, "room_temperature", _R),
        Point(_H, REG_PI_OUTPUT, "pi_output", _R),
        Point(_H, REG_CONTROLLER_DEW_POINT, "dew_point", _R),
        Point(_H, REG_PROGRAM_CODE, "program_code", _R),
        Point(_H, REG_MODE, "mode", _B),
        Point(_H, REG_FIRMWARE, "firmware", _R, static=True),
        Point(_H, REG_SERIAL_LOW, "serial_low", _R, static=True),
        Point(_H, REG_SERIAL_HIGH, "serial_high", _R, static=True),
        Point(_C, COIL_HEATING_OUTPUT, "heating_output", _R),
        Point(_C, COIL_COOLING_OUTPUT, "cooling_output", _R),
        Point(_C, COIL_EMERGENCY_WATER_TEMPERATURE, "emergency_water_temperature", _R),
        Point(_C, COIL_WATER_SENSOR_FAULT, "water_sensor_fault", _R),
        Point(_C, COIL_GLOBAL_ON_OFF, "global_on_off", _B),
        Point(_C, COIL_GLOBAL_COMFORT_ECONOMY, "global_comfort_economy", _B),
        Point(_C, COIL_GLOBAL_HEAT_COOL, "global_heat_cool", _B),
    )


_ZONE_REGISTER_OFFSETS: tuple[tuple[int, str, Confidence], ...] = (
    (0, "temperature", _B),
    (1, "humidity", _B),
    (2, "dew_point", _B),
    (3, "wheel_offset", _R),
    (4, "dew_point_resistance", _R),
    (5, "illuminance", _R),
)

_ZONE_STATUS_OFFSETS: tuple[tuple[int, str, Confidence], ...] = (
    (0, "dry", _B),
    (1, "valve_open", _B),
    (2, "condensation", _B),
    (3, "dew_point_input", _B),
    (4, "on_off", _R),
    (5, "comfort_economy", _R),
    (6, "heat_cool", _B),
    (7, "live", _B),
)

_ZONE_CONTROL_OFFSETS: tuple[tuple[int, str, Confidence], ...] = (
    (0, "control_on_off", _B),
    (1, "control_comfort_economy", _B),
    (2, "control_heat_cool", _R),
)


def zone_points(zone: int) -> tuple[Point, ...]:
    base = zone_register_base(zone)
    setpoints = zone_setpoint_base(zone)
    status = zone_status_coil_base(zone)
    control = zone_control_coil_base(zone)
    prefix = f"zone{zone}."
    points: list[Point] = [
        Point(_H, base + offset, prefix + name, conf)
        for offset, name, conf in _ZONE_REGISTER_OFFSETS
    ]
    points += [
        Point(_H, setpoints, prefix + "comfort_cool_setpoint", _R),
        Point(_H, setpoints + 1, prefix + "comfort_heat_setpoint", _R),
        Point(_H, setpoints + 2, prefix + "economy_heat_setpoint", _R),
        Point(_H, zone_economy_cool_register(zone), prefix + "economy_cool_setpoint", _R),
        Point(_H, zone_packed_wheel_register(zone), prefix + "packed_wheel_word", _R),
        Point(_H, zone_wheel_code_register(zone), prefix + "wheel_code", _B),
    ]
    points += [Point(_C, status + o, prefix + n, c) for o, n, c in _ZONE_STATUS_OFFSETS]
    points += [Point(_C, control + o, prefix + n, c) for o, n, c in _ZONE_CONTROL_OFFSETS]
    points.append(Point(_C, zone_lock_status_coil(zone), prefix + "lock_status", _R))
    return tuple(points)


def unit_points(zones: Iterable[int]) -> tuple[Point, ...]:
    """All points read for one controller; packed wheel words shared by two zones appear once."""
    seen: dict[tuple[Table, int], Point] = {}
    for point in controller_points():
        seen[(point.table, point.address)] = point
    for zone in sorted(set(zones)):
        for point in zone_points(zone):
            key = (point.table, point.address)
            if key in seen:
                existing = seen[key]
                if existing.name.endswith("packed_wheel_word") and point.name.endswith(
                    "packed_wheel_word"
                ):
                    continue
                raise ValueError(f"address collision: {existing.name} and {point.name}")
            seen[key] = point
    return tuple(seen.values())


# --- Read planning ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """One wire request. ``required`` is true if any covered point is corroborated."""

    table: Table
    address: int
    count: int
    points: tuple[Point, ...]

    @property
    def required(self) -> bool:
        return any(p.confidence is Confidence.BOTH for p in self.points)

    @property
    def last_address(self) -> int:
        return self.address + self.count - 1

    def describe(self) -> str:
        return f"{self.table.value}[{self.address}..{self.last_address}]"


def plan_reads(
    points: Iterable[Point],
    *,
    max_registers: int,
    max_coils: int,
    max_gap: int,
    blocked: Collection[tuple[Table, int]] = (),
) -> tuple[ReadRequest, ...]:
    """Group points into logical spans and split them by hard per-request caps.

    A request always starts and ends on a needed address. A new request starts when
    the next address would exceed the table's cap or the hole before it is larger
    than ``max_gap``. Holding registers are planned before coils.
    A hole is never bridged if it contains a ``blocked`` address (known to fail).
    """
    if max_registers < 1 or max_coils < 1:
        raise ValueError("request caps must be positive")
    if max_gap < 0:
        raise ValueError("max_gap must not be negative")
    blocked_set = frozenset(blocked)
    by_table: dict[Table, dict[int, Point]] = {_H: {}, _C: {}}
    for point in points:
        if point.address < 0:
            raise ValueError(f"negative address for {point.name}")
        by_table[point.table][point.address] = point

    requests: list[ReadRequest] = []
    for table, cap in ((_H, max_registers), (_C, max_coils)):
        current: list[Point] = []
        for address in sorted(by_table[table]):
            point = by_table[table][address]
            if current:
                start = current[0].address
                previous = current[-1].address
                hole_blocked = any(
                    (table, hole) in blocked_set for hole in range(previous + 1, address)
                )
                if address - start + 1 > cap or address - previous - 1 > max_gap or hole_blocked:
                    requests.append(_close_request(table, current))
                    current = []
            current.append(point)
        if current:
            requests.append(_close_request(table, current))
    return tuple(requests)


def _close_request(table: Table, points: Sequence[Point]) -> ReadRequest:
    start = points[0].address
    return ReadRequest(table, start, points[-1].address - start + 1, tuple(points))


# --- Codecs ----------------------------------------------------------------------------


def to_signed16(raw: int) -> int:
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"register value out of range: {raw}")
    return raw - 0x10000 if raw & 0x8000 else raw


def decode_signed_tenths(raw: int | None) -> float | None:
    """Signed 16-bit value scaled by 0.1; sentinels decode to ``None``."""
    if raw is None or raw in INVALID_REGISTER_VALUES:
        return None
    return round(to_signed16(raw) / 10, 1)


def decode_unsigned_tenths(raw: int | None) -> float | None:
    if raw is None or raw in INVALID_REGISTER_VALUES:
        return None
    return round(raw / 10, 1)


def decode_raw(raw: int | None) -> int | None:
    if raw is None or raw in INVALID_REGISTER_VALUES:
        return None
    return raw


def decode_serial(low: int | None, high: int | None) -> int | None:
    if low is None or high is None:
        return None
    if low in INVALID_REGISTER_VALUES or high in INVALID_REGISTER_VALUES:
        return None
    return (high << 16) | low


def decode_mode(raw: int | None) -> ControllerMode | None:
    if raw is None:
        return None
    try:
        return ControllerMode(raw)
    except ValueError:
        return None


def unpack_wheel_codes(word: int, order: WheelByteOrder) -> tuple[int, int]:
    """Return (odd zone code, even zone code) from one packed word (unverified layout)."""
    if not 0 <= word <= 0xFFFF:
        raise ValueError(f"register value out of range: {word}")
    low, high = word & 0xFF, word >> 8
    return (low, high) if order is WheelByteOrder.LOW_BYTE_FIRST else (high, low)


def packed_wheel_code(word: int, zone: int, order: WheelByteOrder) -> int:
    first, second = unpack_wheel_codes(word, order)
    return first if validate_zone(zone) % 2 == 1 else second


# --- Wheel math ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WheelScale:
    """``target = reference + (code - center_code) * step``."""

    center_code: int = 6
    min_code: int = 0
    max_code: int = 12
    step: float = 0.5

    def __post_init__(self) -> None:
        if not self.min_code <= self.center_code <= self.max_code:
            raise ValueError("center code must lie within min/max code")
        if not (math.isfinite(self.step) and self.step > 0):
            raise ValueError("step must be a positive finite number")


TARGET_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class WheelCalibration:
    """Per-installation comfort-target calibration.

    ``target = neutral(cooling) + (code - center_code) * step(cooling)`` for the comfort preset.
    Heating and cooling have their own neutral value and step. Economy targets do not use the
    wheel (hardware-validated): they come from the economy setpoint registers.
    """

    neutral_heat: float = 22.0
    neutral_cool: float = 26.0
    step_heat: float = 0.5
    step_cool: float = 0.5
    center_code: int = 6
    min_code: int = 0
    max_code: int = 12

    def __post_init__(self) -> None:
        for name in ("neutral_heat", "neutral_cool"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        self.scale(cooling=False)  # validates step_heat and the code range
        self.scale(cooling=True)

    def scale(self, *, cooling: bool) -> WheelScale:
        return WheelScale(
            center_code=self.center_code,
            min_code=self.min_code,
            max_code=self.max_code,
            step=self.step_cool if cooling else self.step_heat,
        )

    def neutral(self, *, cooling: bool) -> float:
        return self.neutral_cool if cooling else self.neutral_heat

    def comfort_target(self, code: int, *, cooling: bool) -> float:
        return wheel_code_to_target(
            code, self.neutral(cooling=cooling), self.scale(cooling=cooling)
        )

    def comfort_code(self, target: float, *, cooling: bool) -> int:
        """Exact raw code for a requested comfort target; raises ``ValueError`` otherwise."""
        return target_to_wheel_code(
            target, self.neutral(cooling=cooling), self.scale(cooling=cooling)
        )


def wheel_code_to_target(code: int, reference: float, scale: WheelScale) -> float:
    if not scale.min_code <= code <= scale.max_code:
        raise ValueError(f"wheel code {code} outside {scale.min_code}..{scale.max_code}")
    return round(reference + (code - scale.center_code) * scale.step, 3)


def target_to_wheel_code(
    target: float, reference: float, scale: WheelScale, *, tolerance: float = TARGET_TOLERANCE
) -> int:
    """Map a target to exactly one raw code. Never rounds away a partial step."""
    steps = (target - reference) / scale.step
    nearest = round(steps)
    if abs(steps - nearest) * scale.step > tolerance:
        raise ValueError(f"target {target} is not a whole wheel step from {reference}")
    code = scale.center_code + nearest
    if not scale.min_code <= code <= scale.max_code:
        raise ValueError(f"target {target} outside wheel range")
    return code


def zone_target_temperature(
    zone: ZoneSnapshot, calibration: WheelCalibration, *, cooling: bool, comfort: bool
) -> float | None:
    """Regulation target of a zone as read from the device; ``None`` when not derivable.

    Comfort: neutral + (wheel code - centre) x step, separately for heating and cooling (cooling
    follows the heating rule with its own neutral/step, decided 2026-11-07; no cooling run yet).
    Economy heating: the economy setpoint register, the wheel is not applied (hardware dial
    checks). Economy cooling is unvalidated and therefore unavailable, never guessed.
    """
    if comfort:
        if zone.wheel_code is None:
            return None
        try:
            return calibration.comfort_target(zone.wheel_code, cooling=cooling)
        except ValueError:
            return None
    return None if cooling else zone.economy_heat_setpoint


# --- Snapshot decoding -------------------------------------------------------------------


def _reg(image: RawImage, address: int) -> int | None:
    return image.registers.get(address)


def _coil(image: RawImage, address: int) -> bool | None:
    return image.coils.get(address)


def decode_controller(image: RawImage) -> ControllerSnapshot:
    mode_raw = _reg(image, REG_MODE)
    return ControllerSnapshot(
        water_temperature=decode_signed_tenths(_reg(image, REG_WATER_TEMPERATURE)),
        room_temperature=decode_signed_tenths(_reg(image, REG_CONTROLLER_TEMPERATURE)),
        humidity=decode_unsigned_tenths(_reg(image, REG_CONTROLLER_HUMIDITY)),
        pi_output=decode_unsigned_tenths(_reg(image, REG_PI_OUTPUT)),
        dew_point=decode_signed_tenths(_reg(image, REG_CONTROLLER_DEW_POINT)),
        program_code=decode_raw(_reg(image, REG_PROGRAM_CODE)),
        mode=decode_mode(mode_raw),
        mode_raw=mode_raw,
        firmware=decode_raw(_reg(image, REG_FIRMWARE)),
        serial=decode_serial(_reg(image, REG_SERIAL_LOW), _reg(image, REG_SERIAL_HIGH)),
        global_on_off=_coil(image, COIL_GLOBAL_ON_OFF),
        global_comfort_economy=_coil(image, COIL_GLOBAL_COMFORT_ECONOMY),
        global_heat_cool=_coil(image, COIL_GLOBAL_HEAT_COOL),
        heating_output=_coil(image, COIL_HEATING_OUTPUT),
        cooling_output=_coil(image, COIL_COOLING_OUTPUT),
        emergency_water_temperature=_coil(image, COIL_EMERGENCY_WATER_TEMPERATURE),
        water_sensor_fault=_coil(image, COIL_WATER_SENSOR_FAULT),
    )


def decode_zone(image: RawImage, zone: int) -> ZoneSnapshot:
    base = zone_register_base(zone)
    setpoints = zone_setpoint_base(zone)
    status = zone_status_coil_base(zone)
    control = zone_control_coil_base(zone)
    return ZoneSnapshot(
        zone=zone,
        temperature=decode_signed_tenths(_reg(image, base)),
        humidity=decode_unsigned_tenths(_reg(image, base + 1)),
        dew_point=decode_signed_tenths(_reg(image, base + 2)),
        wheel_offset=decode_signed_tenths(_reg(image, base + 3)),
        dew_point_resistance_raw=decode_raw(_reg(image, base + 4)),
        illuminance_raw=decode_raw(_reg(image, base + 5)),
        wheel_code=decode_raw(_reg(image, zone_wheel_code_register(zone))),
        packed_wheel_word=_reg(image, zone_packed_wheel_register(zone)),
        comfort_cool_setpoint=decode_signed_tenths(_reg(image, setpoints)),
        comfort_heat_setpoint=decode_signed_tenths(_reg(image, setpoints + 1)),
        economy_heat_setpoint=decode_signed_tenths(_reg(image, setpoints + 2)),
        economy_cool_setpoint=decode_signed_tenths(_reg(image, zone_economy_cool_register(zone))),
        dry=_coil(image, status),
        valve_open=_coil(image, status + 1),
        condensation=_coil(image, status + 2),
        dew_point_input=_coil(image, status + 3),
        on_off=_coil(image, status + 4),
        comfort_economy=_coil(image, status + 5),
        heat_cool=_coil(image, status + 6),
        live=_coil(image, status + 7),
        control_on_off=_coil(image, control),
        control_comfort_economy=_coil(image, control + 1),
        control_heat_cool=_coil(image, control + 2),
        lock_status=_coil(image, zone_lock_status_coil(zone)),
    )
