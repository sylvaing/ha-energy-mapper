"""Appliances: devices, sensors and hourly statistics imported from the add-on."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
from typing import Any

import aiohttp
import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energy_mapper.appliances import (
    KIND_LOAD,
    KIND_SIGNATURE,
    Appliance,
    appliances_from,
    statistic_id,
)
from custom_components.energy_mapper.const import DOMAIN
from custom_components.energy_mapper.diagnostics import async_get_config_entry_diagnostics
from custom_components.energy_mapper.statistics import hourly_values, merge_rows

from .conftest import HEALTH, URL, addon_settings

H = 3600
# 2027-03-08 and 2027-03-09, as UTC days (the test add-on runs in UTC)
D1 = int(dt.datetime(2027, 3, 8, tzinfo=dt.UTC).timestamp())
D2 = D1 + 24 * H
HEATER = statistic_id(KIND_LOAD, "switch.water_heater")
KETTLE = statistic_id(KIND_SIGNATURE, "short-2000w")


def hourly(day: str, start: int, n: int = 24, known=None, signatures=None, generated=1.0, other=0.2):
    """An hourly answer of the add-on: ``known``/``signatures`` map an hour
    index to ``{id: kWh}``."""
    hours = []
    for i in range(n):
        hours.append(
            {
                "start": start + i * H,
                "house": 1.0,
                "base": 0.3,
                "other": other,
                "known": (known or {}).get(i, {}),
                "signatures": (signatures or {}).get(i, {}),
            }
        )

    def totals(kind: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in hours:
            for k, v in h[kind].items():
                out[k] = round(out.get(k, 0.0) + v, 4)
        return out

    def last(kind: str) -> dict[str, float]:
        return {k: h["start"] + H for h in hours for k in h[kind]}

    def uses(kind: str) -> dict[str, int]:
        return {k: sum(1 for h in hours if k in h[kind]) for k in totals(kind)}

    return {
        "day": day,
        "start": start,
        "end": start + n * H,
        "time_zone": "UTC",
        "generated_at": generated,
        "resolution_s": 60,
        "hours": hours,
        "energy_kwh": {
            "house": float(n),
            "base": 0.3 * n,
            "other": round(other * n, 4),
            "base_w": 300,
            "known": totals("known"),
            "signatures": totals("signatures"),
        },
        "last_active": {"known": last("known"), "signatures": last("signatures")},
        "uses": {"known": uses("known"), "signatures": uses("signatures")},
        "names": {"switch.water_heater": "Water heater"},
    }


def registry(labelled: bool = False, updated: bool = True) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 1.0 if updated else None,
        "signatures": [
            {"id": "short-2000w", "label": "Kettle" if labelled else None, "power_w": 2000},
            {"id": "cyc-1400w", "label": None, "power_w": 1400},
        ],
        "switched_loads": [{"entity_id": "switch.water_heater", "name": "Water heater", "power_w": 1850}],
        "peak": None,
    }


def mock_addon(
    mock: AiohttpClientMocker, days: dict[str, dict[str, Any]], reg: dict[str, Any], health=None
) -> None:
    mock.clear_requests()
    mock.get(f"{URL}/api/health", json=health or HEALTH)
    mock.get(f"{URL}/api/settings", json=addon_settings())
    mock.put(f"{URL}/api/settings", json={"settings": {}})
    mock.get(
        f"{URL}/api/days",
        json={
            "days": sorted(days),
            "summary_days": [],
            "generated": {d: p["generated_at"] for d, p in days.items()},
        },
    )
    mock.get(f"{URL}/api/signatures", json=reg)
    for day, payload in days.items():
        mock.get(f"{URL}/api/hourly/{day}", json=payload)


def device(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, key), entry.entry_id)


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await settle(hass)


async def rows(hass: HomeAssistant, sid: str) -> list[tuple[int, float, float]]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {sid},
        "hour",
        None,
        {"state", "sum"},
    )
    return [(int(r["start"]), round(r["state"], 6), round(r["sum"], 6)) for r in stats.get(sid, [])]


async def our_ids(hass: HomeAssistant) -> set[str]:
    return set(
        await get_instance(hass).async_add_executor_job(lambda: get_metadata(hass, statistic_source=DOMAIN))
    )


def heater_day(day: str, start: int, kwh: dict[int, float], generated=1.0, n=24, kettle=None):
    return hourly(
        day,
        start,
        n=n,
        known={i: {"switch.water_heater": v} for i, v in kwh.items()},
        signatures={i: {"short-2000w": v} for i, v in (kettle or {}).items()},
        generated=generated,
    )


def assert_monotonic(values: list[tuple[int, float, float]]) -> None:
    total = 0.0
    for i, (start, state, sum_) in enumerate(values):
        if i:
            assert start > values[i - 1][0]
        total = round(total + state, 6)
        assert sum_ == pytest.approx(total)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_appliances_and_ids() -> None:
    # a configured switch whose power was never learned is not an appliance yet
    apps = appliances_from(registry(labelled=True), {"switch.pump": "Pump"})
    assert set(apps) == {"load:switch.water_heater", "signature:short-2000w"}
    assert apps["signature:short-2000w"].name == "Kettle"
    assert apps["load:switch.water_heater"].power_w == 1850
    assert apps["load:switch.water_heater"].statistic_id == "energy_mapper:load_switch_water_heater_energy"
    assert KETTLE == "energy_mapper:signature_short_2000w_energy"
    # a rename in the settings follows the last analysed day
    apps = appliances_from(registry(), {"switch.water_heater": "Boiler"})
    assert apps["load:switch.water_heater"] == Appliance(KIND_LOAD, "switch.water_heater", "Boiler", 1850)


def _addon_statistic_ids():
    """The add-on's ``statistic_ids`` module (the add-on code is not a package)."""
    path = pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app" / "statistic_ids.py"
    spec = importlib.util.spec_from_file_location("addon_statistic_ids", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("kind", "addon_kind", "source_id"),
    [
        (KIND_LOAD, "load", "switch.water_heater"),
        (KIND_LOAD, "load", "input_boolean.pump_2"),
        (KIND_LOAD, "load", "switch.relay__x"),
        (KIND_LOAD, "load", "climate.living_room"),
        (KIND_SIGNATURE, "signature", "short-2000w"),
        (KIND_SIGNATURE, "signature", "cyc-150w"),
        (KIND_SIGNATURE, "signature", "long-1200w-3"),
    ],
)
def test_statistic_ids_agree_with_the_addon(kind: str, addon_kind: str, source_id: str) -> None:
    """The add-on maps the statistics of the Energy dashboard back to their
    appliance: both sides must build the same ids (issue #36)."""
    addon = _addon_statistic_ids()
    assert statistic_id(kind, source_id) == addon.statistic_id(addon_kind, source_id)
    assert addon.kind_of(statistic_id(kind, source_id)) == addon_kind


def test_merge_rows() -> None:
    # first import: cumulative from the sum before
    assert merge_rows({0: 1.0, H: 0.0, 2 * H: 2.0}, [], 10.0) == [
        (0, 1.0, 11.0),
        (H, 0.0, 11.0),
        (2 * H, 2.0, 13.0),
    ]
    # re-import of hour 0 with a new value: later rows keep their value, sums shift
    existing = [(0, 1.0, 11.0), (H, 0.0, 11.0), (2 * H, 2.0, 13.0)]
    assert merge_rows({0: 0.5}, existing, 10.0) == [(0, 0.5, 10.5), (H, 0.0, 10.5), (2 * H, 2.0, 12.5)]
    # same values again: only the re-imported hour is written
    assert merge_rows({0: 1.0}, existing, 10.0) == [(0, 1.0, 11.0)]
    # rows without state: value from the sums
    assert merge_rows({0: 1.0}, [(0, None, 11.0), (H, None, 14.0)], 10.0) == [(0, 1.0, 11.0)]
    assert merge_rows({}, existing, 0.0) == []


def test_hourly_values_shares_boundary_hours() -> None:
    heater = Appliance(KIND_LOAD, "switch.water_heater", "Water heater")
    # a day starting at :30 (half-hour time zone): its first hour is shared
    d1 = heater_day("2027-03-08", D1 - H, {0: 0.25, 24: 0.5}, n=25)
    d2 = heater_day("2027-03-09", D2 - H, {0: 0.5, 1: 1.0}, n=25)
    values = hourly_values(heater, {"2027-03-08": d1, "2027-03-09": d2}, ["2027-03-09"])
    assert len(values) == 25
    assert values[D2 - H] == 1.0  # 0.5 from each day
    assert values[D2] == 1.0
    assert values[D2 + 22 * H] == 0.0  # zeros are written too


# ---------------------------------------------------------------------------
# with Home Assistant
# ---------------------------------------------------------------------------
async def test_switched_load_device_entities_and_statistics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = {
        "2027-03-08": heater_day("2027-03-08", D1, {3: 1.85, 4: 0.6}),
        "2027-03-09": heater_day("2027-03-09", D2, {2: 1.0, 23: 0.5}),
    }
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)

    stats = await rows(hass, HEATER)
    assert len(stats) == 48
    assert stats[0][0] == D1 and stats[-1][0] == D2 + 23 * H
    assert stats[3] == (D1 + 3 * H, 1.85, 1.85)
    assert stats[4] == (D1 + 4 * H, 0.6, 2.45)
    assert stats[-1] == (D2 + 23 * H, 0.5, 3.95)
    assert_monotonic(stats)
    assert await our_ids(hass) == {HEATER}  # unlabelled signatures are not appliances

    heater = device(hass, entry, "load:switch.water_heater")
    assert heater is not None and heater.name == "Water heater"
    assert heater.via_device_id == device(hass, entry, entry.entry_id).id
    entities = er.async_entries_for_device(er.async_get(hass), heater.id)
    assert {e.unique_id for e in entities} == {
        "load:switch.water_heater_energy_yesterday",
        "load:switch.water_heater_uses_yesterday",
        "load:switch.water_heater_last_seen",
        "load:switch.water_heater_power",
    }

    state = hass.states.get("sensor.water_heater_energy_yesterday")
    assert state.state == "1.5"
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["day"] == "2027-03-09"
    assert hass.states.get("sensor.water_heater_uses_yesterday").state == "2"
    assert (
        hass.states.get("sensor.water_heater_last_seen").state
        == dt_util.utc_from_timestamp(D2 + 24 * H).isoformat()
    )
    assert hass.states.get("sensor.water_heater_typical_power").state == "1850"
    assert hass.states.get("sensor.energy_mapper_unexplained_energy_yesterday").state == "4.8"
    assert hass.states.get("sensor.energy_mapper_base_load").state == "300"
    assert (
        hass.states.get("sensor.energy_mapper_last_analysis").state
        == dt_util.utc_from_timestamp(HEALTH["last_run"]).isoformat()
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["appliances"]["load:switch.water_heater"]["statistic_id"] == HEATER
    assert diag["imported_days"] == {"2027-03-08": 1.0, "2027-03-09": 1.0}
    assert diag["latest_day"] == "2027-03-09"

    # nothing changed: nothing is fetched or written again
    before = await rows(hass, HEATER)
    await refresh(hass, entry)
    assert not [c for c in aioclient_mock.mock_calls if "/api/hourly/" in str(c[1])][2:]
    assert await rows(hass, HEATER) == before


async def test_reanalysed_day_is_not_double_counted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = {
        "2027-03-08": heater_day("2027-03-08", D1, {3: 2.0}),
        "2027-03-09": heater_day("2027-03-09", D2, {5: 1.0}),
    }
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)
    assert (await rows(hass, HEATER))[-1][2] == 3.0

    # the first day is analysed again: its energy moves and shrinks
    days["2027-03-08"] = heater_day("2027-03-08", D1, {1: 0.5, 2: 0.5}, generated=2.0)
    mock_addon(aioclient_mock, days, registry())
    await refresh(hass, entry)
    fetched = [str(c[1]) for c in aioclient_mock.mock_calls if "/api/hourly/" in str(c[1])]
    assert fetched == [f"{URL}/api/hourly/2027-03-08"]  # only the changed day
    stats = await rows(hass, HEATER)
    assert len(stats) == 48
    assert stats[1] == (D1 + H, 0.5, 0.5)
    assert stats[3] == (D1 + 3 * H, 0.0, 1.0)
    assert stats[24 + 5] == (D2 + 5 * H, 1.0, 2.0)  # the next day shifted
    assert stats[-1][2] == 2.0
    assert_monotonic(stats)

    # analysed once more with the same values: same sums
    days["2027-03-08"]["generated_at"] = 3.0
    mock_addon(aioclient_mock, days, registry())
    await refresh(hass, entry)
    assert await rows(hass, HEATER) == stats

    # a new day continues the sum
    days["2027-03-10"] = heater_day("2027-03-10", D2 + 24 * H, {0: 0.25})
    mock_addon(aioclient_mock, days, registry())
    await refresh(hass, entry)
    stats = await rows(hass, HEATER)
    assert len(stats) == 72
    assert stats[48] == (D2 + 24 * H, 0.25, 2.25)
    assert hass.states.get("sensor.water_heater_energy_yesterday").state == "0.25"


async def test_dst_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    # Europe/Paris, 2027-10-31: 25 hours from 22:00 UTC the day before
    start = int(dt.datetime(2027, 10, 30, 22, tzinfo=dt.UTC).timestamp())
    prev_start = start - 24 * H
    days = {
        "2027-10-30": heater_day("2027-10-30", prev_start, {23: 1.0}),
        "2027-10-31": heater_day("2027-10-31", start, {0: 1.0, 2: 1.0, 3: 1.0, 24: 1.0}, n=25),
    }
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)
    stats = await rows(hass, HEATER)
    assert len(stats) == 49
    assert [s[0] for s in stats] == [prev_start + i * H for i in range(49)]
    # 02:00 and the repeated 02:00 local are two distinct UTC hours
    assert stats[24 + 2] == (start + 2 * H, 1.0, 3.0)
    assert stats[24 + 3] == (start + 3 * H, 1.0, 4.0)
    assert stats[-1] == (start + 24 * H, 1.0, 5.0)
    assert_monotonic(stats)
    assert hass.states.get("sensor.water_heater_energy_yesterday").state == "4.0"


async def test_half_hour_time_zone_reimport(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    # days starting at :30 UTC: the hour around midnight is shared by two days
    d1 = heater_day("2027-03-08", D1 - H, {24: 0.25}, n=25)
    d2 = heater_day("2027-03-09", D2 - H, {0: 0.5, 2: 1.0}, n=25)
    for payload in (d1, d2):
        payload["start"] += 1800
        payload["end"] -= 1800
    days = {"2027-03-08": d1, "2027-03-09": d2}
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)
    stats = await rows(hass, HEATER)
    assert len(stats) == 49
    assert stats[24] == (D2 - H, 0.75, 0.75)  # both halves
    assert stats[-1][2] == 1.75

    # the second day is analysed again: its half of the shared hour changes,
    # the first day's half is kept (its hourly answer is read again)
    d2 = heater_day("2027-03-09", D2 - H, {0: 0.1, 2: 1.0}, n=25, generated=2.0)
    d2["start"] += 1800
    d2["end"] -= 1800
    days["2027-03-09"] = d2
    mock_addon(aioclient_mock, days, registry())
    await refresh(hass, entry)
    fetched = sorted(str(c[1]) for c in aioclient_mock.mock_calls if "/api/hourly/" in str(c[1]))
    assert fetched == [f"{URL}/api/hourly/2027-03-08", f"{URL}/api/hourly/2027-03-09"]
    stats = await rows(hass, HEATER)
    assert stats[24] == (D2 - H, 0.35, 0.35)
    assert stats[-1][2] == pytest.approx(1.35)
    assert_monotonic(stats)


async def test_backfill_then_removal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = {
        "2027-03-08": heater_day("2027-03-08", D1, {3: 1.0}, kettle={7: 0.1}),
        "2027-03-09": heater_day("2027-03-09", D2, {3: 1.0}, kettle={8: 0.2, 19: 0.05}),
    }
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)
    assert await our_ids(hass) == {HEATER}
    heater_rows = await rows(hass, HEATER)

    # the signature gets a label: a new appliance, backfilled from the stored days
    mock_addon(aioclient_mock, days, registry(labelled=True))
    await refresh(hass, entry)
    stats = await rows(hass, KETTLE)
    assert len(stats) == 48
    assert stats[7] == (D1 + 7 * H, 0.1, 0.1)
    assert stats[-1][2] == pytest.approx(0.35)
    assert_monotonic(stats)
    assert await rows(hass, HEATER) == heater_rows  # untouched
    assert hass.states.get("sensor.kettle_energy_yesterday").state == "0.25"
    assert hass.states.get("sensor.kettle_uses_yesterday").state == "2"
    assert device(hass, entry, "signature:short-2000w").name == "Kettle"

    # renamed: the device follows (the id does not change)
    reg = registry(labelled=True)
    reg["signatures"][0]["label"] = "Electric kettle"
    mock_addon(aioclient_mock, days, reg)
    await refresh(hass, entry)
    assert device(hass, entry, "signature:short-2000w").name == "Electric kettle"
    assert len(await rows(hass, KETTLE)) == 48

    # an empty registry (add-on data lost or not yet built) removes nothing
    mock_addon(
        aioclient_mock, days, {**registry(), "updated_at": None, "signatures": [], "switched_loads": []}
    )
    await refresh(hass, entry)
    assert await our_ids(hass) == {HEATER, KETTLE}

    # the label is removed: device, entities and statistics go
    mock_addon(aioclient_mock, days, registry())
    await refresh(hass, entry)
    assert device(hass, entry, "signature:short-2000w") is None
    assert hass.states.get("sensor.kettle_energy_yesterday") is None
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "signature:short-2000w_energy_yesterday")
        is None
    )
    assert await our_ids(hass) == {HEATER}
    assert await rows(hass, KETTLE) == []


async def test_addon_down_keeps_entities_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = {"2027-03-09": heater_day("2027-03-09", D2, {2: 1.0})}
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)
    assert hass.states.get("sensor.water_heater_energy_yesterday").state == "1.0"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/health", exc=aiohttp.ClientConnectionError())
    await refresh(hass, entry)
    for entity_id in (
        "sensor.water_heater_energy_yesterday",
        "sensor.water_heater_last_seen",
        "sensor.energy_mapper_base_load",
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert await our_ids(hass) == {HEATER}  # nothing removed

    # back: the values are there again, without a new import
    mock_addon(aioclient_mock, days, registry())
    await refresh(hass, entry)
    assert hass.states.get("sensor.water_heater_energy_yesterday").state == "1.0"


async def test_values_survive_a_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = {"2027-03-09": heater_day("2027-03-09", D2, {2: 1.0})}
    mock_addon(aioclient_mock, days, registry())
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)

    mock_addon(aioclient_mock, days, registry())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    fetched = [c for c in aioclient_mock.mock_calls if "/api/hourly/" in str(c[1])]
    assert fetched == []  # already imported
    assert hass.states.get("sensor.water_heater_energy_yesterday").state == "1.0"
    assert len(await rows(hass, HEATER)) == 24


async def test_old_addon_without_hourly(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry, caplog
) -> None:
    mock_addon(aioclient_mock, {}, registry())
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/health", json=HEALTH)
    aioclient_mock.get(f"{URL}/api/settings", json=addon_settings())
    aioclient_mock.put(f"{URL}/api/settings", json={"settings": {}})
    aioclient_mock.get(f"{URL}/api/days", json={"days": ["2027-03-09"], "summary_days": []})
    await setup(hass, entry)
    assert "too old for the appliance statistics" in caplog.text
    assert await our_ids(hass) == set()
    assert hass.states.get("sensor.energy_mapper_base_load").state == "unknown"
