"""Energy Mapper: identify the appliances of the home from the whole-house meter.

The Energy Mapper add-on does all the computation; this integration finds it
(Supervisor discovery or a manual address), keeps its settings in Home
Assistant, relays its API to the frontend through websocket commands, and
publishes the appliances it knows as devices with sensors and hourly energy
statistics.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import EnergyMapperClient
from .const import CONF_URL, DOMAIN
from .coordinator import STORAGE_VERSION, EnergyMapperConfigEntry, EnergyMapperCoordinator, EnergyMapperData
from .panel import async_register_panel, async_unregister_panel
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket commands (once, whatever the entries)."""
    async_register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> bool:
    """Connect to the add-on; retried by Home Assistant while it is unreachable."""
    client = EnergyMapperClient(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = EnergyMapperCoordinator(hass, entry, client)
    await coordinator.async_load()
    # raises ConfigEntryNotReady when the add-on does not answer
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = EnergyMapperData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> bool:
    """Unload the sensors and remove the panel; the websocket commands stay registered."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    async_unregister_panel(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> None:
    """Forget what the entities showed; the statistics are kept (a reinstall continues them)."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
