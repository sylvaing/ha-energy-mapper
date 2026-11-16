"""Setup, availability polling and synchronisation of the settings."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energy_mapper.const import (
    CONF_DISCOVERED,
    CONF_URL,
    DOMAIN,
    OPT_SETTINGS,
)
from custom_components.energy_mapper.diagnostics import async_get_config_entry_diagnostics

from .conftest import HEALTH, URL, addon_settings


def _puts(mock: AiohttpClientMocker) -> list:
    return [call for call in mock.mock_calls if call[0].lower() == "put"]


async def test_setup_pushes_the_settings(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    settings = {"pv_entity": "sensor.solar_power", "backfill_days": 7}
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.put(f"{URL}/api/settings", json={"settings": settings})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_URL: URL, CONF_DISCOVERED: True},
        options={OPT_SETTINGS: settings},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert [call[2] for call in _puts(aioclient_mock)] == [settings]

    # pushed once: later polls only check the health
    await entry.runtime_data.coordinator.async_refresh()
    assert len(_puts(aioclient_mock)) == 1

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["addon"] == {"available": True, "health": HEALTH, "settings_synced": True}
    assert diag["entry"]["options"] == {OPT_SETTINGS: settings}

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_new_entry_adopts_the_addon_settings(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    stored = {"grid_entity": "sensor.main_power"}
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.get(f"{URL}/api/settings", json=addon_settings(stored))
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={CONF_URL: URL, CONF_DISCOVERED: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options == {OPT_SETTINGS: stored}
    assert _puts(aioclient_mock) == []


async def test_addon_down_retries_setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    aioclient_mock.get(f"{URL}/api/health", exc=aiohttp.ClientConnectionError())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_settings_pushed_after_a_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    settings = {"backfill_days": 7}
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.put(f"{URL}/api/settings", exc=aiohttp.ClientConnectionError())
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_URL: URL, CONF_DISCOVERED: True},
        options={OPT_SETTINGS: settings},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.settings_synced is False

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.put(f"{URL}/api/settings", json={"settings": settings})
    await entry.runtime_data.coordinator.async_refresh()
    assert entry.runtime_data.coordinator.settings_synced is True
    assert [call[2] for call in _puts(aioclient_mock)] == [settings]
