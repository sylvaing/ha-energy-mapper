"""Fixtures of the integration tests: a mocked Energy Mapper add-on."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energy_mapper.const import CONF_DISCOVERED, CONF_URL, DOMAIN, OPT_SETTINGS

URL = "http://local-energy-mapper:8099"

HEALTH = {
    "status": "ok",
    "version": "0.3.0",
    "last_run": 1800000000.0,
    "days": 2,
    "signatures": 4,
    "storage_bytes": 123456,
}
DAYS = {"days": ["2027-03-08", "2027-03-09"], "summary_days": ["2027-01-02"]}
REPORT = {"day": "2027-03-09", "energy_kwh": {"house": 12.3}, "curve": [], "peaks": []}
SIGNATURES = {"version": 1, "signatures": [], "switched_loads": [], "peak": None}
AUTOMATIC = {
    "grid_entity": "sensor.grid_power",
    "grid_export_entity": None,
    "pv_entity": "sensor.solar_power",
    "battery_entity": None,
    "submeters": ["sensor.heat_pump_power"],
    "switched_loads": [{"entity_id": "switch.water_heater", "name": "Water heater", "on_states": ["on"]}],
}
DEFAULTS = {
    "analysis_time": "04:30",
    "backfill_days": 30,
    "half_life_days": 30,
    "report_retention_days": 90,
    "peak_window_days": 30,
    "grid_invert": False,
    "battery_invert": False,
    "step_threshold_w": 300.0,
    "night_step_threshold_w": None,
    "adaptive_threshold": False,
    "peak_threshold_w": None,
    "review_min_uses": 5,
    "review_min_days": 3,
    "review_wait_days": 14,
    "review_wait_factor": 2.0,
    "review_suggest_score": 80,
    "self_test_devices": 1,
}


def addon_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"settings": settings or {}, "options": {}, "defaults": DEFAULTS, "automatic": AUTOMATIC}


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder) -> None:
    """Prepare the test recorder before ``hass`` is set up."""


@pytest.fixture(autouse=True)
def auto_recorder(mock_recorder_before_hass, recorder_mock) -> None:
    """An in-memory recorder: the integration depends on it to import statistics."""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the integration from custom_components/."""
    return


@pytest.fixture(autouse=True)
def frontend_loaded(hass: HomeAssistant) -> None:
    """Mark ``frontend`` as set up: the real one needs the built frontend
    (``hass_frontend``), which the test environment does not ship. The panel
    registry it owns (``hass.data``) works without it."""
    hass.config.components.add("frontend")


@pytest.fixture
def addon(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """A running add-on with two reports and no settings."""
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.get(f"{URL}/api/days", json=DAYS)
    aioclient_mock.get(f"{URL}/api/report/2027-03-09", json=REPORT)
    aioclient_mock.get(
        f"{URL}/api/report/2027-03-01",
        status=404,
        json={"error": "not_found", "message": "no report for 2027-03-01"},
    )
    aioclient_mock.post(f"{URL}/api/analyze?day=2027-03-09", json={"day": "2027-03-09", "ok": True})
    aioclient_mock.post(
        f"{URL}/api/analyze?day=2027-01-01",
        status=422,
        json={"day": "2027-01-01", "ok": False, "error": "no_data", "message": "2027-01-01: no grid data"},
    )
    aioclient_mock.get(f"{URL}/api/signatures", json=SIGNATURES)
    aioclient_mock.get(f"{URL}/api/settings", json=addon_settings())
    aioclient_mock.put(f"{URL}/api/settings", json={"settings": {}})
    return aioclient_mock


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Energy Mapper",
        data={CONF_URL: URL, CONF_DISCOVERED: True},
        options={OPT_SETTINGS: {}},
    )
