"""Energy Mapper: identify the appliances of the home from the whole-house meter.

The Energy Mapper add-on does all the computation; this integration finds it
(Supervisor discovery or a manual address), keeps its settings in Home
Assistant, relays its API to the frontend through websocket commands, and
publishes the appliances it knows as devices with sensors and hourly energy
statistics.
"""

from __future__ import annotations

import inspect
import logging
from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import EnergyMapperClient
from .appliances import SUBENTRY_TYPE, Appliance, appliance_of_key
from .const import CONF_URL, DOMAIN
from .coordinator import STORAGE_VERSION, EnergyMapperConfigEntry, EnergyMapperCoordinator, EnergyMapperData
from .panel import async_register_panel, async_unregister_panel
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)

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
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await async_register_panel(hass)
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> None:
    """Subentries added, edited or deleted (or the options changed)."""
    await entry.runtime_data.coordinator.async_subentries_changed()


async def async_migrate_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> bool:
    """1.1 -> 1.2: the appliances published by 0.2 (one device each, #12)
    become config subentries. Their devices and entities move into them:
    entity ids, unique ids and statistic ids do not change."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        count = _migrate_appliances(hass, entry, await _stored_appliances(hass, entry))
        hass.config_entries.async_update_entry(entry, minor_version=2)
        _LOGGER.info("Energy Mapper: %d appliance(s) moved to config subentries", count)
    return True


async def _stored_appliances(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> dict[str, str]:
    """key -> name of the appliances 0.2 kept in its store."""
    data = await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_load() or {}
    out: dict[str, str] = {}
    for raw in data.get("appliances") or []:
        try:
            out[f"{'load' if raw['kind'] == 'switched_load' else 'signature'}:{raw['source_id']}"] = raw[
                "name"
            ]
        except (KeyError, TypeError):
            continue
    return out


def _move_device(
    registry: dr.DeviceRegistry, device: dr.DeviceEntry, entry_id: str, subentry_id: str
) -> None:
    if "new_config_subentry_id" in inspect.signature(registry.async_update_device).parameters:
        registry.async_update_device(device.id, new_config_subentry_id=subentry_id)
    else:  # Home Assistant before devices had a single subentry
        registry.async_update_device(
            device.id, add_config_entry_id=entry_id, add_config_subentry_id=subentry_id
        )
        registry.async_update_device(
            device.id, remove_config_entry_id=entry_id, remove_config_subentry_id=None
        )


def _migrate_appliances(hass: HomeAssistant, entry: EnergyMapperConfigEntry, stored: dict[str, str]) -> int:
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    devices = {
        key: device
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        for domain, key in device.identifiers
        if domain == DOMAIN and key != entry.entry_id
    }
    taken = {s.unique_id for s in entry.subentries.values()}
    count = 0
    for key in sorted(set(devices) | set(stored)):
        device = devices.get(key)
        name = (device.name if device else None) or stored.get(key) or key
        appliance: Appliance | None = appliance_of_key(key, name)
        if appliance is None or key in taken:
            continue
        subentry = ConfigSubentry(
            data=MappingProxyType(appliance.subentry_data()),
            subentry_type=SUBENTRY_TYPE,
            title=name,
            unique_id=key,
        )
        hass.config_entries.async_add_subentry(entry, subentry)
        count += 1
        if device is None:
            continue
        _move_device(dev_reg, device, entry.entry_id, subentry.subentry_id)
        for entity in er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
            ent_reg.async_update_entity(entity.entity_id, config_subentry_id=subentry.subentry_id)
    return count


async def async_unload_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> bool:
    """Unload the sensors and remove the panel; the websocket commands stay registered."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    async_unregister_panel(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: EnergyMapperConfigEntry) -> None:
    """Forget what the entities showed; the statistics are kept (a reinstall continues them)."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
