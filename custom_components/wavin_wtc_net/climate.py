"""Read-only climate entities, one per zone.

No target temperature and no controls yet: the wheel formula is validated in plan step 4 and
commands arrive in plan step 5. State comes from the last polled snapshot only.
"""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WtcConfigEntry
from .const import CONF_UNITS, COOLING, ON_OFF_ON
from .entity import WtcEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: WtcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(
        WtcZoneClimate(entry, int(unit), zone)
        for unit, zones in entry.data[CONF_UNITS].items()
        for zone in range(1, zones + 1)
    )


class WtcZoneClimate(WtcEntity, ClimateEntity):
    _attr_name = None
    _attr_translation_key = "zone"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]  # noqa: RUF012
    _attr_supported_features = ClimateEntityFeature(0)  # read-only

    def __init__(self, entry: WtcConfigEntry, unit_id: int, zone: int) -> None:
        super().__init__(entry, unit_id, zone, "climate")

    @property
    def _is_on(self) -> bool | None:
        zone = self.zone_snapshot
        if zone is None:
            return None
        return zone.on_off if zone.on_off is not None else zone.control_on_off

    @property
    def current_temperature(self) -> float | None:
        zone = self.zone_snapshot
        return zone.temperature if zone else None

    @property
    def current_humidity(self) -> float | None:
        zone = self.zone_snapshot
        return zone.humidity if zone else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        zone = self.zone_snapshot
        if zone is None or self._is_on is None:
            return None
        if self._is_on != ON_OFF_ON:
            return HVACMode.OFF
        if zone.heat_cool is None:
            return None
        return HVACMode.COOL if zone.heat_cool == COOLING else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        zone = self.zone_snapshot
        mode = self.hvac_mode
        if zone is None or mode is None:
            return None
        if mode is HVACMode.OFF:
            return HVACAction.OFF
        if zone.valve_open is None:
            return None
        if not zone.valve_open:
            return HVACAction.IDLE
        return HVACAction.COOLING if mode is HVACMode.COOL else HVACAction.HEATING
