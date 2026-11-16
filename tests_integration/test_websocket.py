"""Websocket commands relayed to the add-on."""

from __future__ import annotations

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.energy_mapper.const import DOMAIN

from .conftest import DAYS, HEALTH, REPORT, SIGNATURES, URL, addon_settings


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def test_commands(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    addon: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass)

    msg = await _call(client, type="energy_mapper/status")
    assert msg["success"]
    assert msg["result"] == {
        "configured": True,
        "state": "loaded",
        "url": URL,
        "available": True,
        "health": HEALTH,
        "error": None,
        "settings": {},
    }
    assert (await _call(client, type="energy_mapper/days"))["result"] == DAYS
    assert (await _call(client, type="energy_mapper/report", day="2027-03-09"))["result"] == REPORT
    assert (await _call(client, type="energy_mapper/signatures"))["result"] == SIGNATURES
    assert (await _call(client, type="energy_mapper/settings"))["result"] == addon_settings()
    msg = await _call(client, type="energy_mapper/analyze", day="2027-03-09")
    assert msg["result"] == {"day": "2027-03-09", "ok": True}


async def test_addon_errors(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    addon: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass)

    msg = await _call(client, type="energy_mapper/report", day="2027-03-01")
    assert not msg["success"]
    assert msg["error"] == {"code": "not_found", "message": "no report for 2027-03-01"}
    msg = await _call(client, type="energy_mapper/analyze", day="2027-01-01")
    assert msg["error"]["code"] == "no_data"
    msg = await _call(client, type="energy_mapper/report", day="../state")
    assert msg["error"]["code"] == "invalid_format"

    addon.clear_requests()
    addon.get(f"{URL}/api/days", exc=aiohttp.ClientConnectionError())
    addon.get(f"{URL}/api/health", exc=TimeoutError())
    msg = await _call(client, type="energy_mapper/days")
    assert msg["error"]["code"] == "addon_unavailable"
    msg = await _call(client, type="energy_mapper/status")
    assert msg["result"]["available"] is False
    assert msg["result"]["health"] is None
    assert "no answer" in msg["result"]["error"]


async def test_analyze_requires_admin(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    addon: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    msg = await _call(client, type="energy_mapper/analyze", day="2027-03-09")
    assert msg["error"]["code"] == "unauthorized"
    # reading is allowed to every user
    assert (await _call(client, type="energy_mapper/days"))["success"]


async def test_not_set_up(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    msg = await _call(client, type="energy_mapper/days")
    assert msg["error"]["code"] == "not_loaded"
    msg = await _call(client, type="energy_mapper/status")
    assert msg["result"]["configured"] is False
