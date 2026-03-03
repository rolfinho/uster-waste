"""Config flow for Uster Waste Collection."""
from __future__ import annotations

import re
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .sensor import BASE_URL, DEFAULT_COUNT

_LOGGER = logging.getLogger(__name__)

DOMAIN = "uster_waste"


async def _get_street_map(session) -> dict[str, str]:
    """Fetch the form page and return {street_label: street_id}."""
    async with session.get(BASE_URL) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return {
        label.strip(): sid
        for sid, label in re.findall(r'<option value="(\d+)">([^<]+)</option>', html)
    }


class UsterWasteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Uster Waste Collection."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            street: str = user_input["street"].strip()

            try:
                session = async_get_clientsession(self.hass)
                street_map = await _get_street_map(session)
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                match = next(
                    (sid for label, sid in street_map.items()
                     if label.lower() == street.lower()),
                    None,
                )
                if match is None:
                    errors["street"] = "street_not_found"
                else:
                    await self.async_set_unique_id(
                        re.sub(r"[^a-z0-9]", "_", street.lower())
                    )
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=street, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("street"): str,
                    vol.Optional("count", default=DEFAULT_COUNT): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=50)
                    ),
                }
            ),
            errors=errors,
        )
