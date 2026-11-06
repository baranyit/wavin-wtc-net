"""Test configuration.

``async def`` tests run under pytest-asyncio in auto mode (pulled in by
``pytest-homeassistant-custom-component``). The core tests stay free of Home Assistant imports;
only ``tests/ha`` uses the ``hass`` fixtures.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _enable_custom_integrations_marker() -> None:
    """Placeholder so fixtures defined here are discoverable; HA tests enable integrations."""
