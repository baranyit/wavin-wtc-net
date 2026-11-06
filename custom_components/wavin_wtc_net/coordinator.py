"""Coordinator wrapping ``GatewayPoller``: one cycle per refresh, read-only."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import WtcReadClient
from .const import DOMAIN
from .models import GatewaySnapshot
from .poller import GatewayPoller

if TYPE_CHECKING:
    from . import WtcConfigEntry

_LOGGER = logging.getLogger(__name__)


class WtcCoordinator(DataUpdateCoordinator[GatewaySnapshot]):
    config_entry: WtcConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WtcConfigEntry,
        client: WtcReadClient,
        interval: float,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.poller = GatewayPoller(client, interval=interval)

    async def _async_update_data(self) -> GatewaySnapshot:
        snapshot = await self.poller.run_cycle()
        if snapshot is None:  # a cycle is still running: keep the last data, do not queue
            if self.data is None:
                raise UpdateFailed("A poll cycle is already running")
            return self.data
        if not any(unit.available for unit in snapshot.units):
            errors = "; ".join(f"unit {u.unit_id}: {u.error}" for u in snapshot.units)
            raise UpdateFailed(f"No controller answered ({errors})")
        self._sync_issues(snapshot)
        return snapshot

    def _sync_issues(self, snapshot: GatewaySnapshot) -> None:
        entry_id = self.config_entry.entry_id
        for unit in snapshot.units:
            issue_id = f"excluded_{entry_id}_{unit.unit_id}"
            if unit.available and unit.excluded_points:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="excluded_points",
                    translation_placeholders={
                        "unit": str(unit.unit_id),
                        "points": ", ".join(unit.excluded_points),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
