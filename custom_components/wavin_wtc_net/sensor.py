"""Sensors: controller and zone values. Raw/diagnostic points are disabled by default."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WtcConfigEntry
from .const import (
    COMFORT,
    CONF_NEUTRAL_COOL,
    CONF_NEUTRAL_HEAT,
    CONF_UNITS,
    CONF_WHEEL_STEP_COOL,
    CONF_WHEEL_STEP_HEAT,
    COOLING,
    DEFAULT_NEUTRAL_COOL,
    DEFAULT_NEUTRAL_HEAT,
    DEFAULT_WHEEL_STEP,
    ON_OFF_ON,
)
from .entity import WtcEntity
from .models import ControllerSnapshot, ZoneSnapshot
from .protocol import WheelCalibration, zone_target_temperature

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WtcControllerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ControllerSnapshot], float | int | str | None]


@dataclass(frozen=True, kw_only=True)
class WtcZoneSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ZoneSnapshot], float | int | str | None]


def _mode(controller: ControllerSnapshot) -> str | None:
    return controller.mode.name.lower() if controller.mode is not None else None


def _preset(zone: ZoneSnapshot) -> str | None:
    value = zone.comfort_economy
    if value is None:
        value = zone.control_comfort_economy
    if value is None:
        return None
    return "comfort" if value == COMFORT else "economy"


CONTROLLER_SENSORS: tuple[WtcControllerSensorDescription, ...] = (
    WtcControllerSensorDescription(
        key="water_temperature",
        translation_key="water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.water_temperature,
    ),
    WtcControllerSensorDescription(
        key="mode",
        translation_key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=["init", "heat", "cool", "changeover"],
        value_fn=_mode,
    ),
    WtcControllerSensorDescription(
        key="pi_output",
        translation_key="pi_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.pi_output,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcControllerSensorDescription(
        key="room_temperature",
        translation_key="controller_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.room_temperature,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcControllerSensorDescription(
        key="humidity",
        translation_key="controller_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.humidity,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcControllerSensorDescription(
        key="dew_point",
        translation_key="controller_dew_point",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.dew_point,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcControllerSensorDescription(
        key="program_code",
        translation_key="program_code",
        value_fn=lambda c: c.program_code,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcControllerSensorDescription(
        key="firmware",
        translation_key="firmware",
        value_fn=lambda c: c.firmware,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WtcControllerSensorDescription(
        key="serial",
        translation_key="serial",
        value_fn=lambda c: c.serial,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

ZONE_SENSORS: tuple[WtcZoneSensorDescription, ...] = (
    WtcZoneSensorDescription(
        key="temperature",
        translation_key="zone_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.temperature,
    ),
    WtcZoneSensorDescription(
        key="humidity",
        translation_key="zone_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.humidity,
    ),
    WtcZoneSensorDescription(
        key="dew_point",
        translation_key="zone_dew_point",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.dew_point,
    ),
    WtcZoneSensorDescription(
        key="preset",
        translation_key="preset",
        device_class=SensorDeviceClass.ENUM,
        options=["comfort", "economy"],
        value_fn=_preset,
    ),
    WtcZoneSensorDescription(
        key="wheel_offset",
        translation_key="wheel_offset",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.wheel_offset,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcZoneSensorDescription(
        key="wheel_code",
        translation_key="wheel_code",
        value_fn=lambda z: z.wheel_code,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcZoneSensorDescription(
        key="comfort_cool_setpoint",
        translation_key="comfort_cool_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.comfort_cool_setpoint,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcZoneSensorDescription(
        key="comfort_heat_setpoint",
        translation_key="comfort_heat_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.comfort_heat_setpoint,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcZoneSensorDescription(
        key="economy_heat_setpoint",
        translation_key="economy_heat_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.economy_heat_setpoint,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    WtcZoneSensorDescription(
        key="economy_cool_setpoint",
        translation_key="economy_cool_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda z: z.economy_cool_setpoint,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: WtcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entities: list[SensorEntity] = []
    for unit_text, zones in entry.data[CONF_UNITS].items():
        unit = int(unit_text)
        entities += [WtcControllerSensor(entry, unit, d) for d in CONTROLLER_SENSORS]
        for zone in range(1, zones + 1):
            entities += [WtcZoneSensor(entry, unit, zone, d) for d in ZONE_SENSORS]
            entities.append(WtcTargetSensor(entry, unit, zone))
    async_add_entities(entities)


class WtcControllerSensor(WtcEntity, SensorEntity):
    entity_description: WtcControllerSensorDescription

    def __init__(
        self, entry: WtcConfigEntry, unit_id: int, description: WtcControllerSensorDescription
    ) -> None:
        super().__init__(entry, unit_id, None, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        controller = self.controller
        return self.entity_description.value_fn(controller) if controller else None


class WtcZoneSensor(WtcEntity, SensorEntity):
    entity_description: WtcZoneSensorDescription

    def __init__(
        self, entry: WtcConfigEntry, unit_id: int, zone: int, description: WtcZoneSensorDescription
    ) -> None:
        super().__init__(entry, unit_id, zone, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        zone = self.zone_snapshot
        return self.entity_description.value_fn(zone) if zone else None


TARGET_DESCRIPTION = SensorEntityDescription(
    key="target_temperature",
    translation_key="target_temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
)


class WtcTargetSensor(WtcEntity, SensorEntity):
    """Regulation target derived from the polled wheel code and the entry's calibration.

    A sensor, not the climate target: the climate entity stays without controls until plan step 5.
    Unknown mode/preset, off, or an unvalidated case (economy cooling) gives no value.
    """

    entity_description = TARGET_DESCRIPTION

    def __init__(self, entry: WtcConfigEntry, unit_id: int, zone: int) -> None:
        super().__init__(entry, unit_id, zone, TARGET_DESCRIPTION.key)
        options = entry.options
        self._calibration = WheelCalibration(
            neutral_heat=float(options.get(CONF_NEUTRAL_HEAT, DEFAULT_NEUTRAL_HEAT)),
            neutral_cool=float(options.get(CONF_NEUTRAL_COOL, DEFAULT_NEUTRAL_COOL)),
            step_heat=float(options.get(CONF_WHEEL_STEP_HEAT, DEFAULT_WHEEL_STEP)),
            step_cool=float(options.get(CONF_WHEEL_STEP_COOL, DEFAULT_WHEEL_STEP)),
        )

    @property
    def native_value(self) -> float | None:
        zone = self.zone_snapshot
        if zone is None:
            return None
        on = zone.on_off if zone.on_off is not None else zone.control_on_off
        preset = (
            zone.comfort_economy
            if zone.comfort_economy is not None
            else zone.control_comfort_economy
        )
        if on != ON_OFF_ON or zone.heat_cool is None or preset is None:
            return None
        return zone_target_temperature(
            zone,
            self._calibration,
            cooling=zone.heat_cool == COOLING,
            comfort=preset == COMFORT,
        )
