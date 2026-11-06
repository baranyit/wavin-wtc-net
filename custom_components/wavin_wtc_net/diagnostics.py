"""Redacted diagnostics. Includes the raw image so hardware validation can be done offline."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import WtcConfigEntry
from .const import CONF_GATEWAY_ID

TO_REDACT = {CONF_HOST, CONF_GATEWAY_ID, "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WtcConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    transport = entry.runtime_data.transport
    stats = transport.stats
    snapshot = coordinator.data
    units: list[dict[str, Any]] = []
    for unit in snapshot.units if snapshot else ():
        units.append(
            {
                "unit_id": unit.unit_id,
                "available": unit.available,
                "error": unit.error,
                "failed_optional_reads": list(unit.failed_optional_reads),
                "excluded_points": list(unit.excluded_points),
                "raw_registers": dict(unit.raw.registers) if unit.raw else None,
                "raw_coils": dict(unit.raw.coils) if unit.raw else None,
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "poll_interval_s": coordinator.update_interval.total_seconds()
        if coordinator.update_interval
        else None,
        "minimum_poll_interval_s": coordinator.client.minimum_poll_interval,
        "requests_per_cycle": coordinator.client.requests_per_cycle,
        "first_cycle_requests": coordinator.client.first_cycle_requests,
        "hole_failures": coordinator.client.hole_failures,
        "transport": {
            "connected": stats.connected,
            "requests_started": stats.requests_started,
            "failures": stats.failures,
            "consecutive_connection_failures": stats.consecutive_connection_failures,
            "last_latency_s": stats.last_latency,
        },
        "skipped_cycles": coordinator.poller.skipped_cycles,
        "units": units,
    }
