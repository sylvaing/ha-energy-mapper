"""Review of the signatures in the panel (#10): websocket commands, admin
checks, validation into appliances (config subentries), and the single
Repairs notice."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.energy_mapper.const import (
    DOMAIN,
    ISSUE_REVIEW,
    OPT_REVIEW_REPAIR,
    OPT_SETTINGS,
    PANEL_REVIEW_PATH,
)

from .conftest import URL
from .test_appliances import pushed, subentries, two_days
from .test_statistics import device, mock_addon, refresh, settle, setup

CANDIDATE = {
    "id": "cyc-1400w",
    "kind": "cycling",
    "band": "session",
    "label": None,
    "power_w": 1400,
    "count": 9,
    "days_seen": 4,
    "review_state": "proposed",
}


def review(proposed: int = 1, **counts: int) -> dict[str, Any]:
    return {
        "criteria": {"min_uses": 5, "min_days": 3, "wait_days": 14, "wait_factor": 2.0},
        "now": 1.0,
        "counts": {"proposed": proposed, "waiting": 0, "ignored": 0, "validated": 0, **counts},
        "signatures": [CANDIDATE] if proposed else [],
    }


def reg() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 1.0,
        "signatures": [
            {"id": "short-2000w", "kind": "steady", "band": "short", "label": None, "power_w": 2000},
            {"id": "cyc-1400w", "kind": "cycling", "band": "session", "label": None, "power_w": 1400},
        ],
        "switched_loads": [{"entity_id": "switch.water_heater", "name": "Water heater", "power_w": 1850}],
        "peak": None,
    }


def decisions(mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    return [c[2] for c in mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/api/review")]


def addon(mock: AiohttpClientMocker, proposed: int = 1) -> None:
    mock_addon(mock, two_days(), reg(), review=review(proposed))
    mock.post(f"{URL}/api/review", json={"signature": CANDIDATE | {"review_state": "waiting"}})


def replace(mock: AiohttpClientMocker, method: str, path: str, **kwargs: Any) -> None:
    """Answer ``method path`` differently from now on."""
    mock._mocks = [  # noqa: SLF001
        m
        for m in mock._mocks
        if not (m.method.lower() == method and str(m._url).endswith(path))  # noqa: SLF001
    ]
    getattr(mock, method)(f"{URL}{path}", **kwargs)


def issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW)


async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def test_repair_notice_follows_the_count(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    addon(aioclient_mock, proposed=3)
    await setup(hass, entry)
    found = issue(hass)
    assert found is not None
    assert found.translation_key == ISSUE_REVIEW
    assert found.translation_placeholders == {"count": "3", "url": PANEL_REVIEW_PATH}
    assert not found.is_fixable
    assert found.learn_more_url == PANEL_REVIEW_PATH

    # nothing left to review: the notice goes
    addon(aioclient_mock, proposed=0)
    await refresh(hass, entry)
    assert issue(hass) is None

    # turned off in the options
    addon(aioclient_mock, proposed=2)
    await refresh(hass, entry)
    assert issue(hass) is not None
    hass.config_entries.async_update_entry(entry, options={**entry.options, OPT_REVIEW_REPAIR: False})
    await settle(hass)
    assert issue(hass) is None
    await refresh(hass, entry)
    assert issue(hass) is None

    # removed with the entry
    hass.config_entries.async_update_entry(entry, options={**entry.options, OPT_REVIEW_REPAIR: True})
    await refresh(hass, entry)
    assert issue(hass) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert issue(hass) is None


async def test_old_addon_without_review(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    mock_addon(aioclient_mock, two_days(), reg())
    replace(aioclient_mock, "get", "/api/review", status=404, json={"error": "not_found"})
    await setup(hass, entry)
    assert entry.runtime_data.coordinator.review_count is None
    assert issue(hass) is None


async def test_review_commands_are_for_admins(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    await setup(hass, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    for msg in (
        {"type": "energy_mapper/review"},
        {"type": "energy_mapper/validate", "signature_id": "cyc-1400w", "name": "Hob"},
        {"type": "energy_mapper/wait", "signature_id": "cyc-1400w"},
        {"type": "energy_mapper/ignore", "signature_id": "cyc-1400w"},
        {"type": "energy_mapper/restore", "signature_id": "cyc-1400w"},
    ):
        answer = await _call(client, **msg)
        assert answer["error"]["code"] == "unauthorized", msg
    assert decisions(aioclient_mock) == []


async def test_list_wait_ignore_restore(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    await setup(hass, entry)
    client = await hass_ws_client(hass)

    answer = await _call(client, type="energy_mapper/review")
    assert answer["success"]
    assert answer["result"] == {**review(1), "repair": True}

    for action in ("wait", "ignore", "restore"):
        answer = await _call(client, type=f"energy_mapper/{action}", signature_id="cyc-1400w")
        assert answer["success"], answer
        assert answer["result"]["signature"]["id"] == "cyc-1400w"
    assert decisions(aioclient_mock) == [
        {"id": "cyc-1400w", "action": "wait"},
        {"id": "cyc-1400w", "action": "ignore"},
        {"id": "cyc-1400w", "action": "restore"},
    ]

    # nothing left after a decision: the notice goes at once
    assert issue(hass) is not None
    replace(aioclient_mock, "get", "/api/review", json=review(0, ignored=1))
    replace(
        aioclient_mock,
        "post",
        "/api/review",
        status=404,
        json={"error": "not_found", "message": "no signature nope"},
    )
    answer = await _call(client, type="energy_mapper/ignore", signature_id="nope")
    assert answer["error"] == {"code": "not_found", "message": "no signature nope"}
    answer = await _call(client, type="energy_mapper/review")
    assert answer["result"]["counts"]["ignored"] == 1
    assert issue(hass) is None

    answer = await _call(client, type="energy_mapper/wait", signature_id="")
    assert answer["error"]["code"] == "invalid_format"


async def test_validate_as_new_appliance(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    answer = await _call(client, type="energy_mapper/validate", signature_id="cyc-1400w", name=" Hob ")
    assert answer["success"], answer
    source = {"type": "signature", "id": "cyc-1400w", "kind": "cycling", "band": "session", "power_w": 1400.0}
    assert answer["result"] == {
        "appliance": {"key": "signature:cyc-1400w", "name": "Hob", "sources": [source]}
    }
    await settle(hass)

    sub = subentries(entry)["signature:cyc-1400w"]
    assert sub.title == "Hob"
    assert sub.data["sources"] == [source]
    # the add-on knows the appliance before the decision names it
    assert any(a["key"] == "signature:cyc-1400w" for a in pushed(aioclient_mock)[-1])
    assert decisions(aioclient_mock) == [
        {"id": "cyc-1400w", "action": "validate", "appliance": "signature:cyc-1400w"}
    ]
    # its device follows (the synchronisation scheduled by the new subentry)
    assert device(hass, entry, "signature:cyc-1400w") is not None

    # validated twice: refused
    answer = await _call(client, type="energy_mapper/validate", signature_id="cyc-1400w", name="Hob 2")
    assert answer["error"]["code"] == "already_linked"
    answer = await _call(client, type="energy_mapper/validate", signature_id="medium-9w", name="X")
    assert answer["error"]["code"] == "not_found"
    answer = await _call(client, type="energy_mapper/validate", signature_id="short-2000w", name="")
    assert answer["error"]["code"] == "invalid_format"
    answer = await _call(client, type="energy_mapper/validate", signature_id="short-2000w", name="   ")
    assert answer["error"]["code"] == "invalid_format"


async def test_validate_by_linking(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    await setup(hass, entry)
    client = await hass_ws_client(hass)

    # a switch without power measurement: a switched load
    answer = await _call(
        client,
        type="energy_mapper/validate",
        signature_id="cyc-1400w",
        name="Radiator",
        entity_id="switch.radiator",
    )
    assert answer["success"], answer
    assert answer["result"]["appliance"]["key"] == "load:switch.radiator"
    await settle(hass)
    assert subentries(entry)["load:switch.radiator"].data["sources"] == [
        {"type": "switched_load", "entity_id": "switch.radiator"}
    ]
    # a relay used by a validated appliance is not excluded
    assert "switch.radiator" not in (entry.options[OPT_SETTINGS].get("excluded_switched_loads") or [])

    # a power sensor: a sub-meter
    answer = await _call(
        client,
        type="energy_mapper/validate",
        signature_id="short-2000w",
        name="Kettle",
        entity_id="sensor.kettle_power",
    )
    assert answer["result"]["appliance"]["sources"] == [
        {"type": "submeter", "entity_id": "sensor.kettle_power"}
    ]
    assert decisions(aioclient_mock)[-1] == {
        "id": "short-2000w",
        "action": "validate",
        "appliance": "submeter:sensor.kettle_power",
    }

    # already the source of another appliance, or not a switch nor a sensor
    answer = await _call(
        client,
        type="energy_mapper/validate",
        signature_id="short-2000w",
        name="X",
        entity_id="switch.water_heater",
    )
    assert answer["error"]["code"] == "already_linked"
    answer = await _call(
        client, type="energy_mapper/validate", signature_id="short-2000w", name="X", entity_id="automation.x"
    )
    assert answer["error"]["code"] == "invalid_format"
    answer = await _call(
        client, type="energy_mapper/validate", signature_id="short-2000w", name="X", entity_id="nope"
    )
    assert answer["error"]["code"] == "invalid_format"
