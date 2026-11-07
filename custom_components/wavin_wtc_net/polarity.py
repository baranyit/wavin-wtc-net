"""Polarity of the raw status/control bits (single source of truth).

Confirmed by the vendor BMS document v1.9, HomeGate and the first hardware run
(``docs/open-decisions.md`` decision 5, ``docs/hardware-validation-2026-11-07.md``).
Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from typing import Final

ON_OFF_ON: Final = True
COMFORT: Final = True
COOLING: Final = True
LOCKED: Final = True
LIVE_CONNECTED: Final = True
