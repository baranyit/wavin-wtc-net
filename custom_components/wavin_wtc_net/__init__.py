"""Wavin WTC-NET integration for Home Assistant.

Read-only by default. Setup, reload, diagnostics and unload never write to the device. Only with
write mode enabled in the options, for explicitly selected zones, a queued wheel command is sent
at the start of the next poll cycle. The protocol core (``protocol``, ``models``, ``errors``,
``transport``, ``client``, ``poller``, ``commands``, ``runtime``, ...) stays free of Home
Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .client import DEFAULT_MAX_GAP
from .commands import CommandExecutor, CommandPermissions, CommandQueue
from .const import (
    COMMAND_TTL_FACTOR,
    CONF_GATEWAY_ID,
    CONF_MAX_GAP,
    CONF_POLL_INTERVAL,
    CONF_UNITS,
    CONF_WRITABLE_ZONES,
    CONF_WRITE_ENABLED,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import WtcCoordinator
from .runtime import build_client
from .transport import AsyncioClock, GatewayTransport
from .write_transport import WritableGatewayTransport

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


def permissions_from_options(options: Mapping[str, Any]) -> CommandPermissions:
    zones: set[tuple[int, int]] = set()
    for item in options.get(CONF_WRITABLE_ZONES, []):
        unit_text, _, zone_text = str(item).partition(":")
        if unit_text.isdigit() and zone_text.isdigit():
            zones.add((int(unit_text), int(zone_text)))
    return CommandPermissions(bool(options.get(CONF_WRITE_ENABLED, False)), frozenset(zones))


async def async_setup_entry(hass: HomeAssistant, entry: WtcConfigEntry) -> bool:
    units = {int(unit): int(zones) for unit, zones in entry.data[CONF_UNITS].items()}
    permissions = permissions_from_options(entry.options)
    writable = permissions.write_enabled and bool(permissions.zones)
    transport, client = build_client(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        units,
        max_gap=entry.options.get(CONF_MAX_GAP, DEFAULT_MAX_GAP),
        writable=writable,
    )
    interval = max(
        float(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
        client.minimum_poll_interval,
    )
    executor: CommandExecutor | None = None
    if writable:
        assert isinstance(transport, WritableGatewayTransport)
        queue = CommandQueue(AsyncioClock(), interval * COMMAND_TTL_FACTOR)
        executor = CommandExecutor(transport, queue, permissions)
    coordinator = WtcCoordinator(hass, entry, client, interval, executor)
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
