"""Constants for the Home Assistant layer.

Polarity of the raw coils (confirmed by the vendor BMS document v1.9 and HomeGate, see
``docs/open-decisions.md`` decision 5): ``on_off`` True = on, ``comfort_economy`` True =
comfort, ``heat_cool`` True = cooling, lock True = locked, ``live`` True = connected.
These are fixed, not user-configurable.
"""

from __future__ import annotations

from typing import Final

from .polarity import COMFORT, COOLING, LIVE_CONNECTED, LOCKED, ON_OFF_ON

DOMAIN: Final = "wavin_wtc_net"

CONF_GATEWAY_ID: Final = "gateway_id"
CONF_UNITS: Final = "units"  # {"1": 6, "2": 4}: Modbus unit ID -> number of zones
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_MAX_GAP: Final = "max_gap"
CONF_NEUTRAL_HEAT: Final = "neutral_heat"
CONF_NEUTRAL_COOL: Final = "neutral_cool"
CONF_WHEEL_STEP_HEAT: Final = "wheel_step_heat"
CONF_WHEEL_STEP_COOL: Final = "wheel_step_cool"
CONF_WRITE_ENABLED: Final = "write_enabled"
CONF_WRITABLE_ZONES: Final = "writable_zones"  # ["2:2", ...] = "unit:zone"

DEFAULT_NAME: Final = "Wavin WTC-NET"
DEFAULT_PORT: Final = 502
DEFAULT_POLL_INTERVAL: Final = 60
MAX_POLL_INTERVAL: Final = 300
DEFAULT_NEUTRAL_HEAT: Final = 22.0
DEFAULT_NEUTRAL_COOL: Final = 26.0
DEFAULT_WHEEL_STEP: Final = 0.5
COMMAND_TTL_FACTOR: Final = 2.0  # a queued command expires after this many poll intervals

__all__ = ["COMFORT", "COOLING", "LIVE_CONNECTED", "LOCKED", "ON_OFF_ON"]
