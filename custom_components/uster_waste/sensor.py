"""Sensor platform for Uster (CH) waste collection schedules.

Configuration (configuration.yaml):

  sensor:
    - platform: uster_waste
      street: "Bahnhofstrasse 1 - 17"   # exact name as shown on uster.ch
      count: 5                           # optional, default 5
      name: "Kehricht"                   # optional, overrides entity name
      scan_interval: 43200              # optional, seconds (default 12 h)
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "uster_waste"
BASE_URL = "https://www.uster.ch/abfallstrassenabschnitt"

CONF_STREET = "street"
CONF_COUNT = "count"

DEFAULT_NAME = "Uster Waste"
DEFAULT_COUNT = 5
DEFAULT_SCAN_INTERVAL = timedelta(hours=12)

# Maps partial collection-type strings to MDI icons
COLLECTION_ICONS: dict[str, str] = {
    "Papiersammlung":       "mdi:newspaper",
    "Kartonsammlung":       "mdi:package-variant",
    "Häckseldienst":        "mdi:tree",
    "Textilsammlung":       "mdi:hanger",
    "Sonderabfallsammlung": "mdi:biohazard",
    "Metallabfuhr":         "mdi:wrench",
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_STREET): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COUNT, default=DEFAULT_COUNT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=50)
        ),
    }
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Uster Waste sensor from a config entry (UI flow)."""
    street: str = entry.data["street"]
    count: int = entry.data.get("count", DEFAULT_COUNT)

    session = async_get_clientsession(hass)
    coordinator = UsterWasteCoordinator(hass, session, street, DEFAULT_SCAN_INTERVAL)
    await coordinator.async_refresh()

    async_add_entities([UsterWasteSensor(coordinator, street, street, count)], True)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Uster Waste sensor."""
    street: str = config[CONF_STREET]
    count: int = config[CONF_COUNT]
    name: str = config[CONF_NAME]

    # Honour optional scan_interval override from configuration.yaml
    scan_interval: timedelta = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    session = async_get_clientsession(hass)
    coordinator = UsterWasteCoordinator(hass, session, street, scan_interval)

    # Perform an initial refresh so the entity has data on startup
    await coordinator.async_refresh()

    async_add_entities([UsterWasteSensor(coordinator, name, street, count)], True)


# ---------------------------------------------------------------------------
# Data coordinator
# ---------------------------------------------------------------------------

class UsterWasteCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Fetch and cache waste collection data for one street."""

    def __init__(
        self,
        hass: HomeAssistant,
        session,
        street: str,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{street}",
            update_interval=update_interval,
        )
        self._session = session
        self._street = street

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            return await _fetch_collections(self._session, self._street)
        except Exception as err:
            raise UpdateFailed(f"Error fetching Uster waste data: {err}") from err


# ---------------------------------------------------------------------------
# HTTP + parsing
# ---------------------------------------------------------------------------

async def _fetch_collections(session, street: str) -> list[dict[str, Any]]:
    """
    1. GET the form page  → extract fresh CSRF token + street-name→ID map.
    2. GET the results page → parse the collection table.
    Returns a list of dicts sorted by date, future dates only:
      { "type": str, "date": "YYYY-MM-DD", "days_until": int }
    """

    # -- Step 1: form page --------------------------------------------------
    async with session.get(BASE_URL) as resp:
        resp.raise_for_status()
        html: str = await resp.text()

    # CSRF token
    token_match = re.search(
        r'name="strassenabschnitt\[_token\]"\s+value="([^"]+)"', html
    )
    if not token_match:
        raise ValueError("CSRF token not found in form page — site layout may have changed")
    token = token_match.group(1)

    # Street-name → section-ID map  (option value="5145">Bahnhofstrasse…)
    street_map: dict[str, str] = {
        label.strip(): sid
        for sid, label in re.findall(r'<option value="(\d+)">([^<]+)</option>', html)
    }

    # Case-insensitive lookup
    street_id: str | None = next(
        (sid for label, sid in street_map.items() if label.lower() == street.lower()),
        None,
    )
    if not street_id:
        sample = ", ".join(sorted(street_map)[:8])
        raise ValueError(
            f"Street {street!r} not found on uster.ch. "
            f"First few available streets: {sample} …"
        )

    _LOGGER.debug("Resolved %r → strassenabschnittId=%s", street, street_id)

    # -- Step 2: results page -----------------------------------------------
    params = {
        "strassenabschnitt[_token]": token,
        "strassenabschnitt[strassenabschnittId]": street_id,
    }
    async with session.get(BASE_URL, params=params) as resp:
        resp.raise_for_status()
        html = await resp.text()

    # Each table row:
    #   <td>Papiersammlung</td>
    #   <td data-order="2026-03-07T00:00:00+01:00 07:00"><a …>…</a></td>
    rows = re.findall(
        r"<td>([^<]+)</td>\s*<td[^>]+data-order=\"([^\"]+)\"",
        html,
    )

    today = date.today()
    collections: list[dict[str, Any]] = []

    for raw_type, data_order in rows:
        # data-order: "2026-03-07T00:00:00+01:00 07:00"  (ISO part + time label)
        iso_part = data_order.split(" ")[0]
        try:
            dt = datetime.fromisoformat(iso_part)
        except ValueError:
            _LOGGER.warning("Cannot parse date %r for collection type %r", data_order, raw_type)
            continue

        collection_date = dt.date()
        if collection_date < today:
            continue  # skip past dates

        collections.append(
            {
                "type": raw_type.strip(),
                "date": collection_date.isoformat(),
                "days_until": (collection_date - today).days,
            }
        )

    collections.sort(key=lambda x: x["date"])
    _LOGGER.debug("Fetched %d upcoming collection(s) for %r", len(collections), street)
    return collections


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------

class UsterWasteSensor(CoordinatorEntity[UsterWasteCoordinator], SensorEntity):
    """Sensor showing the next waste collection for a given street in Uster."""

    def __init__(
        self,
        coordinator: UsterWasteCoordinator,
        name: str,
        street: str,
        count: int,
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = name
        self._street = street
        self._count = count
        self._attr_unique_id = f"{DOMAIN}_{re.sub(r'[^a-z0-9]', '_', street.lower())}"

    # -- State ---------------------------------------------------------------

    @property
    def native_value(self) -> str | None:
        """State = type of the next upcoming collection."""
        data = self.coordinator.data
        return data[0]["type"] if data else None

    # -- Icon ----------------------------------------------------------------

    @property
    def icon(self) -> str:
        data = self.coordinator.data
        if data:
            col_type = data[0]["type"]
            for keyword, icon in COLLECTION_ICONS.items():
                if keyword in col_type:
                    return icon
        return "mdi:trash-can-outline"

    # -- Attributes ----------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """
        next_date   – ISO date of the next collection
        days_until  – days until the next collection (0 = today)
        upcoming    – list of the next `count` collections
        street      – configured street name
        """
        data = self.coordinator.data or []
        upcoming = data[: self._count]
        next_item = upcoming[0] if upcoming else {}
        return {
            "street": self._street,
            "next_date": next_item.get("date"),
            "days_until": next_item.get("days_until"),
            "upcoming": upcoming,
        }
