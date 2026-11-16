"""Energy Mapper: identify the appliances of the home from the whole-house meter.

The Energy Mapper add-on does all the computation; this integration finds it
(Supervisor discovery or a manual address), keeps its settings in Home
Assistant and relays its API to the frontend through websocket commands.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import EnergyMapperClient
from .const import CONF_URL, DOMAIN
from .coordinator import EnergyMapperConfigEntry, EnergyMapperCoordinator, EnergyMapperData
from .panel import async_register_panel, async_unregister_panel
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket commands (once, whatever the entries)."""
    async_register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> bool:
    """Connect to the add-on; retried by Home Assistant while it is unreachable."""
    client = EnergyMapperClient(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = EnergyMapperCoordinator(hass, entry, client)
    # raises ConfigEntryNotReady when the add-on does not answer
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = EnergyMapperData(client=client, coordinator=coordinator)
    await async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> bool:
    """Remove the panel; no platform, the websocket commands stay registered."""
    async_unregister_panel(hass)
    return True
