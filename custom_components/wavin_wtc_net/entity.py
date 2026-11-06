"""Base entities. Properties read the coordinator snapshot only; no I/O."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WtcConfigEntry, controller_device_id, zone_device_id
from .const import CONF_GATEWAY_ID, DOMAIN
from .coordinator import WtcCoordinator
from .models import ControllerSnapshot, UnitSnapshot, ZoneSnapshot


class WtcEntity(CoordinatorEntity[WtcCoordinator]):
    _attr_has_entity_name = True
    _requires_live = True

    def __init__(
        self,
        entry: WtcConfigEntry,
        unit_id: int,
        zone: int | None,
        key: str,
    ) -> None:
        super().__init__(entry.runtime_data.coordinator)
        gateway_id: str = entry.data[CONF_GATEWAY_ID]
        self._unit_id = unit_id
        self._zone = zone
        zone_part = f"z{zone}" if zone is not None else "c"
        self._attr_unique_id = f"{gateway_id}_u{unit_id}_{zone_part}_{key}"
        if zone is None:
            self._attr_device_info = DeviceInfo(
                identifiers={controller_device_id(gateway_id, unit_id)}
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={zone_device_id(gateway_id, unit_id, zone)},
                manufacturer="Wavin",
                model="WTC-NET zone",
                name=f"Unit {unit_id} zone {zone}",
                via_device=controller_device_id(gateway_id, unit_id),
            )

    @property
    def unit_snapshot(self) -> UnitSnapshot | None:
        return self.coordinator.data.unit(self._unit_id) if self.coordinator.data else None

    @property
    def controller(self) -> ControllerSnapshot | None:
        unit = self.unit_snapshot
        return unit.controller if unit and unit.available else None

    @property
    def zone_snapshot(self) -> ZoneSnapshot | None:
        unit = self.unit_snapshot
        if unit is None or not unit.available or self._zone is None:
            return None
        return unit.zone(self._zone)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._zone is None:
            return self.controller is not None
        zone = self.zone_snapshot
        # A thermostat that reports "not connected" has no trustworthy data.
        return zone is not None and (not self._requires_live or zone.live is not False)


__all__ = ["DOMAIN", "WtcEntity"]
