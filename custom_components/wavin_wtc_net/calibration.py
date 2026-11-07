"""Entry options -> wheel calibration and the derived zone target (shared by entities)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_NEUTRAL_COOL,
    CONF_NEUTRAL_HEAT,
    CONF_WHEEL_STEP_COOL,
    CONF_WHEEL_STEP_HEAT,
    DEFAULT_NEUTRAL_COOL,
    DEFAULT_NEUTRAL_HEAT,
    DEFAULT_WHEEL_STEP,
)
from .models import ZoneSnapshot
from .polarity import COMFORT, COOLING, ON_OFF_ON
from .protocol import WheelCalibration, zone_target_temperature


def calibration_from_options(options: Mapping[str, Any]) -> WheelCalibration:
    return WheelCalibration(
        neutral_heat=float(options.get(CONF_NEUTRAL_HEAT, DEFAULT_NEUTRAL_HEAT)),
        neutral_cool=float(options.get(CONF_NEUTRAL_COOL, DEFAULT_NEUTRAL_COOL)),
        step_heat=float(options.get(CONF_WHEEL_STEP_HEAT, DEFAULT_WHEEL_STEP)),
        step_cool=float(options.get(CONF_WHEEL_STEP_COOL, DEFAULT_WHEEL_STEP)),
    )


def zone_effective_target(zone: ZoneSnapshot, calibration: WheelCalibration) -> float | None:
    """Regulation target as read from the device; ``None`` when off or not derivable."""
    on = zone.on_off if zone.on_off is not None else zone.control_on_off
    preset = (
        zone.comfort_economy if zone.comfort_economy is not None else zone.control_comfort_economy
    )
    if on != ON_OFF_ON or zone.heat_cool is None or preset is None:
        return None
    return zone_target_temperature(
        zone, calibration, cooling=zone.heat_cool == COOLING, comfort=preset == COMFORT
    )
