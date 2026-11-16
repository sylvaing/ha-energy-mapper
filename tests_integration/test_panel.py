"""Registration of the sidebar panel."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.energy_mapper import panel
from custom_components.energy_mapper.panel import (
    BUNDLE_NAME,
    BUNDLE_PATH,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_URL_PATH,
    STATIC_URL,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_panel_registered_and_removed(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    addon: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    assert BUNDLE_PATH.is_file(), "build the panel: cd frontend && npm ci && npm run build"
    await _setup(hass, entry)

    # the bundle is served
    client = await hass_client_no_auth()
    resp = await client.get(f"{STATIC_URL}/{BUNDLE_NAME}")
    assert resp.status == 200
    assert await resp.read() == BUNDLE_PATH.read_bytes()

    registered = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH]
    assert registered.component_name == "custom"
    assert registered.sidebar_title == "Energy Mapper"
    assert registered.sidebar_icon == PANEL_ICON
    assert registered.require_admin is False
    custom = registered.config["_panel_custom"]
    assert custom["name"] == PANEL_ELEMENT
    assert custom["module_url"].startswith(f"{STATIC_URL}/{BUNDLE_NAME}?v=")
    assert len(custom["module_url"].rsplit("=", 1)[1]) == 12

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[frontend.DATA_PANELS]

    # reloaded: the panel comes back, the static path is not registered twice
    with patch.object(hass.http, "async_register_static_paths") as static:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    static.assert_not_called()
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]


async def test_missing_bundle_skips_the_panel(
    hass: HomeAssistant, addon: AiohttpClientMocker, entry: MockConfigEntry, tmp_path: Path
) -> None:
    with patch.object(panel, "BUNDLE_PATH", tmp_path / BUNDLE_NAME):
        await _setup(hass, entry)
    assert PANEL_URL_PATH not in hass.data.get(frontend.DATA_PANELS, {})
    # the integration itself works without the panel
    assert entry.runtime_data.client is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
