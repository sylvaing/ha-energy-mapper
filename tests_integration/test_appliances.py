"""Appliances as config subentries (#11): discovery, migration, rename, link
change, merge, deletion, manual add and adoption."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energy_mapper.appliances import SUBENTRY_TYPE, appliance_of_key, statistic_id
from custom_components.energy_mapper.const import CONF_DISCOVERED, CONF_URL, DOMAIN, OPT_SETTINGS
from custom_components.energy_mapper.statistics import async_import, hourly_values

from .conftest import URL
from .test_statistics import (
    D1,
    D2,
    HEATER,
    KETTLE,
    H,
    assert_monotonic,
    device,
    hourly,
    mock_addon,
    our_ids,
    refresh,
    registry,
    rows,
    settle,
    setup,
)

KETTLE2 = statistic_id("signature", "medium-2000w")


def two_days(**kettles: dict[int, dict[str, float]]) -> dict[str, dict[str, Any]]:
    return {
        "2027-03-08": hourly(
            "2027-03-08",
            D1,
            known={3: {"switch.water_heater": 1.0}},
            signatures=kettles.get("d1") or {7: {"short-2000w": 0.1}},
        ),
        "2027-03-09": hourly(
            "2027-03-09",
            D2,
            known={3: {"switch.water_heater": 2.0}},
            signatures=kettles.get("d2") or {8: {"short-2000w": 0.2}},
        ),
    }


def full_registry(**extra: Any) -> dict[str, Any]:
    reg = registry(labelled=True)
    reg["signatures"][0].update(kind="steady", band="short")
    reg["signatures"].append(
        {"id": "medium-2000w", "kind": "steady", "band": "medium", "label": "Tea kettle", "power_w": 2000}
    )
    reg.update(extra)
    return reg


def subentries(entry: MockConfigEntry) -> dict[str, Any]:
    """unique id (appliance key) -> subentry."""
    return {s.unique_id: s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE}


def pushed(mock: AiohttpClientMocker) -> list[list[dict[str, Any]]]:
    return [
        c[2]["appliances"] for c in mock.mock_calls if c[0] == "PUT" and str(c[1]).endswith("/api/appliances")
    ]


def settings_pushed(mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    return [c[2] for c in mock.mock_calls if c[0] == "PUT" and str(c[1]).endswith("/api/settings")]


async def reconfigure(hass: HomeAssistant, entry: MockConfigEntry, key: str, step: str, data: dict[str, Any]):
    sub = subentries(entry)[key]
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id},
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": step})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    await settle(hass)
    return result


async def add(hass: HomeAssistant, entry: MockConfigEntry, step: str, data: dict[str, Any]):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": step})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    await settle(hass)
    return result


async def stat_name(hass: HomeAssistant, sid: str) -> str | None:
    meta = await get_instance(hass).async_add_executor_job(lambda: get_metadata(hass, statistic_ids={sid}))
    return meta[sid][1]["name"] if sid in meta else None


# ---------------------------------------------------------------------------
async def test_discovered_appliances_become_subentries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    mock_addon(aioclient_mock, two_days(), full_registry())
    await setup(hass, entry)

    subs = subentries(entry)
    assert set(subs) == {"load:switch.water_heater", "signature:short-2000w", "signature:medium-2000w"}
    heater = subs["load:switch.water_heater"]
    assert heater.title == "Water heater"
    assert dict(heater.data) == {
        "key": "load:switch.water_heater",
        "statistic_id": HEATER,
        "sources": [{"type": "switched_load", "entity_id": "switch.water_heater"}],
    }
    assert subs["signature:short-2000w"].data["sources"] == [
        {"type": "signature", "id": "short-2000w", "kind": "steady", "band": "short", "power_w": 2000.0}
    ]
    # devices and entities belong to their subentry
    dev = device(hass, entry, "load:switch.water_heater")
    assert dev.config_subentry_id == heater.subentry_id
    entity = er.async_get(hass).async_get(
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "load:switch.water_heater_energy_yesterday")
    )
    assert entity.entity_id == "sensor.water_heater_energy_yesterday"
    assert entity.config_subentry_id == heater.subentry_id
    assert await our_ids(hass) == {HEATER, KETTLE, KETTLE2}
    assert hass.states.get("sensor.kettle_energy_yesterday").state == "0.2"

    # pushed to the add-on once, then only on changes
    assert pushed(aioclient_mock) == [
        [
            {
                "key": "load:switch.water_heater",
                "name": "Water heater",
                "statistic_id": HEATER,
                "sources": [{"type": "switched_load", "entity_id": "switch.water_heater"}],
            },
            {
                "key": "signature:medium-2000w",
                "name": "Tea kettle",
                "statistic_id": KETTLE2,
                "sources": [
                    {
                        "type": "signature",
                        "id": "medium-2000w",
                        "kind": "steady",
                        "band": "medium",
                        "power_w": 2000.0,
                    }
                ],
            },
            {
                "key": "signature:short-2000w",
                "name": "Kettle",
                "statistic_id": KETTLE,
                "sources": [
                    {
                        "type": "signature",
                        "id": "short-2000w",
                        "kind": "steady",
                        "band": "short",
                        "power_w": 2000.0,
                    }
                ],
            },
        ]
    ]
    mock_addon(aioclient_mock, two_days(), full_registry())
    await refresh(hass, entry)
    assert pushed(aioclient_mock) == []
    # a new appliance in the add-on, already known by its source: no duplicate
    assert len(subentries(entry)) == 3


async def test_migration_of_existing_appliances(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Appliances published by 0.2 (#12): devices, entities and statistics
    without subentries. They move into subentries without losing anything."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Energy Mapper",
        data={CONF_URL: URL, CONF_DISCOVERED: True},
        options={OPT_SETTINGS: {}},
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    service = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    old = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "load:switch.water_heater")},
        name="Water heater",
        via_device_id=service.id,
    )
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "load:switch.water_heater_energy_yesterday",
        config_entry=entry,
        device_id=old.id,
        suggested_object_id="my_heater_energy",
    )
    # its statistics, and a labelled signature known only by the store
    days = two_days()
    heater = appliance_of_key("load:switch.water_heater", "Water heater")
    await async_import(hass, heater, hourly_values(heater, days, list(days)))
    await settle(hass)
    before = await rows(hass, HEATER)
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_save(
        {
            "appliances": [
                {"kind": "switched_load", "source_id": "switch.water_heater", "name": "Water heater"},
                {"kind": "signature", "source_id": "short-2000w", "name": "Kettle"},
            ],
            "imported": {"2027-03-08": 1.0, "2027-03-09": 1.0},
        }
    )

    mock_addon(aioclient_mock, days, registry())  # the label is gone from the add-on: kept here
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)

    assert entry.minor_version == 2
    subs = subentries(entry)
    assert set(subs) == {"load:switch.water_heater", "signature:short-2000w"}
    assert subs["signature:short-2000w"].title == "Kettle"
    moved = dev_reg.async_get(old.id)
    assert moved is not None and moved.config_subentry_id == subs["load:switch.water_heater"].subentry_id
    entity = ent_reg.async_get("sensor.my_heater_energy")  # the entity id did not change
    assert entity.config_subentry_id == subs["load:switch.water_heater"].subentry_id
    assert entity.device_id == old.id
    assert await rows(hass, HEATER) == before  # nothing re-imported or lost
    assert_monotonic(before)
    assert len(subentries(entry)) == 2  # the relay of the registry is that appliance
    # the kettle (no statistic yet) gets its backfill
    assert len(await rows(hass, KETTLE)) == 48


async def test_rename_keeps_the_ids(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    mock_addon(aioclient_mock, two_days(), registry())
    await setup(hass, entry)
    before = await rows(hass, HEATER)
    sub_id = subentries(entry)["load:switch.water_heater"].subentry_id
    mock_addon(aioclient_mock, two_days(), registry())

    result = await reconfigure(hass, entry, "load:switch.water_heater", "rename", {"name": "  "})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "invalid_name"}
    result = await reconfigure(hass, entry, "load:switch.water_heater", "rename", {"name": " Boiler "})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    sub = subentries(entry)["load:switch.water_heater"]
    assert (sub.title, sub.subentry_id) == ("Boiler", sub_id)
    assert device(hass, entry, "load:switch.water_heater").name == "Boiler"
    state = hass.states.get("sensor.water_heater_energy_yesterday")  # same entity id
    assert state.attributes["friendly_name"] == "Boiler Energy yesterday"
    assert await stat_name(hass, HEATER) == "Boiler"
    assert await rows(hass, HEATER) == before
    assert [a["name"] for a in pushed(aioclient_mock)[-1]] == ["Boiler"]
    assert hass.states.get("sensor.boiler_energy_yesterday") is None


async def test_link_changes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = two_days()
    mock_addon(aioclient_mock, days, full_registry())
    await setup(hass, entry)
    mock_addon(aioclient_mock, days, full_registry())

    # the water heater moves to another relay (forced on-states)
    result = await reconfigure(
        hass,
        entry,
        "load:switch.water_heater",
        "link_relay",
        {"entity_id": "switch.boiler", "on_states": ["off"]},
    )
    assert result["reason"] == "reconfigure_successful"
    sub = subentries(entry)["load:switch.water_heater"]
    assert sub.data["sources"] == [
        {"type": "switched_load", "entity_id": "switch.boiler", "on_states": ["off"]}
    ]
    assert sub.data["statistic_id"] == HEATER  # same statistic, same entities
    # the old relay is excluded: not measured, not proposed again
    assert entry.options[OPT_SETTINGS]["excluded_switched_loads"] == ["switch.water_heater"]
    assert settings_pushed(aioclient_mock)[-1] == {"excluded_switched_loads": ["switch.water_heater"]}
    assert pushed(aioclient_mock)[-1][0]["sources"] == [
        {"type": "switched_load", "entity_id": "switch.boiler", "on_states": ["off"]}
    ]

    # the next day is measured on the new relay; the registry still remembers the old one
    days["2027-03-10"] = hourly("2027-03-10", D2 + 24 * H, known={1: {"switch.boiler": 1.5}})
    mock_addon(aioclient_mock, days, full_registry())
    await refresh(hass, entry)
    stats = await rows(hass, HEATER)
    assert stats[48 + 1] == (D2 + 25 * H, 1.5, 4.5)
    assert set(subentries(entry)) == {
        "load:switch.water_heater",
        "signature:short-2000w",
        "signature:medium-2000w",
    }
    assert hass.states.get("sensor.water_heater_energy_yesterday").state == "1.5"

    # the kettle signature turns out to be a relay-driven appliance
    result = await reconfigure(
        hass, entry, "signature:short-2000w", "link_relay", {"entity_id": "switch.kettle"}
    )
    assert subentries(entry)["signature:short-2000w"].data["sources"] == [
        {"type": "switched_load", "entity_id": "switch.kettle"}
    ]
    declared = {a["key"]: a for a in pushed(aioclient_mock)[-1]}
    assert declared["signature:short-2000w"]["sources"] == [
        {"type": "switched_load", "entity_id": "switch.kettle"}
    ]
    assert KETTLE in await our_ids(hass)  # its history is kept

    # the tea kettle is measured by a smart plug from now on: a sub-meter
    kettle2_rows = await rows(hass, KETTLE2)
    result = await reconfigure(
        hass, entry, "signature:medium-2000w", "link_sensor", {"entity_id": "sensor.plug_power"}
    )
    assert subentries(entry)["signature:medium-2000w"].data["sources"] == [
        {"type": "submeter", "entity_id": "sensor.plug_power"}
    ]
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, "signature:medium-2000w_energy_yesterday") is None
    assert device(hass, entry, "signature:medium-2000w") is not None
    days["2027-03-11"] = hourly("2027-03-11", D2 + 48 * H, signatures={1: {"medium-2000w": 9.0}})
    mock_addon(aioclient_mock, days, full_registry())
    await refresh(hass, entry)
    assert await rows(hass, KETTLE2) == kettle2_rows  # history kept, nothing new imported

    # a relay already used by another appliance is refused
    result = await reconfigure(
        hass, entry, "signature:short-2000w", "link_relay", {"entity_id": "switch.boiler"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"entity_id": "already_linked"}
    assert result["description_placeholders"]["other"] == "Water heater"


async def test_merge(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    days = two_days(
        d1={7: {"short-2000w": 0.1, "medium-2000w": 0.5}},
        d2={8: {"short-2000w": 0.2}, 9: {"medium-2000w": 0.4}},
    )
    mock_addon(aioclient_mock, days, full_registry())
    await setup(hass, entry)
    tea_device = device(hass, entry, "signature:medium-2000w")
    assert tea_device is not None
    mock_addon(aioclient_mock, days, full_registry())

    result = await reconfigure(
        hass,
        entry,
        "signature:short-2000w",
        "merge",
        {"other": subentries(entry)["signature:medium-2000w"].subentry_id},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "merged"
    assert result["description_placeholders"] == {"name": "Kettle", "other": "Tea kettle"}

    subs = subentries(entry)
    assert set(subs) == {"load:switch.water_heater", "signature:short-2000w"}
    assert [s["id"] for s in subs["signature:short-2000w"].data["sources"]] == ["short-2000w", "medium-2000w"]
    # the history of both, hour by hour, under the kept appliance
    stats = await rows(hass, KETTLE)
    assert stats[7] == (D1 + 7 * H, 0.6, 0.6)
    assert stats[24 + 8] == (D2 + 8 * H, 0.2, 0.8)
    assert stats[24 + 9] == (D2 + 9 * H, 0.4, 1.2)
    assert_monotonic(stats)
    assert await our_ids(hass) == {HEATER, KETTLE}
    assert dr.async_get(hass).async_get(tea_device.id) is None
    assert hass.states.get("sensor.tea_kettle_energy_yesterday") is None
    assert hass.states.get("sensor.kettle_energy_yesterday").state == "0.6"
    declared = {a["key"]: a for a in pushed(aioclient_mock)[-1]}
    assert set(declared) == {"load:switch.water_heater", "signature:short-2000w"}
    assert len(declared["signature:short-2000w"]["sources"]) == 2

    # a re-analysed day re-imports the sum of both sources
    days["2027-03-09"]["generated_at"] = 2.0
    days["2027-03-09"]["hours"][9]["signatures"] = {"medium-2000w": 0.3, "short-2000w": 0.1}
    mock_addon(aioclient_mock, days, full_registry())
    await refresh(hass, entry)
    stats = await rows(hass, KETTLE)
    assert stats[24 + 9] == (D2 + 9 * H, 0.4, 1.2)
    assert_monotonic(stats)


async def test_delete_has_no_comeback(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Energy Mapper",
        data={CONF_URL: URL, CONF_DISCOVERED: True},
        options={
            OPT_SETTINGS: {
                "switched_loads": [
                    {"entity_id": "switch.water_heater", "name": "Heater", "on_states": ["on"]}
                ]
            }
        },
    )
    mock_addon(aioclient_mock, two_days(), registry())
    await setup(hass, entry)
    heater = subentries(entry)["load:switch.water_heater"]
    mock_addon(aioclient_mock, two_days(), registry())

    hass.config_entries.async_remove_subentry(entry, heater.subentry_id)
    await settle(hass)
    assert device(hass, entry, "load:switch.water_heater") is None
    assert hass.states.get("sensor.water_heater_energy_yesterday") is None
    assert await our_ids(hass) == set()
    # excluded, and removed from the manual switched loads
    assert entry.options[OPT_SETTINGS] == {"excluded_switched_loads": ["switch.water_heater"]}
    assert settings_pushed(aioclient_mock)[-1] == {"excluded_switched_loads": ["switch.water_heater"]}
    assert pushed(aioclient_mock)[-1] == []

    # the add-on still remembers the relay: it does not come back
    mock_addon(aioclient_mock, two_days(), registry())
    await refresh(hass, entry)
    assert subentries(entry) == {}
    assert await our_ids(hass) == set()

    # added again by hand: no longer excluded
    result = await add(hass, entry, "add_relay", {"name": "Boiler", "entity_id": "switch.water_heater"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "excluded_switched_loads" not in entry.options[OPT_SETTINGS]
    assert subentries(entry)["load:switch.water_heater"].title == "Boiler"
    assert len(await rows(hass, HEATER)) == 48  # backfilled again


async def test_manual_add(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    mock_addon(aioclient_mock, two_days(), registry())
    await setup(hass, entry)
    mock_addon(aioclient_mock, two_days(), registry())

    # a relay-driven load
    result = await add(
        hass, entry, "add_relay", {"name": "Pool pump", "entity_id": "switch.pool_pump", "on_states": ["on"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = subentries(entry)["load:switch.pool_pump"]
    assert sub.title == "Pool pump"
    assert sub.data == {
        "key": "load:switch.pool_pump",
        "statistic_id": "energy_mapper:load_switch_pool_pump_energy",
        "sources": [{"type": "switched_load", "entity_id": "switch.pool_pump", "on_states": ["on"]}],
    }
    assert "load:switch.pool_pump" in {a["key"] for a in pushed(aioclient_mock)[-1]}
    # the same relay twice is refused
    result = await add(hass, entry, "add_relay", {"name": "Heater 2", "entity_id": "switch.water_heater"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"entity_id": "already_linked"}

    # "Kettle, 2000 W" before it is seen
    aioclient_mock.post(
        f"{URL}/api/signatures",
        json={
            "signature": {
                "id": "short-2000w",
                "kind": "steady",
                "band": "short",
                "power_w": 2000,
                "label": "Kettle",
            },
            "created": True,
        },
    )
    result = await add(hass, entry, "add_expected", {"name": "Kettle", "power_w": 2000, "usage": "short"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    call = next(
        c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/api/signatures")
    )
    assert call[2] == {"kind": "steady", "band": "short", "power_w": 2000.0, "label": "Kettle"}
    sub = subentries(entry)["signature:short-2000w"]
    assert sub.data["sources"] == [
        {"type": "signature", "id": "short-2000w", "kind": "steady", "band": "short", "power_w": 2000.0}
    ]
    assert sub.data["statistic_id"] == KETTLE


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (
            409,
            {"error": "conflict", "message": "a signature of another appliance is at this power: x"},
            "power_w",
        ),
        (405, {"error": "method_not_allowed"}, "base"),
    ],
)
async def test_manual_add_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    status: int,
    body: dict[str, Any],
    error: str,
) -> None:
    mock_addon(aioclient_mock, two_days(), registry())
    await setup(hass, entry)
    aioclient_mock.post(f"{URL}/api/signatures", status=status, json=body)
    result = await add(hass, entry, "add_expected", {"name": "Kettle", "power_w": 2000, "usage": "cycling"})
    assert result["type"] is FlowResultType.FORM
    assert set(result["errors"]) == {error}
    call = next(
        c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/api/signatures")
    )
    assert (call[2]["kind"], call[2]["band"]) == ("cycling", "session")


async def test_adoption_and_old_addon(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    """A reinstalled integration takes over the appliances the add-on holds."""
    held = [
        {
            "key": "appliance-dryer",
            "name": "Dryer",
            "statistic_id": "energy_mapper:dryer_energy",
            "sources": [{"type": "submeter", "entity_id": "sensor.dryer_power"}],
        },
        {
            "key": "load:switch.water_heater",
            "name": "Boiler",
            "statistic_id": HEATER,
            "sources": [{"type": "switched_load", "entity_id": "switch.water_heater", "on_states": ["off"]}],
        },
    ]
    mock_addon(aioclient_mock, two_days(), registry(), appliances=held)
    await setup(hass, entry)
    subs = subentries(entry)
    assert subs["appliance-dryer"].title == "Dryer"
    assert subs["load:switch.water_heater"].title == "Boiler"  # adopted before discovery
    assert subs["load:switch.water_heater"].data["sources"][0]["on_states"] == ["off"]
    assert len(subs) == 2
    assert entry.runtime_data.coordinator.declared

    # an add-on older than 0.5.0: appliances work, nothing is pushed
    coordinator = entry.runtime_data.coordinator
    coordinator.declared = False
    coordinator.appliances_api = None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/health", json={"status": "ok", "version": "0.4.1"})
    aioclient_mock.get(
        f"{URL}/api/days",
        json={"days": sorted(two_days()), "summary_days": [], "generated": {d: 1.0 for d in two_days()}},
    )
    aioclient_mock.get(f"{URL}/api/signatures", json=registry())
    for day, payload in two_days().items():
        aioclient_mock.get(f"{URL}/api/hourly/{day}", json=payload)
    aioclient_mock.get(f"{URL}/api/appliances", status=404, json={"error": "not_found"})
    await refresh(hass, entry)
    assert coordinator.appliances_api is False
    assert pushed(aioclient_mock) == []
