"""Climate entities, one per zone.

State comes from the last polled snapshot. A zone gets a settable comfort target, on/off and
preset only when write mode is on and the zone is explicitly enabled. Such a request is shown
immediately (optimistic overlay), written after a 2 s debounce and decided by the next poll cycle:
if the read shows a different value (a dial turn, a rejected write), the real value is shown.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    PRESET_COMFORT,
    PRESET_ECO,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WtcConfigEntry
from .calibration import zone_effective_target
from .commands import CommandKind, CommandRejectedError
from .const import COMFORT, CONF_UNITS, COOLING, DOMAIN, ON_OFF_ON
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
        self._mode_writable = executor is not None and executor.permissions.allows_mode(
            unit_id, zone
        )
        features = ClimateEntityFeature(0)
        if self._writable:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._mode_writable:
            features |= (
                ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.PRESET_MODE
            )
            self._attr_preset_modes = [PRESET_COMFORT, PRESET_ECO]
        self._attr_supported_features = features

    @property
    def preset_mode(self) -> str | None:
        zone = self.zone_snapshot
        if zone is None or self._zone is None or not self._mode_writable:
            return None
        if (
            pending := self.coordinator.optimistic(
                self._unit_id, self._zone, CommandKind.ZONE_PRESET
            )
        ) is not None:
            return PRESET_COMFORT if pending.target else PRESET_ECO
        value = zone.comfort_economy
        if value is None:
            value = zone.control_comfort_economy
        if value is None:
            return None
        return PRESET_COMFORT if value == COMFORT else PRESET_ECO

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._queue_mode(CommandKind.ZONE_PRESET, preset_mode == PRESET_COMFORT)

    async def async_turn_on(self) -> None:
        self._queue_mode(CommandKind.ZONE_POWER, True)

    async def async_turn_off(self) -> None:
        self._queue_mode(CommandKind.ZONE_POWER, False)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Only off and on: heat/cool is never written (vendor: no effect per zone in slave)."""
        if hvac_mode is HVACMode.OFF:
            self._queue_mode(CommandKind.ZONE_POWER, False)
        elif hvac_mode in (HVACMode.HEAT, HVACMode.COOL) and hvac_mode is self.hvac_mode:
            return  # already in that mode
        elif hvac_mode in (HVACMode.HEAT, HVACMode.COOL) and self.hvac_mode is HVACMode.OFF:
            self._queue_mode(CommandKind.ZONE_POWER, True)
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"reason": "heat/cool cannot be changed from here"},
            )

    def _queue_mode(self, kind: CommandKind, value: bool) -> None:
        if self._zone is None:
            return
        try:
            self.coordinator.submit_zone_mode(self._unit_id, self._zone, kind, value)
        except CommandRejectedError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"reason": str(err)},
            ) from err

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
        if zone is None:
            return None
        if self._zone is not None and (
            pending := self.coordinator.optimistic(
                self._unit_id, self._zone, CommandKind.WHEEL_TARGET
            )
        ):
            return float(pending.target)
        return zone_effective_target(zone, self._calibration)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        executor = self.coordinator.executor
        if not (self._writable or self._mode_writable) or executor is None or self._zone is None:
            return None
        records = [
            executor.queue.latest(self._unit_id, self._zone, kind)
            for kind in (CommandKind.WHEEL_TARGET, CommandKind.ZONE_POWER, CommandKind.ZONE_PRESET)
        ]
        latest = max((r for r in records if r), key=lambda r: r.updated_at, default=None)
        if latest is None:
            return None
        return {
            "last_command_kind": latest.command.kind.value,
            "last_command_target": latest.command.target,
            "last_command_status": latest.status.value,
            "last_command_detail": latest.detail,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Queue; shown at once, written after the debounce, decided by the next reads."""
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
        if self._zone is not None and (
            pending := self.coordinator.optimistic(
                self._unit_id, self._zone, CommandKind.ZONE_POWER
            )
        ):
            return bool(pending.target)
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
