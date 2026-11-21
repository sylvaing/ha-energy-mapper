"""Config flow (discovery and manual address) and options flow."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energy_mapper.config_flow import settings_from_form, suggested_values
from custom_components.energy_mapper.const import (
    CONF_DISCOVERED,
    CONF_URL,
    DOMAIN,
    OPT_REVIEW_REPAIR,
    OPT_SETTINGS,
)

from .conftest import AUTOMATIC, DEFAULTS, HEALTH, URL, addon_settings

DISCOVERY = HassioServiceInfo(
    config={"host": "local-energy-mapper", "port": 8099},
    name="Energy Mapper",
    slug="local_energy_mapper",
    uuid="1234",
)


# ---------------------------------------------------------------------------
# manual address
# ---------------------------------------------------------------------------
async def test_user_flow(hass: HomeAssistant, addon: AiohttpClientMocker) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    with _no_setup():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: f"{URL}/"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Energy Mapper"
    assert result["data"] == {CONF_URL: URL, CONF_DISCOVERED: False}
    assert result["result"].unique_id == DOMAIN


async def test_user_flow_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "ftp://x"})
    assert result["errors"] == {CONF_URL: "invalid_url"}

    aioclient_mock.get("http://down:8099/api/health", exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://down:8099"})
    assert result["errors"] == {"base": "cannot_connect"}

    aioclient_mock.get("http://other:8080/api/health", json={"hello": "world"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://other:8080"}
    )
    assert result["errors"] == {"base": "invalid_response"}

    aioclient_mock.get("http://html:80/api/health", text="<html></html>")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://html:80"})
    assert result["errors"] == {"base": "invalid_response"}


async def test_single_instance(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Supervisor discovery
# ---------------------------------------------------------------------------
async def test_hassio_discovery(hass: HomeAssistant, addon: AiohttpClientMocker) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {"addon": "Energy Mapper"}
    with _no_setup():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL, CONF_DISCOVERED: True}


async def test_hassio_discovery_addon_down(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{URL}/api/health", exc=TimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=DISCOVERY
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_rediscovery_updates_the_address(hass: HomeAssistant, addon: AiohttpClientMocker) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_URL: "http://old-host:8099", CONF_DISCOVERED: True}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_URL] == URL


async def test_rediscovery_keeps_a_manual_address(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_URL: "http://my-server:8099", CONF_DISCOVERED: False}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_URL] == "http://my-server:8099"


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------
FORM = {
    "sensors": {
        "grid_entity": "sensor.grid_power",  # as detected: not an override
        "grid_invert": False,
        "pv_entity": "sensor.other_solar_power",  # changed
        "battery_invert": True,
        "submeters": ["sensor.heat_pump_power"],
    },
    "loads": {
        "switched_loads": [
            {"entity_id": "switch.water_heater", "name": "Water heater", "on_states": ["on"]},
            {"entity_id": "switch.radiator", "name": "Radiator", "on_states": ["off"]},
        ],
        "excluded_switched_loads": ["switch.pump", "switch.pump"],
    },
    "thresholds": {"step_threshold_w": 200},
    "analysis": {"analysis_time": "03:15:00", "backfill_days": 30, "half_life_days": 20},
    "review": {
        "review_repair": False,
        "review_min_uses": 5,
        "review_wait_factor": 3,
        "review_suggest_score": 90.0,
    },
}
EXPECTED = {
    "pv_entity": "sensor.other_solar_power",
    "battery_invert": True,
    "switched_loads": [{"entity_id": "switch.radiator", "name": "Radiator", "on_states": ["off"]}],
    "excluded_switched_loads": ["switch.pump"],
    "step_threshold_w": 200.0,
    "analysis_time": "03:15",
    "half_life_days": 20,
    "review_wait_factor": 3.0,
    "review_suggest_score": 90,
}


def test_settings_from_form() -> None:
    assert settings_from_form(FORM, addon_settings()) == EXPECTED
    # nothing changed: nothing stored
    assert settings_from_form(suggested_values(addon_settings()), addon_settings()) == {}
    # Home Assistant unreachable from the add-on: what is filled in is kept
    no_auto = {"settings": {}, "defaults": DEFAULTS, "automatic": None}
    assert settings_from_form({"sensors": {"grid_entity": "sensor.grid_power"}}, no_auto) == {
        "grid_entity": "sensor.grid_power"
    }


def test_suggested_values() -> None:
    values = suggested_values(
        addon_settings(
            {"pv_entity": "sensor.pv2", "backfill_days": 5, "excluded_switched_loads": ["switch.pump"]}
        )
    )
    assert values["loads"]["excluded_switched_loads"] == ["switch.pump"]
    assert values["sensors"]["grid_entity"] == AUTOMATIC["grid_entity"]
    assert values["sensors"]["pv_entity"] == "sensor.pv2"
    assert values["sensors"]["submeters"] == AUTOMATIC["submeters"]
    assert values["loads"]["switched_loads"] == AUTOMATIC["switched_loads"]
    assert values["analysis"]["backfill_days"] == 5
    assert values["analysis"]["analysis_time"] == "04:30:00"
    assert "peak_threshold_w" not in values["thresholds"]
    assert values["review"] == {
        "review_min_uses": 5,
        "review_min_days": 3,
        "review_wait_days": 14,
        "review_wait_factor": 2.0,
        "review_suggest_score": 80,
    }


async def test_options_flow_pushes_the_settings(
    hass: HomeAssistant, addon: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    addon.mock_calls.clear()
    result = await hass.config_entries.options.async_configure(result["flow_id"], FORM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {OPT_SETTINGS: EXPECTED, OPT_REVIEW_REPAIR: False}
    puts = [call for call in addon.mock_calls if call[0].lower() == "put"]
    assert len(puts) == 1
    assert str(puts[0][1]) == f"{URL}/api/settings"
    assert puts[0][2] == EXPECTED


async def test_options_flow_refused_or_down(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.get(f"{URL}/api/settings", json=addon_settings())
    aioclient_mock.put(
        f"{URL}/api/settings",
        status=400,
        json={"error": "invalid_settings", "message": "half_life_days: expected a value between 1 and 365"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], FORM)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_settings"}
    assert "half_life_days" in result["description_placeholders"]["error"]
    assert entry.options == {OPT_SETTINGS: {}}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/settings", exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


def _no_setup():
    from unittest.mock import patch

    return patch("custom_components.energy_mapper.async_setup_entry", return_value=True)
