"""Zone lock entities, created only for zones enabled for lock writes.

The state is the polled lock status, never the command. A lock/unlock request is queued and sent at
the start of the next poll cycle; the status coil lags the command, so the entity changes only
after a later read shows the new status (docs/open-decisions.md decision 13).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WtcConfigEntry
from .commands import CommandKind, CommandRejectedError
from .const import CONF_UNITS, DOMAIN, LOCKED
from .entity import WtcEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: WtcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    executor = entry.runtime_data.coordinator.executor
    if executor is None:
        return
    async_add_entities(
        WtcZoneLock(entry, int(unit), zone)
        for unit, zones in entry.data[CONF_UNITS].items()
        for zone in range(1, zones + 1)
        if executor.permissions.allows_lock(int(unit), zone)
    )


class WtcZoneLock(WtcEntity, LockEntity):
    _attr_translation_key = "zone_lock"

    def __init__(self, entry: WtcConfigEntry, unit_id: int, zone: int) -> None:
        super().__init__(entry, unit_id, zone, "lock")

    @property
    def is_locked(self) -> bool | None:
        zone = self.zone_snapshot
        if zone is None or zone.lock_status is None:
            return None
        return zone.lock_status == LOCKED

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        executor = self.coordinator.executor
        if executor is None or self._zone is None:
            return None
        record = executor.queue.latest(self._unit_id, self._zone, CommandKind.LOCK)
        if record is None:
            return None
        return {
            "last_command_locked": record.command.target,
            "last_command_status": record.status.value,
            "last_command_detail": record.detail,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        self._queue(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        self._queue(False)

    def _queue(self, locked: bool) -> None:
        if self._zone is None:
            return
        try:
            self.coordinator.submit_lock(self._unit_id, self._zone, locked)
        except CommandRejectedError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"reason": str(err)},
            ) from err
