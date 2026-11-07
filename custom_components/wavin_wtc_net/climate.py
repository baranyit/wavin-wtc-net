"""Climate entities, one per zone.

State comes from the last polled snapshot only, never from a command. A zone gets a settable
comfort target only when write mode is on and the zone is explicitly enabled; the value is queued
and sent at the start of the next poll cycle, then confirmed by that cycle's read-back.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WtcConfigEntry
from .calibration import zone_effective_target
from .commands import CommandRejectedError
from .const import CONF_UNITS, COOLING, DOMAIN, ON_OFF_ON
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
        coordinator = entry.runtime_data.coordinator
        self._calibration = coordinator.calibration
        executor = coordinator.executor
        self._writable = executor is not None and executor.permissions.allows(unit_id, zone)
        if self._writable:
            self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def _cooling(self) -> bool:
        return self.hvac_mode is HVACMode.COOL

    @property
    def target_temperature_step(self) -> float:
        return self._calibration.scale(cooling=self._cooling).step

    @property
    def min_temp(self) -> float:
        scale = self._calibration.scale(cooling=self._cooling)
        neutral = self._calibration.neutral(cooling=self._cooling)
        return neutral + (scale.min_code - scale.center_code) * scale.step

    @property
    def max_temp(self) -> float:
        scale = self._calibration.scale(cooling=self._cooling)
        neutral = self._calibration.neutral(cooling=self._cooling)
        return neutral + (scale.max_code - scale.center_code) * scale.step

    @property
    def target_temperature(self) -> float | None:
        zone = self.zone_snapshot
        return zone_effective_target(zone, self._calibration) if zone else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        executor = self.coordinator.executor
        if not self._writable or executor is None or self._zone is None:
            return None
        record = executor.queue.latest(self._unit_id, self._zone)
        if record is None:
            return None
        return {
            "last_command_target": record.command.target,
            "last_command_status": record.status.value,
            "last_command_detail": record.detail,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Queue only. The write happens at the next scheduled cycle; state changes on read-back."""
        if (target := kwargs.get(ATTR_TEMPERATURE)) is None or self._zone is None:
            return
        try:
            self.coordinator.submit_wheel_target(self._unit_id, self._zone, float(target))
        except CommandRejectedError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"reason": str(err)},
            ) from err

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
