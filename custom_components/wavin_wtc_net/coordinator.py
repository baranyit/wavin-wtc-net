"""Coordinator wrapping ``GatewayPoller``: one cycle per refresh.

A cycle only reads. With write mode on, explicit user commands are written by the command layer
after a 2 s debounce, between the requests of whatever is running (``commands.py``). Entities show
the requested value until a read says otherwise (optimistic overlay); reads decide afterwards.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .calibration import calibration_from_options
from .client import WtcReadClient
from .commands import (
    Command,
    CommandExecutor,
    CommandKind,
    CommandRecord,
    CommandRejectedError,
    RejectReason,
    validate_lock_target,
    validate_wheel_target,
    validate_zone_mode,
)
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
        executor: CommandExecutor | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.executor = executor
        self.calibration = calibration_from_options(entry.options)
        self.poller = GatewayPoller(
            client,
            interval=interval,
            on_snapshot=executor.verify if executor else None,
        )
        if executor is not None:
            executor.cycle_index = lambda: self.poller.cycles_started
            executor.on_change = self.async_update_listeners

    def optimistic(self, unit_id: int, zone: int, kind: CommandKind) -> Command | None:
        """The requested value to show until a read decides, or ``None``."""
        if self.executor is None:
            return None
        return self.executor.queue.optimistic(unit_id, zone, kind)

    async def async_shutdown(self) -> None:
        """Stop command dispatch (unwritten commands are dropped, never replayed)."""
        if self.executor is not None:
            await self.executor.close()
        await super().async_shutdown()

    def submit_wheel_target(self, unit_id: int, zone: int, target: float) -> CommandRecord:
        """Validate and queue a comfort target. No I/O here: the debounced flush writes it."""
        executor = self.executor
        if executor is None:
            raise CommandRejectedError(RejectReason.WRITE_DISABLED, "write mode is off")
        code = validate_wheel_target(
            unit_id=unit_id,
            zone=zone,
            target=target,
            snapshot=self.data,
            calibration=self.calibration,
            permissions=executor.permissions,
        )
        command = executor.queue.create(
            CommandKind.WHEEL_TARGET, unit_id, zone, target, code, executor.connection_generation
        )
        record = executor.queue.submit(command)
        executor.notify_submitted()
        self.async_update_listeners()
        return record

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

    def submit_lock(self, unit_id: int, zone: int, locked: bool) -> CommandRecord:
        """Validate and queue a lock change. No I/O here: the debounced flush writes it."""
        executor = self.executor
        if executor is None:
            raise CommandRejectedError(RejectReason.WRITE_DISABLED, "write mode is off")
        validate_lock_target(
            unit_id=unit_id, zone=zone, snapshot=self.data, permissions=executor.permissions
        )
        command = executor.queue.create(
            CommandKind.LOCK, unit_id, zone, locked, int(locked), executor.connection_generation
        )
        record = executor.queue.submit(command)
        executor.notify_submitted()
        self.async_update_listeners()
        return record

    def submit_zone_mode(
        self, unit_id: int, zone: int, kind: CommandKind, value: bool
    ) -> CommandRecord:
        """Queue a zone on/off (``ZONE_POWER``) or comfort/economy (``ZONE_PRESET``) change."""
        executor = self.executor
        if executor is None:
            raise CommandRejectedError(RejectReason.WRITE_DISABLED, "write mode is off")
        validate_zone_mode(
            unit_id=unit_id,
            zone=zone,
            kind=kind,
            snapshot=self.data,
            permissions=executor.permissions,
        )
        command = executor.queue.create(
            kind, unit_id, zone, value, int(value), executor.connection_generation
        )
        record = executor.queue.submit(command)
        executor.notify_submitted()
        self.async_update_listeners()
        return record
