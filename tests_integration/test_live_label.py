"""Labelling live events (#17): websocket ``energy_mapper/live/label``, the
labelled examples in the appliances' subentries, their completion when the
stop arrives, and the nightly link from ``GET /api/examples``."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.energy_mapper.appliances import (
    MAX_EXAMPLES,
    add_example,
    new_appliance,
    signature_group,
)

from .conftest import URL
from .test_appliances import pushed, subentries, two_days
from .test_review import decisions, replace
from .test_statistics import mock_addon, refresh, settle, setup

SESSION = 1_800_000_000_000
HEATER = "load:switch.water_heater"


def reg() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 1.0,
        "signatures": [
            {
                "id": "short-2000w",
                "kind": "steady",
                "band": "short",
                "label": None,
                "power_w": 2000,
                "count": 23,
            },
            {
                "id": "cyc-1400w",
                "kind": "cycling",
                "band": "session",
                "label": None,
                "power_w": 1400,
                "count": 9,
            },
            {
                "id": "short-1200w",
                "kind": "steady",
                "band": "short",
                "label": None,
                "power_w": 1200,
                "count": 6,
            },
        ],
        "switched_loads": [{"entity_id": "switch.water_heater", "name": "Water heater", "power_w": 1850}],
        "peak": None,
    }


def t0() -> float:
    return round(dt_util.utcnow().timestamp() - 3600)


def event(eid: int, t_on: float, running: bool = False, **kw: Any) -> dict[str, Any]:
    return {
        "id": eid,
        "seq": SESSION + 5,
        "t_on": t_on,
        "delta_w": 2000,
        "state": "confirmed",
        "running": running,
        "t_off": None if running else t_on + 150,
        "off_w": None if running else 2000,
        "duration_s": None if running else 150,
        "power_w": 2000,
        "session": None,
        "pv_moved": False,
        "confidence": "likely",
        "candidates": [],
        "steps": [[t_on, 2000]],
        **kw,
    }


def addon(mock: AiohttpClientMocker) -> None:
    mock_addon(mock, two_days(), reg())
    mock.post(f"{URL}/api/review", json={"signature": reg()["signatures"][0] | {"review_state": "validated"}})
    mock.get(f"{URL}/api/examples", json={"last_day": "2027-03-09", "appliances": []})


def live_event(mock: AiohttpClientMocker, ev: dict[str, Any]) -> None:
    mock.get(f"{URL}/api/live/event?id={ev['id']}&session={SESSION}", json={"session": SESSION, "event": ev})


def expected(mock: AiohttpClientMocker, sid: str, power: int, created: bool = True) -> None:
    sig = {"id": sid, "kind": "steady", "band": "short", "label": "x", "power_w": power, "count": 0}
    mock.post(f"{URL}/api/signatures", json={"signature": sig, "created": created})


def expectations(mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    return [c[2] for c in mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/api/signatures")]


async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def test_name_a_signature_from_a_live_event(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    start = t0()
    live_event(aioclient_mock, event(5, start))
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    answer = await _call(
        client,
        type="energy_mapper/live/label",
        target="signature",
        event_id=5,
        session=SESSION,
        signature_id="short-2000w",
        name="Kettle",
    )
    assert answer["success"], answer
    result = answer["result"]
    assert result["effect"] == "signature_named"
    assert result["signature"] == {
        "id": "short-2000w",
        "kind": "steady",
        "band": "short",
        "power_w": 2000,
        "past_uses": 23,
        "created": False,
    }
    assert result["appliance"]["key"] == "signature:short-2000w"
    assert result["appliance"]["examples"] == 1
    assert result["open"] is False
    example = result["example"]
    assert (example["t_on"], example["t_off"], example["power_w"]) == (start, start + 150, 2000)
    assert (example["source"], example["signature_id"]) == ("live", "short-2000w")
    assert "live" not in example
    await settle(hass)
    sub = subentries(entry)["signature:short-2000w"]
    assert sub.title == "Kettle"
    assert sub.data["examples"] == [example]
    # pushed with the appliance (so the add-on's night sees it), then decided
    declared = {a["key"]: a for a in pushed(aioclient_mock)[-1]}
    assert declared["signature:short-2000w"]["examples"] == [example]
    assert decisions(aioclient_mock) == [
        {"id": "short-2000w", "action": "validate", "appliance": "signature:short-2000w"}
    ]

    # the same signature again ("Yes"): a confirmation, one more example
    live_event(aioclient_mock, event(6, start + 300))
    answer = await _call(
        client,
        type="energy_mapper/live/label",
        target="signature",
        event_id=6,
        session=SESSION,
        signature_id="short-2000w",
        name="Kettle",
    )
    assert answer["result"]["effect"] == "example_added"
    assert answer["result"]["confirmed"] is True
    assert answer["result"]["appliance"]["examples"] == 2
    assert len(decisions(aioclient_mock)) == 1


async def test_new_appliance_with_a_whole_use(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    """Guided mode: explicit times; the expected signature is created at once."""
    addon(aioclient_mock)
    expected(aioclient_mock, "short-850w", 850)
    await setup(hass, entry)
    aioclient_mock.mock_calls.clear()
    # the registry answers with the new expected signature from now on
    registry = reg()
    registry["signatures"].append(
        {
            "id": "short-850w",
            "kind": "steady",
            "band": "short",
            "label": "Toaster",
            "power_w": 850,
            "count": 0,
        }
    )
    replace(aioclient_mock, "get", "/api/signatures", json=registry)
    client = await hass_ws_client(hass)
    start = t0()
    answer = await _call(
        client,
        type="energy_mapper/live/label",
        target="new",
        name="Toaster",
        t_on=start,
        t_off=start + 130,
        power_w=850,
        source="guided",
    )
    assert answer["success"], answer
    result = answer["result"]
    assert result["effect"] == "appliance_created"
    assert result["signature"]["id"] == "short-850w"
    assert result["signature"]["created"] is True
    assert expectations(aioclient_mock) == [
        {"kind": "steady", "band": "short", "power_w": 850, "label": "Toaster"}
    ]
    await settle(hass)
    sub = subentries(entry)["signature:short-850w"]
    assert sub.data["sources"][0]["id"] == "short-850w"
    assert sub.data["examples"][0]["source"] == "guided"
    assert decisions(aioclient_mock)[-1] == {
        "id": "short-850w",
        "action": "validate",
        "appliance": "signature:short-850w",
    }


async def test_new_appliance_open_then_completed_by_the_stop(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    start = t0()
    live_event(aioclient_mock, event(7, start, running=True, delta_w=1200, power_w=1200))
    expected(aioclient_mock, "short-1200w", 1200, created=False)
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    answer = await _call(
        client, type="energy_mapper/live/label", target="new", name="Hair dryer", event_id=7, session=SESSION
    )
    assert answer["success"], answer
    result = answer["result"]
    assert result["effect"] == "appliance_created"
    assert result["open"] is True
    assert result["signature"] is None
    assert result["appliance"]["key"] == "appliance:hair_dryer"
    assert result["appliance"]["sources"] == []
    assert result["example"]["live"] == {"id": 7, "session": SESSION}
    await settle(hass)
    assert subentries(entry)["appliance:hair_dryer"].data["sources"] == []
    assert expectations(aioclient_mock) == []

    # the stop arrives in the live view: the example is completed and the
    # signature of the whole use (an unnamed one of the registry) is linked
    coordinator = entry.runtime_data.coordinator
    stopped = event(7, start, delta_w=1200, power_w=1200)
    coordinator.async_live_events(SESSION + 1, [stopped])  # another session: nothing
    coordinator.async_live_events(SESSION, [event(8, start)])  # another event: nothing
    await settle(hass)
    assert expectations(aioclient_mock) == []
    coordinator.async_live_events(SESSION, [stopped])
    await settle(hass)
    assert expectations(aioclient_mock) == [
        {"kind": "steady", "band": "short", "power_w": 1200, "label": "Hair dryer"}
    ]
    sub = subentries(entry)["appliance:hair_dryer"]
    assert [s["id"] for s in sub.data["sources"]] == ["short-1200w"]
    (example,) = sub.data["examples"]
    assert (example["t_off"], example["signature_id"]) == (start + 150, "short-1200w")
    assert "live" not in example
    assert decisions(aioclient_mock)[-1] == {
        "id": "short-1200w",
        "action": "validate",
        "appliance": "appliance:hair_dryer",
    }


async def test_cancelled_event_drops_the_open_example(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    start = t0()
    live_event(aioclient_mock, event(9, start, running=True))
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    answer = await _call(
        client,
        type="energy_mapper/live/label",
        target="appliance",
        appliance=HEATER,
        event_id=9,
        session=SESSION,
    )
    assert answer["result"]["effect"] == "example_added"
    assert answer["result"]["open"] is True
    await settle(hass)
    assert len(subentries(entry)[HEATER].data["examples"]) == 1
    # told by the live hub (a reset snapshot of the add-on, here)
    entry.runtime_data.live._replace(  # noqa: SLF001
        {"seq": SESSION + 9, "session": SESSION, "events": [event(9, start, state="cancelled")]}
    )
    await settle(hass)
    assert "examples" not in subentries(entry)[HEATER].data


async def test_label_an_existing_appliance(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    start = t0()
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    base = {"type": "energy_mapper/live/label", "target": "appliance", "appliance": HEATER, "power_w": 1400}

    # an unnamed signature: it joins the appliance (the review's validate)
    answer = await _call(client, **base, signature_id="cyc-1400w", t_on=start, t_off=start + 600)
    assert answer["success"], answer
    assert answer["result"]["effect"] == "signature_merged"
    assert answer["result"]["signature"]["past_uses"] == 9
    await settle(hass)
    sub = subentries(entry)[HEATER]
    assert [s.get("id") or s.get("entity_id") for s in sub.data["sources"]] == [
        "switch.water_heater",
        "cyc-1400w",
    ]
    assert decisions(aioclient_mock)[-1] == {"id": "cyc-1400w", "action": "validate", "appliance": HEATER}

    # now its own: a confirmation; a plain example
    answer = await _call(client, **base, signature_id="cyc-1400w", t_on=start + 900, t_off=start + 1000)
    assert answer.get("success"), answer
    assert (answer["result"]["effect"], answer["result"]["confirmed"]) == ("example_added", True)
    answer = await _call(client, **base, t_on=start + 1200, t_off=None)
    assert (answer["result"]["effect"], answer["result"]["open"]) == ("example_added", True)
    assert answer["result"]["appliance"]["examples"] == 3

    # a signature of another appliance ("No, it is..."): only the example
    answer = await _call(
        client,
        type="energy_mapper/live/label",
        target="signature",
        signature_id="short-2000w",
        name="Kettle",
        t_on=start + 1300,
        t_off=start + 1400,
        power_w=2000,
    )
    assert answer["result"]["effect"] == "signature_named"
    await settle(hass)
    answer = await _call(client, **base, signature_id="short-2000w", t_on=start + 1500, t_off=start + 1600)
    assert answer["result"]["effect"] == "example_added"
    assert answer["result"]["other_appliance"] == {"key": "signature:short-2000w", "name": "Kettle"}

    # merged appliances keep the examples of both
    await settle(hass)
    subs = subentries(entry)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_merge(subs[HEATER].subentry_id, subs["signature:short-2000w"].subentry_id)
    await settle(hass)
    assert len(subentries(entry)[HEATER].data["examples"]) == 5


async def test_new_appliance_tracked_by_an_entity(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    hass.states.async_set("switch.pool_pump", "on")
    hass.states.async_set("sensor.oven_power", "0", {"device_class": "power", "unit_of_measurement": "W"})
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    start = t0()
    base = {
        "type": "energy_mapper/live/label",
        "target": "new",
        "t_on": start,
        "t_off": start + 60,
        "power_w": 900,
    }
    answer = await _call(client, **base, name="Pump", track="switch", entity_id="switch.pool_pump")
    assert answer["success"], answer
    assert answer["result"]["appliance"]["sources"] == [
        {"type": "switched_load", "entity_id": "switch.pool_pump"}
    ]
    answer = await _call(client, **base, name="Oven", track="teacher", entity_id="sensor.oven_power")
    assert answer["result"]["appliance"]["teacher"]["entity_id"] == "sensor.oven_power"
    assert answer["result"]["appliance"]["key"] == "appliance:oven"
    # refused: no entity, a switch as a sensor, already linked
    answer = await _call(client, **base, name="X", track="switch")
    assert answer["error"]["code"] == "invalid_format"
    answer = await _call(client, **base, name="X", track="sensor", entity_id="switch.pool_pump")
    assert answer["error"]["code"] == "invalid_format"
    answer = await _call(client, **base, name="X", track="switch", entity_id="switch.pool_pump")
    assert answer["error"]["code"] == "already_linked"
    assert expectations(aioclient_mock) == []


async def test_never_propose_again_and_errors(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon(aioclient_mock)
    aioclient_mock.get(
        f"{URL}/api/live/event?id=1", status=409, json={"error": "session_ended", "message": "ended"}
    )
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    answer = await _call(client, type="energy_mapper/live/label", target="ignore", signature_id="short-2000w")
    assert answer["result"]["effect"] == "signature_ignored"
    assert decisions(aioclient_mock) == [{"id": "short-2000w", "action": "ignore"}]

    start = t0()
    for msg, code in (
        ({"target": "ignore"}, "invalid_format"),
        ({"target": "new", "name": "X"}, "invalid_format"),  # no event, no times
        ({"target": "new", "name": "X", "event_id": 1, "session": SESSION}, "session_ended"),
        ({"target": "new", "name": " ", "t_on": start, "power_w": 500}, "invalid_format"),
        ({"target": "new", "name": "X", "t_on": start, "t_off": start - 5, "power_w": 500}, "invalid_format"),
        ({"target": "new", "name": "X", "t_on": start + 7200, "power_w": 500}, "invalid_format"),
        ({"target": "appliance", "appliance": "nope", "t_on": start, "power_w": 500}, "not_found"),
        ({"target": "signature", "name": "X", "t_on": start, "power_w": 500}, "invalid_format"),
        (
            {"target": "signature", "signature_id": "medium-9w", "name": "X", "t_on": start, "power_w": 9},
            "not_found",
        ),
    ):
        answer = await _call(client, type="energy_mapper/live/label", **msg)
        assert answer["error"]["code"] == code, (msg, answer)
    # the times are enough when the event is gone from the session
    answer = await _call(
        client,
        type="energy_mapper/live/label",
        target="appliance",
        appliance=HEATER,
        event_id=1,
        session=SESSION,
        t_on=start,
        t_off=start + 100,
        power_w=1800,
    )
    assert answer["result"]["effect"] == "example_added"

    reader = await hass_ws_client(hass, hass_read_only_access_token)
    answer = await _call(reader, type="energy_mapper/live/label", target="ignore", signature_id="short-2000w")
    assert answer["error"]["code"] == "unauthorized"


async def test_night_links_the_examples(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    """``GET /api/examples``: the signature the examples were found as
    becomes the appliance's, the open example gets the activation's stop,
    and the panel's appliances show what was found."""
    addon(aioclient_mock)
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    start = t0()
    answer = await _call(
        client, type="energy_mapper/live/label", target="new", name="Toaster", t_on=start, power_w=1200
    )
    assert answer["result"]["open"] is True
    await settle(hass)
    status = {
        "key": "appliance:toaster",
        "name": "Toaster",
        "measured": False,
        "total": 2,
        "found": 1,
        "other": 0,
        "pending": 1,
        "signature": None,
        "link": {
            "id": "short-1200w",
            "kind": "steady",
            "band": "short",
            "power_w": 1200,
            "count": 6,
            "label": None,
        },
        "replace": [],
        "conflict": None,
        "disagreement": [],
        "examples": [
            {
                "t_on": start,
                "t_off": None,
                "power_w": 1200,
                "source": "live",
                "status": "found",
                "reason": None,
                "signature_id": "short-1200w",
                "activation": {"day": "2027-03-09", "start": start - 2, "end": start + 170, "power_w": 1190},
                "small_use": None,
            },
            # under the step threshold, seen by the curve (#100, add-on 0.18.2+)
            {
                "t_on": start + 600,
                "t_off": start + 720,
                "power_w": 70,
                "source": "live",
                "status": "pending",
                "reason": "small_untracked",
                "signature_id": None,
                "activation": None,
                "small_use": {"day": "2027-03-09", "start": start + 604, "end": start + 718, "power_w": 68},
            },
        ],
    }
    replace(aioclient_mock, "get", "/api/examples", json={"last_day": "2027-03-09", "appliances": [status]})
    await refresh(hass, entry)
    sub = subentries(entry)["appliance:toaster"]
    assert sub.data["sources"] == [
        {"type": "signature", "id": "short-1200w", "kind": "steady", "band": "short", "power_w": 1200.0}
    ]
    (example,) = sub.data["examples"]
    assert (example["t_off"], example["power_w"]) == (start + 170, 1190)
    assert decisions(aioclient_mock)[-1] == {
        "id": "short-1200w",
        "action": "validate",
        "appliance": "appliance:toaster",
    }
    answer = await _call(client, type="energy_mapper/appliances")
    toaster = next(a for a in answer["result"]["appliances"] if a["key"] == "appliance:toaster")
    assert toaster["examples"] == [example]
    assert toaster["examples_status"]["found"] == 1
    # the reasons reach the panel as the add-on gives them
    assert toaster["examples_status"]["examples"][1]["reason"] == "small_untracked"
    assert toaster["examples_status"]["examples"][1]["small_use"]["power_w"] == 68


async def test_example_helpers() -> None:
    """Pure helpers: the bound of 20 (guided ones kept first), the same use
    labelled again replaces it, the signature group of a use, a key from the
    name for an appliance without a source."""
    ex = [
        {"t_on": float(i * 100), "t_off": None, "power_w": 1, "source": "live", "created": 0.0}
        for i in range(19)
    ]
    ex[0]["source"] = "guided"
    kept = add_example(ex, {"t_on": 5000.0, "t_off": None, "power_w": 1, "source": "live", "created": 0.0})
    assert len(kept) == MAX_EXAMPLES
    kept = add_example(kept, {"t_on": 6000.0, "t_off": None, "power_w": 1, "source": "live", "created": 0.0})
    assert len(kept) == MAX_EXAMPLES
    assert kept[0]["source"] == "guided"  # the oldest live one went
    assert kept[1]["t_on"] == 200.0
    again = add_example(
        kept, {"t_on": 6000.4, "t_off": 6100.0, "power_w": 2, "source": "live", "created": 1.0}
    )
    assert len(again) == MAX_EXAMPLES
    assert again[-1]["t_off"] == 6100.0
    assert signature_group(0, 120) == ("steady", "short")
    assert signature_group(0, 1200) == ("steady", "medium")
    assert signature_group(0, 7200) == ("steady", "long")
    assert signature_group(0, 600, cycling=True) == ("cycling", "session")
    assert new_appliance("Hair dryer", [], ["appliance:hair_dryer"]).key == "appliance:hair_dryer-2"
