"""Polarity of the raw status/control bits (single source of truth).

Confirmed by the vendor BMS document v1.9 and the first hardware run (2026-11-07).
Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from typing import Final

ON_OFF_ON: Final = True
COMFORT: Final = True
COOLING: Final = True
LOCKED: Final = True
LIVE_CONNECTED: Final = True
