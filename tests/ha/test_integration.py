"""Home Assistant layer: flows, setup, entities, availability, diagnostics, translations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.wavin_wtc_net.const import (
    CONF_GATEWAY_ID,
    CONF_MAX_GAP,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.wavin_wtc_net.errors import WtcConnectionError
from custom_components.wavin_wtc_net.protocol import Table

from ..mock_wtc import Fault, MockWtcBackend
from .conftest import GATEWAY_ID, HOST

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "wavin_wtc_net"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# -- setup and entities ---------------------------------------------------------------------


async def test_setup_creates_ten_climate_devices_without_writes(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.LOADED
    assert len(hass.states.async_entity_ids("climate")) == 10
    assert mock_device.writes == []
    assert len(mock_device.records) == 29  # first cycle includes the static points
    assert {r.op for r in mock_device.records} <= {"read_holding_registers", "read_coils"}


async def test_target_temperature_uses_the_calibration(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{GATEWAY_ID}_u2_z3_target_temperature"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 21.5  # code 5, neutral 22, step 0.5 (comfort)
    assert mock_device.writes == []


async def test_custom_wheel_calibration_changes_the_target(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "neutral_heat": 20.0, "wheel_step_heat": 1.0},
    )
    await _setup(hass, mock_entry)
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{GATEWAY_ID}_u2_z3_target_temperature"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 19.0  # 20 + (5 - 6) x 1.0


async def test_options_accept_calibration_and_keep_defaults(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    ok = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 60,
            CONF_MAX_GAP: 8,
            "neutral_heat": 21.5,
            "neutral_cool": 25.0,
            "wheel_step_heat": 0.5,
            "wheel_step_cool": 1.0,
        },
    )
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options["neutral_cool"] == 25.0
    assert mock_entry.options["wheel_step_cool"] == 1.0


async def test_identity_is_gateway_id_based_not_entry_id(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, mock_entry.entry_id)
    assert entries
    for entity in entries:
        assert entity.unique_id.startswith(f"{GATEWAY_ID}_u")
        assert mock_entry.entry_id not in entity.unique_id
    dev_reg = dr.async_get(hass)
    zone_device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{GATEWAY_ID}_u2_z4")})
    controller = dev_reg.async_get_device(identifiers={(DOMAIN, f"{GATEWAY_ID}_u2")})
    assert zone_device is not None and controller is not None
    assert zone_device.via_device_id == controller.id


async def test_zone_state_comes_from_the_snapshot(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_u2_z3_climate")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "heat"
    assert state.attributes["current_temperature"] == 21.7
    assert state.attributes["hvac_action"] == "heating"
    assert "temperature" not in state.attributes or state.attributes["temperature"] is None


async def test_partial_unit_failure_marks_only_that_unit_unavailable(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_device.faults.append(
        Fault("exception", unit_id=2, table=Table.HOLDING_REGISTER, address=4121, remaining=99)
    )
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.LOADED
    ent_reg = er.async_get(hass)
    ok = ent_reg.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_u1_z1_climate")
    bad = ent_reg.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_u2_z1_climate")
    assert ok and bad
    assert hass.states.get(ok).state != "unavailable"  # type: ignore[union-attr]
    assert hass.states.get(bad).state == "unavailable"  # type: ignore[union-attr]
    assert mock_device.writes == []


async def test_all_units_down_retries_setup(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_device.offline_units = {1, 2}
    mock_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_makes_no_writes(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.NOT_LOADED
    assert mock_device.writes == []


# -- config flow ----------------------------------------------------------------------------


async def test_user_flow_creates_entry_with_generated_gateway_id(
    hass: HomeAssistant, mock_device: MockWtcBackend
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.wavin_wtc_net.config_flow.probe_endpoint") as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "Home", "host": HOST, "port": 502, "units": "1:6, 2:4"},
        )
        await hass.async_block_till_done()
    probe.assert_awaited_once_with(HOST, 502, [1, 2])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["units"] == {"1": 6, "2": 4}
    assert len(result["data"][CONF_GATEWAY_ID]) == 32
    assert result["options"] == {
        CONF_POLL_INTERVAL: 60,
        CONF_MAX_GAP: 8,
        "neutral_heat": 22.0,
        "neutral_cool": 26.0,
        "wheel_step_heat": 0.5,
        "wheel_step_cool": 0.5,
        "write_enabled": False,
        "writable_zones": [],
        "lock_zones": [],
        "mode_zones": [],
    }


async def test_user_flow_rejects_bad_units_and_unreachable_endpoint(
    hass: HomeAssistant, mock_device: MockWtcBackend
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Home", "host": HOST, "port": 502, "units": "1-6"}
    )
    assert bad["type"] is FlowResultType.FORM and bad["errors"] == {"base": "invalid_units"}
    with patch(
        "custom_components.wavin_wtc_net.config_flow.probe_endpoint",
        side_effect=WtcConnectionError("nope"),
    ):
        down = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Home", "host": HOST, "port": 502, "units": "1:6"}
        )
    assert down["type"] is FlowResultType.FORM and down["errors"] == {"base": "cannot_connect"}


async def test_duplicate_endpoint_is_aborted(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Other", "host": HOST, "port": 502, "units": "1:2"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reconfigure_keeps_gateway_identity(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    result = await mock_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with (
        patch("custom_components.wavin_wtc_net.config_flow.probe_endpoint"),
        patch("custom_components.wavin_wtc_net.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "Home", "host": "192.0.2.11", "port": 502, "units": "1:6, 2:4"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert mock_entry.data["host"] == "192.0.2.11"
    assert mock_entry.data[CONF_GATEWAY_ID] == GATEWAY_ID


# -- options flow ---------------------------------------------------------------------------


async def test_options_reject_interval_below_derived_minimum(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["description_placeholders"] == {"minimum": "44"}
    low = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 30, CONF_MAX_GAP: 8}
    )
    assert low["type"] is FlowResultType.FORM
    assert low["errors"] == {CONF_POLL_INTERVAL: "interval_too_low"}
    ok = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 90, CONF_MAX_GAP: 8}
    )
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options[CONF_POLL_INTERVAL] == 90


async def test_gap_zero_raises_the_minimum_interval(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    low = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_MAX_GAP: 0}
    )
    assert low["errors"] == {CONF_POLL_INTERVAL: "interval_too_low"}  # 41 requests -> 62 s


# -- diagnostics and translations -----------------------------------------------------------


async def test_diagnostics_are_redacted_and_carry_the_raw_image(
    hass: HomeAssistant,
    hass_client: object,
    mock_device: MockWtcBackend,
    mock_entry: MockConfigEntry,
) -> None:
    await _setup(hass, mock_entry)
    data = await get_diagnostics_for_config_entry(hass, hass_client, mock_entry)  # type: ignore[arg-type]
    text = json.dumps(data)
    assert HOST not in text and GATEWAY_ID not in text
    assert data["first_cycle_requests"] == 29 and data["minimum_poll_interval_s"] == 44.0
    assert data["units"][0]["raw_registers"] and data["units"][0]["raw_coils"]


def _keys(node: object, prefix: str = "") -> set[str]:
    if isinstance(node, dict):
        return {k for key, v in node.items() for k in _keys(v, f"{prefix}{key}.")}
    return {prefix}


def test_every_english_string_has_a_hungarian_translation() -> None:
    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((PACKAGE / "translations" / "en.json").read_text(encoding="utf-8"))
    hungarian = json.loads((PACKAGE / "translations" / "hu.json").read_text(encoding="utf-8"))
    assert strings == english
    assert _keys(english) == _keys(hungarian)


# -- write mode ------------------------------------------------------------------------------


async def _wait_for_writes(hass: HomeAssistant, device: MockWtcBackend, count: int) -> None:
    """Let the (shortened) debounce run until ``count`` writes reached the mock device."""
    for _ in range(100):
        if len(device.writes) >= count:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()
    assert len(device.writes) == count


def _climate_id(hass: HomeAssistant, unit: int, zone: int) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "climate", DOMAIN, f"{GATEWAY_ID}_u{unit}_z{zone}_climate"
    )
    assert entity_id is not None
    return entity_id


async def test_write_mode_is_off_by_default_no_target_control(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    state = hass.states.get(_climate_id(hass, 2, 2))
    assert state is not None
    assert state.attributes["supported_features"] == 0
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": state.entity_id, "temperature": 22.0},
            blocking=True,
        )
    assert mock_device.writes == []


async def test_set_temperature_is_shown_at_once_written_after_the_debounce_and_confirmed(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "write_enabled": True, "writable_zones": ["2:2"]},
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _climate_id(hass, 2, 2)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["temperature"] == 22.5  # mock: code 7, neutral 22, step 0.5
    other = hass.states.get(_climate_id(hass, 2, 3))
    assert other is not None and other.attributes["supported_features"] == 0  # not enabled

    requests_before = len(mock_device.records)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity_id, "temperature": 22.0}, blocking=True
    )
    assert len(mock_device.records) == requests_before  # queued only: no traffic yet
    assert mock_device.writes == []
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["temperature"] == 22.0  # optimistic: the request is shown at once
    assert state.attributes["last_command_status"] == "queued"

    await _wait_for_writes(hass, mock_device, 1)  # written after the debounce
    assert mock_device.writes == [("write_register", 2, 4605, 6)]
    assert [r.op for r in mock_device.records[requests_before:]] == ["write_register"]  # no re-read
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["temperature"] == 22.0
    assert state.attributes["last_command_status"] == "sent"

    await mock_entry.runtime_data.coordinator.async_refresh()  # the next cycle decides
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["temperature"] == 22.0  # now from the read
    assert state.attributes["last_command_status"] == "confirmed"

    await mock_entry.runtime_data.coordinator.async_refresh()
    assert len(mock_device.writes) == 1  # never replayed


async def test_rejected_targets_never_reach_the_device(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "write_enabled": True, "writable_zones": ["2:2"]},
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _climate_id(hass, 2, 2)
    for target in (22.25, 30.0):  # not a whole step / outside the wheel range
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": target},
                blocking=True,
            )
    await mock_entry.runtime_data.coordinator.async_refresh()
    assert mock_device.writes == []


async def test_options_require_a_zone_when_writing_is_enabled(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_POLL_INTERVAL: 60, CONF_MAX_GAP: 8, "write_enabled": True, "writable_zones": []},
    )
    assert bad["errors"] == {"writable_zones": "no_zone_selected"}
    ok = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 60,
            CONF_MAX_GAP: 8,
            "write_enabled": True,
            "writable_zones": ["1:1"],
        },
    )
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert mock_device.writes == []  # configuring writing writes nothing


async def test_lock_entities_only_for_lock_zones_and_changes_are_queued(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "write_enabled": True, "lock_zones": ["1:4"]},
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_entity_ids("lock")) == 1
    entity_id = hass.states.async_entity_ids("lock")[0]
    assert hass.states.get(entity_id).state == "unlocked"  # type: ignore[union-attr]
    # lock permission does not give a settable climate target
    climate = hass.states.get(_climate_id(hass, 1, 4))
    assert climate is not None and climate.attributes["supported_features"] == 0

    requests_before = len(mock_device.records)
    await hass.services.async_call("lock", "lock", {"entity_id": entity_id}, blocking=True)
    assert len(mock_device.records) == requests_before and mock_device.writes == []
    assert hass.states.get(entity_id).state == "locked"  # type: ignore[union-attr]  # optimistic

    await _wait_for_writes(hass, mock_device, 1)
    assert mock_device.writes == [("write_coil", 1, 4659, True)]
    await mock_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None and state.state == "locked"
    assert state.attributes["last_command_status"] == "confirmed"


async def test_no_lock_entities_when_writing_is_off(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_entry)
    assert hass.states.async_entity_ids("lock") == []


async def test_preset_and_power_are_queued_for_mode_zones_only(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "write_enabled": True, "mode_zones": ["2:2"]},
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _climate_id(hass, 2, 2)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["preset_mode"] == "comfort"
    other = hass.states.get(_climate_id(hass, 2, 3))
    assert other is not None and "preset_modes" not in other.attributes

    before = len(mock_device.records)
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": entity_id, "preset_mode": "eco"}, blocking=True
    )
    assert len(mock_device.records) == before and mock_device.writes == []
    assert hass.states.get(entity_id).attributes["preset_mode"] == "eco"  # type: ignore[union-attr]
    await _wait_for_writes(hass, mock_device, 1)
    await mock_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_device.writes == [("write_coil", 2, 4613, False)]
    assert hass.states.get(entity_id).attributes["preset_mode"] == "eco"  # type: ignore[union-attr]

    await hass.services.async_call("climate", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "off"  # type: ignore[union-attr]  # optimistic
    await _wait_for_writes(hass, mock_device, 2)
    await mock_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_device.writes[-1] == ("write_coil", 2, 4612, False)
    assert hass.states.get(entity_id).state == "off"  # type: ignore[union-attr]
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "heat"}, blocking=True
    )
    await _wait_for_writes(hass, mock_device, 3)
    await mock_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_device.writes[-1] == ("write_coil", 2, 4612, True)
    assert hass.states.get(entity_id).state == "heat"  # type: ignore[union-attr]
    with pytest.raises(ServiceValidationError):  # heat/cool itself is never written
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "cool"}, blocking=True
        )
    heat_cool_addresses = {4614}  # zone 2 heat/cool control coil
    assert not [w for w in mock_device.writes if w[2] in heat_cool_addresses | {4127, 4128, 4129}]


async def test_optimistic_value_reverts_to_the_real_one_when_the_device_ignores_the_write(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "write_enabled": True, "writable_zones": ["2:2"]},
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _climate_id(hass, 2, 2)
    mock_device.ignore_writes = True  # accepted by the transport, never applied
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity_id, "temperature": 22.0}, blocking=True
    )
    assert hass.states.get(entity_id).attributes["temperature"] == 22.0  # type: ignore[union-attr]
    await _wait_for_writes(hass, mock_device, 1)
    assert hass.states.get(entity_id).attributes["temperature"] == 22.0  # type: ignore[union-attr]
    await mock_entry.runtime_data.coordinator.async_refresh()  # first cycle after the write
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["temperature"] == 22.5  # the real value is back
    assert state.attributes["last_command_status"] == "sent"  # still awaited, never rewritten
    await mock_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["last_command_status"] == "timeout"  # type: ignore[union-attr]
    assert len(mock_device.writes) == 1


async def test_unload_drops_waiting_commands_without_a_write(
    hass: HomeAssistant, mock_device: MockWtcBackend, mock_entry: MockConfigEntry
) -> None:
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, "write_enabled": True, "writable_zones": ["2:2"]},
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": _climate_id(hass, 2, 2), "temperature": 22.0},
        blocking=True,
    )
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await asyncio.sleep(0.8)  # longer than debounce and max wait
    assert mock_device.writes == []
