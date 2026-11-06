"""Constants for the Home Assistant layer.

Polarity of the raw coils (confirmed by the vendor BMS document v1.9 and HomeGate, see
``docs/open-decisions.md`` decision 5): ``on_off`` True = on, ``comfort_economy`` True =
comfort, ``heat_cool`` True = cooling, lock True = locked, ``live`` True = connected.
These are fixed, not user-configurable.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "wavin_wtc_net"

CONF_GATEWAY_ID: Final = "gateway_id"
CONF_UNITS: Final = "units"  # {"1": 6, "2": 4}: Modbus unit ID -> number of zones
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_MAX_GAP: Final = "max_gap"

DEFAULT_NAME: Final = "Wavin WTC-NET"
DEFAULT_PORT: Final = 502
DEFAULT_POLL_INTERVAL: Final = 60
MAX_POLL_INTERVAL: Final = 300

ON_OFF_ON: Final = True
COMFORT: Final = True
COOLING: Final = True
LOCKED: Final = True
LIVE_CONNECTED: Final = True
