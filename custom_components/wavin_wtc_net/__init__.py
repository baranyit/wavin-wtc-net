"""Wavin WTC-NET integration for Home Assistant (read-only).

Setup, reload, diagnostics and unload never write to the device. The protocol core
(``protocol``, ``models``, ``errors``, ``transport``, ``client``, ``poller``,
``pymodbus_backend``, ``runtime``, ``unit_spec``) stays free of Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .client import DEFAULT_MAX_GAP
from .const import (
    CONF_GATEWAY_ID,
    CONF_MAX_GAP,
    CONF_POLL_INTERVAL,
    CONF_UNITS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import WtcCoordinator
from .runtime import build_client
from .transport import GatewayTransport

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR]


@dataclass(slots=True)
class WtcRuntimeData:
    coordinator: WtcCoordinator
    transport: GatewayTransport


type WtcConfigEntry = ConfigEntry[WtcRuntimeData]


def controller_device_id(gateway_id: str, unit_id: int) -> tuple[str, str]:
    return (DOMAIN, f"{gateway_id}_u{unit_id}")


def zone_device_id(gateway_id: str, unit_id: int, zone: int) -> tuple[str, str]:
    return (DOMAIN, f"{gateway_id}_u{unit_id}_z{zone}")


async def async_setup_entry(hass: HomeAssistant, entry: WtcConfigEntry) -> bool:
    units = {int(unit): int(zones) for unit, zones in entry.data[CONF_UNITS].items()}
    transport, client = build_client(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        units,
        max_gap=entry.options.get(CONF_MAX_GAP, DEFAULT_MAX_GAP),
    )
    interval = max(
        float(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
        client.minimum_poll_interval,
    )
    coordinator = WtcCoordinator(hass, entry, client, interval)
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await transport.close()
        raise
    entry.runtime_data = WtcRuntimeData(coordinator, transport)

    # Register the controller devices first so zone devices can reference them (via_device).
    registry = dr.async_get(hass)
    gateway_id = entry.data[CONF_GATEWAY_ID]
    for unit_id in sorted(units):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={controller_device_id(gateway_id, unit_id)},
            manufacturer="Wavin",
            model="WTC-NET controller",
            name=f"{entry.data[CONF_NAME]} unit {unit_id}",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WtcConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.transport.close()
    return unloaded
