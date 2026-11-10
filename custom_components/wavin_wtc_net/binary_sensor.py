"""Binary sensors. Polarity: see ``const.py`` (confirmed by vendor document and hardware)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WtcConfigEntry
from .const import CONF_UNITS, LIVE_CONNECTED, LOCKED
from .entity import WtcEntity
from .models import ControllerSnapshot, ZoneSnapshot

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WtcControllerBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[ControllerSnapshot], bool | None]


@dataclass(frozen=True, kw_only=True)
class WtcZoneBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[ZoneSnapshot], bool | None]


def _eq(value: bool | None, expected: bool) -> bool | None:
    return None if value is None else value == expected


CONTROLLER_BINARY: tuple[WtcControllerBinaryDescription, ...] = (
    WtcControllerBinaryDescription(
        key="heating_output",
        translation_key="heating_output",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda c: c.heating_output,
    ),
    WtcControllerBinaryDescription(
        key="cooling_output",
        translation_key="cooling_output",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda c: c.cooling_output,
    ),
    WtcControllerBinaryDescription(
        key="emergency_water_temperature",
        translation_key="emergency_water_temperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.emergency_water_temperature,
    ),
    WtcControllerBinaryDescription(
        key="water_sensor_fault",
        translation_key="water_sensor_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.water_sensor_fault,
    ),
)

ZONE_BINARY: tuple[WtcZoneBinaryDescription, ...] = (
    WtcZoneBinaryDescription(
        key="valve_open",
        translation_key="valve_open",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda z: z.valve_open,
    ),
    WtcZoneBinaryDescription(
        key="condensation",
        translation_key="condensation",
        device_class=BinarySensorDeviceClass.MOISTURE,
        value_fn=lambda z: z.condensation,
    ),
    WtcZoneBinaryDescription(
        key="dry",
        translation_key="dry",
        value_fn=lambda z: z.dry,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WtcZoneBinaryDescription(
        key="live",
        translation_key="live",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda z: _eq(z.live, LIVE_CONNECTED),
    ),
    WtcZoneBinaryDescription(
        key="locked",
        translation_key="locked",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda z: _eq(z.lock_status, LOCKED),
    ),
    WtcZoneBinaryDescription(
        key="dew_point_input",
        translation_key="dew_point_input",
        value_fn=lambda z: z.dew_point_input,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: WtcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entities: list[BinarySensorEntity] = []
    for unit_text, zones in entry.data[CONF_UNITS].items():
        unit = int(unit_text)
        entities += [WtcControllerBinary(entry, unit, d) for d in CONTROLLER_BINARY]
        for zone in range(1, zones + 1):
            entities += [WtcZoneBinary(entry, unit, zone, d) for d in ZONE_BINARY]
    async_add_entities(entities)


class WtcControllerBinary(WtcEntity, BinarySensorEntity):
    entity_description: WtcControllerBinaryDescription

    def __init__(
        self, entry: WtcConfigEntry, unit_id: int, description: WtcControllerBinaryDescription
    ) -> None:
        super().__init__(entry, unit_id, None, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        controller = self.controller
        return self.entity_description.value_fn(controller) if controller else None


class WtcZoneBinary(WtcEntity, BinarySensorEntity):
    entity_description: WtcZoneBinaryDescription

    def __init__(
        self, entry: WtcConfigEntry, unit_id: int, zone: int, description: WtcZoneBinaryDescription
    ) -> None:
        super().__init__(entry, unit_id, zone, description.key)
        self.entity_description = description
        # The "live" sensor reports the disconnect itself, so it must stay available.
        self._requires_live = description.key != "live"

    @property
    def is_on(self) -> bool | None:
        zone = self.zone_snapshot
        return self.entity_description.value_fn(zone) if zone else None
