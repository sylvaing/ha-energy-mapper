"""Config flow (discovery and manual address) and options flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energy_mapper.config_flow import (
    addon_url,
    origin,
    settings_from_form,
    suggested_values,
)
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


def _supervisor(slugs: list[str] | Exception):
    """The Supervisor, with these add-ons installed (or failing)."""
    client = MagicMock()
    if isinstance(slugs, Exception):
        client.addons.list = AsyncMock(side_effect=slugs)
    else:
        client.addons.list = AsyncMock(return_value=[SimpleNamespace(slug=s) for s in slugs])
    return (
        patch("custom_components.energy_mapper.config_flow.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.get_supervisor_client", return_value=client),
    )


def _suggested_url(result) -> str | None:
    for key in result["data_schema"].schema:
        if key == CONF_URL:
            return (key.description or {}).get("suggested_value")
    return None


def test_addon_url() -> None:
    assert addon_url(["core_ssh", "local_energy_mapper"]) == "http://local-energy-mapper:8099"
    assert addon_url(["a1b2c3d4_energy_mapper"]) == "http://a1b2c3d4-energy-mapper:8099"
    assert addon_url(["core_ssh", "a1b2c3d4_energy_mapper_beta"]) is None
    assert addon_url([]) is None


async def test_user_flow_proposes_the_local_addon(hass: HomeAssistant) -> None:
    hassio, client = _supervisor(["core_mosquitto", "local_energy_mapper"])
    with hassio, client:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert _suggested_url(result) == "http://local-energy-mapper:8099"


async def test_user_flow_proposes_the_store_addon(hass: HomeAssistant) -> None:
    """Installed from the add-on repository: any repository hash (#32)."""
    hassio, client = _supervisor(["core_mosquitto", "5c53de3b_energy_mapper"])
    with hassio, client:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert _suggested_url(result) == "http://5c53de3b-energy-mapper:8099"


async def test_user_flow_without_supervisor(hass: HomeAssistant) -> None:
    """Standalone container: an empty field, the hint tells the address."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    assert _suggested_url(result) is None
    # a Supervisor that fails or has no Energy Mapper add-on: empty too
    for slugs in (aiohttp.ClientError("down"), ["core_ssh"]):
        hassio, client = _supervisor(slugs)
        hass.config_entries.flow.async_abort(result["flow_id"])
        with hassio, client:
            result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert _suggested_url(result) is None


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
    "thresholds": {
        "step_threshold_w": 200,
        "night_step_threshold_w": 150,
        "adaptive_threshold": True,
        "cold_appliances": True,
    },
    "analysis": {
        "analysis_time": "03:15:00",
        "backfill_days": 30,
        "half_life_days": 20,
        "self_test_devices": 0.0,
    },
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
    "night_step_threshold_w": 150.0,
    "adaptive_threshold": True,
    "cold_appliances": True,
    "analysis_time": "03:15",
    "half_life_days": 20,
    "self_test_devices": 0,
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
    assert values["analysis"]["self_test_devices"] == 1
    assert "peak_threshold_w" not in values["thresholds"]
    assert "night_step_threshold_w" not in values["thresholds"]  # off by default
    assert values["thresholds"]["adaptive_threshold"] is False  # the noise-adaptive threshold (#88): off
    assert values["thresholds"]["cold_appliances"] is False  # the cold appliances (#89): off
    assert values["review"] == {
        "review_min_uses": 5,
        "review_min_days": 3,
        "review_wait_days": 14,
        "review_wait_factor": 2.0,
        "review_suggest_score": 80,
    }


HEAT_PUMP = "sensor.heat_pump_power"
WATER_HEATER = AUTOMATIC["switched_loads"][0]
AS_FOUND = {"submeters_mode": "automatic", "submeters": [HEAT_PUMP]}


def _form(sensors: dict, loads: dict | None = None) -> dict:
    if loads is None:
        loads = {"switched_loads": [WATER_HEATER]}
    return {"sensors": sensors, "loads": loads}


def test_no_submeters() -> None:
    """#33: "no sub-meters" is kept, not replaced by the Energy dashboard's."""
    settings = settings_from_form(
        _form({"submeters_mode": "none", "submeters": [HEAT_PUMP]}), addon_settings()
    )
    assert settings == {"submeters_mode": "manual"}
    # an empty own list is the same
    assert settings_from_form(_form({"submeters_mode": "manual"}), addon_settings()) == settings
    values = suggested_values(addon_settings(settings))
    assert values["sensors"]["submeters_mode"] == "none"
    assert "submeters" not in values["sensors"]
    # shown as is: saving again changes nothing
    assert settings_from_form(values, addon_settings(settings)) == settings


def test_excluded_and_own_submeters() -> None:
    # an automatic sub-meter removed from the list is excluded
    settings = settings_from_form(_form({"submeters_mode": "automatic", "submeters": []}), addon_settings())
    assert settings == {"excluded_submeters": [HEAT_PUMP]}
    values = suggested_values(addon_settings(settings))
    assert values["sensors"]["submeters_mode"] == "automatic"
    assert "submeters" not in values["sensors"]
    assert settings_from_form(values, addon_settings(settings)) == settings
    # put back in the list: automatic again
    assert settings_from_form(_form({"submeters": [HEAT_PUMP]}), addon_settings(settings)) == {}
    # Home Assistant unreachable from the add-on: the exclusions stay
    no_auto = {**addon_settings(settings), "automatic": None}
    assert settings_from_form(_form({"submeters_mode": "automatic"}, {}), no_auto) == settings
    # a sensor the Energy dashboard does not list: the list becomes the user's own
    form = _form({"submeters_mode": "automatic", "submeters": [HEAT_PUMP, "sensor.ev_power"]})
    own = {"submeters_mode": "manual", "submeters": [HEAT_PUMP, "sensor.ev_power"]}
    assert settings_from_form(form, addon_settings()) == own
    values = suggested_values(addon_settings(own))
    assert values["sensors"]["submeters_mode"] == "manual"
    assert values["sensors"]["submeters"] == own["submeters"]
    # older settings: a list alone is the user's own list
    legacy = suggested_values(addon_settings({"submeters": ["sensor.ev_power"]}))
    assert legacy["sensors"]["submeters_mode"] == "manual"


def test_excluded_automatic_switched_load() -> None:
    """#33: an Energy-dashboard switched load removed from the list is
    excluded; removed from the exclusions, it comes back."""
    settings = settings_from_form(_form(AS_FOUND, {"switched_loads": []}), addon_settings())
    assert settings == {"excluded_switched_loads": ["switch.water_heater"]}
    values = suggested_values(addon_settings(settings))
    assert "switched_loads" not in values["loads"]
    assert values["loads"]["excluded_switched_loads"] == ["switch.water_heater"]
    assert settings_from_form(values, addon_settings(settings)) == settings
    # removed from the exclusions (and not in the list): automatic again
    assert settings_from_form(_form(AS_FOUND, {"switched_loads": []}), addon_settings(settings)) == {}
    # put back in the list: not excluded any more
    back = _form(
        AS_FOUND, {"switched_loads": [WATER_HEATER], "excluded_switched_loads": ["switch.water_heater"]}
    )
    assert settings_from_form(back, addon_settings(settings)) == {}
    # changed rather than removed: an own entry, not an exclusion
    changed = {**WATER_HEATER, "on_states": ["off"]}
    assert settings_from_form(_form(AS_FOUND, {"switched_loads": [changed]}), addon_settings()) == {
        "switched_loads": [changed]
    }


def test_origin() -> None:
    names = {HEAT_PUMP: "Heat pump", "switch.pump": "Pool pump"}
    settings = {
        "excluded_switched_loads": ["switch.pump"],
        "switched_loads": [{"entity_id": "switch.radiator", "name": "Radiator"}],
    }
    assert origin(addon_settings(settings), lambda e: names.get(e, e)) == {
        "auto_submeters": "Heat pump",
        "auto_loads": "Water heater",
        "excluded": "Pool pump",
        "own": "Radiator",
    }
    assert origin({"settings": {}, "automatic": None}, str) == {
        "auto_submeters": "?",
        "auto_loads": "?",
        "excluded": "—",
        "own": "—",
    }


async def test_options_flow_no_submeters_and_exclusion(
    hass: HomeAssistant, addon: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["auto_submeters"] == HEAT_PUMP
    assert result["description_placeholders"]["auto_loads"] == "Water heater"
    form = suggested_values(addon_settings())
    form["sensors"]["submeters_mode"] = "none"
    form["loads"]["switched_loads"] = []
    addon.mock_calls.clear()
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    expected = {"submeters_mode": "manual", "excluded_switched_loads": ["switch.water_heater"]}
    assert entry.options[OPT_SETTINGS] == expected
    puts = [call for call in addon.mock_calls if call[0].lower() == "put"]
    assert puts[-1][2] == expected


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
    return patch("custom_components.energy_mapper.async_setup_entry", return_value=True)
