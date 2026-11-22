"""Learning an appliance from a measured device (#14): the teacher in the
appliance's subentry, the websocket commands, the learned signature linked
when the learning is finished, and the admin checks."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.energy_mapper.appliances import Source

from .conftest import URL
from .test_appliances import pushed, subentries, two_days
from .test_review import decisions, reg, replace
from .test_statistics import mock_addon, settle, setup

PLUG = "sensor.oven_plug_power"
OVEN = "submeter:sensor.oven_plug_power"
LEARNED = {"id": "medium-2400w", "kind": "steady", "band": "medium", "power_w": 2400, "label": "Oven"}
STATUS = {
    "key": OVEN,
    "entity_id": PLUG,
    "days": 6,
    "uses": 5,
    "learned": {"kind": "steady", "band": "medium", "power_w": 2400},
    "signature": {"id": "medium-2400w", "in_registry": True, "linked": False},
    "evaluation": {"precision": 0.9, "recall": 1.0},
}


def addon(mock: AiohttpClientMocker) -> None:
    mock_addon(mock, two_days(), reg())
    mock.get(f"{URL}/api/teaching", json={"teachers": [STATUS]})
    mock.post(f"{URL}/api/teaching", json={"signature": LEARNED, "created": False, "learned": {}})
    mock.post(f"{URL}/api/review", json={"signature": LEARNED})


async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def _prepare(hass: HomeAssistant, mock: AiohttpClientMocker, entry: MockConfigEntry) -> None:
    addon(mock)
    await setup(hass, entry)
    hass.states.async_set(PLUG, "0.0", {"device_class": "power", "unit_of_measurement": "W"})
    hass.states.async_set(
        "sensor.oven_energy", "1.2", {"device_class": "energy", "unit_of_measurement": "kWh"}
    )
    entry.runtime_data.coordinator.async_add_appliance("Oven", [Source("submeter", PLUG)])
    await settle(hass)


async def test_teach_and_finish(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass)
    answer = await _call(client, type="energy_mapper/teach", appliance=OVEN, entity_id=PLUG)
    assert answer["success"], answer
    assert answer["result"]["teacher"]["entity_id"] == PLUG
    await settle(hass)
    teacher = subentries(entry)[OVEN].data["teacher"]
    assert teacher["entity_id"] == PLUG and teacher["since"] > 0
    # the add-on knows it: it stops subtracting the plug
    oven = next(a for a in pushed(aioclient_mock)[-1] if a["key"] == OVEN)
    assert oven["teacher"]["entity_id"] == PLUG
    # the panel sees it on the card, and the status of the learning
    answer = await _call(client, type="energy_mapper/appliances")
    card = next(a for a in answer["result"]["appliances"] if a["key"] == OVEN)
    assert card["teacher"]["entity_id"] == PLUG
    answer = await _call(client, type="energy_mapper/teaching")
    assert answer["result"] == {"supported": True, "teachers": [STATUS]}

    # finished with the learned signature: tracked from the curve from now on
    answer = await _call(client, type="energy_mapper/stop_teaching", appliance=OVEN, use=True)
    assert answer["success"], answer
    source = {
        "type": "signature",
        "id": "medium-2400w",
        "kind": "steady",
        "band": "medium",
        "power_w": 2400.0,
    }
    assert answer["result"] == {
        "key": OVEN,
        "sources": [source],
        "signature": LEARNED,
        "created": False,
        "programs": [],
    }
    await settle(hass)
    data = subentries(entry)[OVEN].data
    assert data["sources"] == [source]
    assert "teacher" not in data
    assert decisions(aioclient_mock)[-1] == {"id": "medium-2400w", "action": "validate", "appliance": OVEN}
    oven = next(a for a in pushed(aioclient_mock)[-1] if a["key"] == OVEN)
    assert "teacher" not in oven and oven["sources"] == [source]

    # not learning any more
    answer = await _call(client, type="energy_mapper/stop_teaching", appliance=OVEN)
    assert answer["error"]["code"] == "not_found"


async def test_stop_without_using(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass)
    assert (await _call(client, type="energy_mapper/teach", appliance=OVEN, entity_id=PLUG))["success"]
    answer = await _call(client, type="energy_mapper/stop_teaching", appliance=OVEN, use=False)
    assert answer["success"], answer
    assert answer["result"]["signature"] is None
    await settle(hass)
    data = subentries(entry)[OVEN].data
    assert data["sources"] == [{"type": "submeter", "entity_id": PLUG}]
    assert "teacher" not in data
    assert not [
        c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/api/teaching")
    ]


async def test_errors(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass)
    for entity in ("sensor.oven_energy", "switch.water_heater", "sensor.missing_power"):
        answer = await _call(client, type="energy_mapper/teach", appliance=OVEN, entity_id=entity)
        assert answer["error"]["code"] == "invalid_format", entity
    answer = await _call(client, type="energy_mapper/teach", appliance="appliance:nope", entity_id=PLUG)
    assert answer["error"]["code"] == "not_found"
    # the plug measures another appliance
    answer = await _call(
        client, type="energy_mapper/teach", appliance="load:switch.water_heater", entity_id=PLUG
    )
    assert answer["error"]["code"] == "already_linked"

    # the add-on refuses to link: its error is relayed, nothing changes
    assert (await _call(client, type="energy_mapper/teach", appliance=OVEN, entity_id=PLUG))["success"]
    replace(
        aioclient_mock,
        "post",
        "/api/teaching",
        status=422,
        json={"error": "not_learned", "message": "no use of the appliance seen by its teacher yet"},
    )
    answer = await _call(client, type="energy_mapper/stop_teaching", appliance=OVEN, use=True)
    assert answer["error"]["code"] == "not_learned"
    assert subentries(entry)[OVEN].data["teacher"]["entity_id"] == PLUG

    # an add-on before 0.11.0
    replace(aioclient_mock, "get", "/api/teaching", status=404, json={"error": "not_found"})
    answer = await _call(client, type="energy_mapper/teaching")
    assert answer["result"] == {"supported": False, "teachers": []}
    answer = await _call(client, type="energy_mapper/teach", appliance=OVEN, entity_id=PLUG)
    assert answer["error"]["code"] == "not_supported"


async def test_actions_are_for_admins(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    msgs: list[dict[str, Any]] = [
        {"type": "energy_mapper/teach", "appliance": OVEN, "entity_id": PLUG},
        {"type": "energy_mapper/stop_teaching", "appliance": OVEN, "use": True},
    ]
    for msg in msgs:
        answer = await _call(client, **msg)
        assert answer["error"]["code"] == "unauthorized", msg
    # the status is for everyone
    answer = await _call(client, type="energy_mapper/teaching")
    assert answer["success"], answer
    assert "teacher" not in subentries(entry)[OVEN].data
