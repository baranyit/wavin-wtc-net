"""Home Assistant test fixtures: a 6+4 mock device behind the real integration code."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavin_wtc_net import runtime
from custom_components.wavin_wtc_net.const import (
    CONF_GATEWAY_ID,
    CONF_MAX_GAP,
    CONF_POLL_INTERVAL,
    CONF_UNITS,
    DOMAIN,
)

from ..mock_wtc import MockWtcBackend, VirtualClock

GATEWAY_ID = "gw-test-0001"
HOST = "192.0.2.10"  # TEST-NET-1, never reached: the backend is the in-process mock


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let HA load custom_components/wavin_wtc_net in every HA test."""


@pytest.fixture
def mock_device() -> Iterator[MockWtcBackend]:
    """Patch the client factory so the integration talks to the in-process 6+4 mock."""
    clock = VirtualClock()
    backend = MockWtcBackend.for_profile(clock)
    factory: Callable[..., Any] = partial(runtime.build_client, backend=backend, clock=clock)
    with (
        patch("custom_components.wavin_wtc_net.build_client", factory),
        patch("custom_components.wavin_wtc_net.config_flow.build_client", factory),
    ):
        yield backend


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=GATEWAY_ID,
        data={
            "name": "Home",
            "host": HOST,
            "port": 502,
            CONF_GATEWAY_ID: GATEWAY_ID,
            CONF_UNITS: {"1": 6, "2": 4},
        },
        options={CONF_POLL_INTERVAL: 60, CONF_MAX_GAP: 8},
    )
