"""Config, options and reconfigure flows. All read-only; no writes are possible."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback

from .client import DEFAULT_MAX_GAP, MAX_ALLOWED_GAP
from .const import (
    CONF_GATEWAY_ID,
    CONF_MAX_GAP,
    CONF_NEUTRAL_COOL,
    CONF_NEUTRAL_HEAT,
    CONF_POLL_INTERVAL,
    CONF_UNITS,
    CONF_WHEEL_STEP_COOL,
    CONF_WHEEL_STEP_HEAT,
    DEFAULT_NAME,
    DEFAULT_NEUTRAL_COOL,
    DEFAULT_NEUTRAL_HEAT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_WHEEL_STEP,
    DOMAIN,
    MAX_POLL_INTERVAL,
)
from .errors import WtcError
from .runtime import build_client, probe_endpoint
from .unit_spec import UnitSpecError, format_units, parse_units

if TYPE_CHECKING:
    from . import WtcConfigEntry

CONF_UNITS_TEXT = "units"


def _endpoint_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_UNITS_TEXT, default=defaults.get(CONF_UNITS_TEXT, "1:6")): str,
        }
    )


async def _validate(user_input: dict[str, Any]) -> tuple[dict[int, int] | None, str | None]:
    """Return (units, error_key). Performs the read-only connectivity probe."""
    try:
        units = parse_units(user_input[CONF_UNITS_TEXT])
    except UnitSpecError:
        return None, "invalid_units"
    try:
        await probe_endpoint(user_input[CONF_HOST], user_input[CONF_PORT], sorted(units))
    except WtcError:
        return units, "cannot_connect"
    return units, None


class WtcConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def _endpoint_in_use(self, host: str, port: int, *, ignore_entry_id: str | None = None) -> bool:
        return any(
            entry.data.get(CONF_HOST) == host
            and entry.data.get(CONF_PORT) == port
            and entry.entry_id != ignore_entry_id
            for entry in self._async_current_entries(include_ignore=False)
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._endpoint_in_use(user_input[CONF_HOST], user_input[CONF_PORT]):
                return self.async_abort(reason="already_configured")
            units, error = await _validate(user_input)
            if error is None and units is not None:
                gateway_id = uuid.uuid4().hex
                await self.async_set_unique_id(gateway_id)
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_GATEWAY_ID: gateway_id,
                        CONF_UNITS: {str(unit): zones for unit, zones in units.items()},
                    },
                    options={
                        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                        CONF_MAX_GAP: DEFAULT_MAX_GAP,
                        CONF_NEUTRAL_HEAT: DEFAULT_NEUTRAL_HEAT,
                        CONF_NEUTRAL_COOL: DEFAULT_NEUTRAL_COOL,
                        CONF_WHEEL_STEP_HEAT: DEFAULT_WHEEL_STEP,
                        CONF_WHEEL_STEP_COOL: DEFAULT_WHEEL_STEP,
                    },
                )
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="user",
            data_schema=_endpoint_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change endpoint or unit IDs. The gateway ID (identity) never changes."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._endpoint_in_use(
                user_input[CONF_HOST], user_input[CONF_PORT], ignore_entry_id=entry.entry_id
            ):
                return self.async_abort(reason="already_configured")
            units, error = await _validate(user_input)
            if error is None and units is not None:
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_NAME],
                    data_updates={
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_UNITS: {str(unit): zones for unit, zones in units.items()},
                    },
                )
            errors["base"] = error or "unknown"
        defaults = user_input or {
            CONF_NAME: entry.data[CONF_NAME],
            CONF_HOST: entry.data[CONF_HOST],
            CONF_PORT: entry.data[CONF_PORT],
            CONF_UNITS_TEXT: format_units({int(u): z for u, z in entry.data[CONF_UNITS].items()}),
        }
        return self.async_show_form(
            step_id="reconfigure", data_schema=_endpoint_schema(defaults), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: WtcConfigEntry) -> WtcOptionsFlow:
        return WtcOptionsFlow()


class WtcOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        units = {int(u): z for u, z in entry.data[CONF_UNITS].items()}
        _, client = build_client(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            units,
            max_gap=(user_input or entry.options).get(CONF_MAX_GAP, DEFAULT_MAX_GAP),
        )
        minimum = client.minimum_poll_interval
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_POLL_INTERVAL] < minimum:
                errors[CONF_POLL_INTERVAL] = "interval_too_low"
            else:
                return self.async_create_entry(data=user_input)
        current = user_input or entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=max(current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL), minimum),
                ): vol.All(int, vol.Range(min=1, max=MAX_POLL_INTERVAL)),
                vol.Required(
                    CONF_MAX_GAP, default=current.get(CONF_MAX_GAP, DEFAULT_MAX_GAP)
                ): vol.All(int, vol.Range(min=0, max=MAX_ALLOWED_GAP)),
                vol.Required(
                    CONF_NEUTRAL_HEAT, default=current.get(CONF_NEUTRAL_HEAT, DEFAULT_NEUTRAL_HEAT)
                ): vol.All(vol.Coerce(float), vol.Range(min=5, max=35)),
                vol.Required(
                    CONF_NEUTRAL_COOL, default=current.get(CONF_NEUTRAL_COOL, DEFAULT_NEUTRAL_COOL)
                ): vol.All(vol.Coerce(float), vol.Range(min=5, max=35)),
                vol.Required(
                    CONF_WHEEL_STEP_HEAT,
                    default=current.get(CONF_WHEEL_STEP_HEAT, DEFAULT_WHEEL_STEP),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=2)),
                vol.Required(
                    CONF_WHEEL_STEP_COOL,
                    default=current.get(CONF_WHEEL_STEP_COOL, DEFAULT_WHEEL_STEP),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=2)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"minimum": str(int(minimum))},
        )
