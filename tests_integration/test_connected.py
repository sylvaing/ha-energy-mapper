"""Learning from a connected appliance (#79): the devices proposed with
their operation state, phase state, energy counter and programme, the
teacher of mode ``state`` (on an appliance, or a new one tracked by its
state), the learned signatures of its programmes, and the errors."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.energy_mapper.appliances import Appliance, Source, Teacher
from custom_components.energy_mapper.connected import devices, is_door, run_states

from .conftest import HEALTH, URL
from .test_appliances import pushed, subentries, two_days
from .test_review import decisions, reg
from .test_statistics import mock_addon, settle, setup

STATE = "sensor.washer_dryer_operation_state"
JOB = "sensor.washer_dryer_job_state"
DOOR = "sensor.washer_dryer_door"
PROGRAM = "select.washer_dryer_program"
COUNTER = "sensor.washer_dryer_energy"
POWER = "sensor.washer_dryer_power"
KEY = f"load:{STATE}"
LEARNED = {"id": "cycle-2000w", "kind": "multiphase", "band": "cycle", "power_w": 2000, "label": "Dryer"}
OTHER = {"id": "cycle-2100w", "kind": "multiphase", "band": "cycle", "power_w": 2100, "label": "Dryer"}


def _states() -> dict[str, dict[str, Any]]:
    return {
        STATE: {"state": "stop", "attributes": {"device_class": "enum", "options": ["run", "stop", "pause"]}},
        JOB: {
            "state": "none",
            "attributes": {"device_class": "enum", "options": ["none", "drying", "cooling"]},
        },
        DOOR: {
            "state": "closed",
            "attributes": {"device_class": "enum", "options": ["open", "closed", "locked"]},
        },
        PROGRAM: {"state": "eco", "attributes": {"options": ["eco", "quick"]}},
        COUNTER: {
            "state": "12.3",
            "attributes": {
                "device_class": "energy",
                "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
            },
        },
        POWER: {"state": "0", "attributes": {"device_class": "power", "unit_of_measurement": "W"}},
    }


# ---------------------------------------------------------------------------
# the proposal (pure)
# ---------------------------------------------------------------------------
def test_proposal() -> None:
    registry = [{"entity_id": e, "device_id": "dev1"} for e in _states()]
    registry.append({"entity_id": "sensor.door_only", "device_id": "dev2"})
    states = {
        **_states(),
        "sensor.door_only": {
            "state": "open",
            "attributes": {"device_class": "enum", "options": ["open", "closed"]},
        },
    }
    recorded = {PROGRAM: False}
    (dev,) = devices(registry, states, {"dev1": "Washer dryer"}, lambda e: recorded.get(e, True))
    assert dev["name"] == "Washer dryer" and dev["appliance"] is None
    assert dev["proposal"] == {
        "entity_id": STATE,
        "on_states": ["run"],
        "phase_entity": JOB,
        "energy_entity": COUNTER,
        "program_entity": PROGRAM,
        "power_entity": POWER,
    }
    roles = {e["entity_id"]: (e["role"], e["recorded"]) for e in dev["entities"]}
    assert roles[PROGRAM] == ("program", False)
    assert roles[STATE] == ("state", True)
    assert DOOR not in roles  # a door is never a state to learn from


def test_proposal_reuses_an_appliance() -> None:
    registry = [{"entity_id": e, "device_id": "dev1"} for e in (STATE, JOB, DOOR)]
    states = _states()
    appliance = Appliance(KEY, "Dryer", "energy_mapper:x", (Source("switched_load", JOB, ("drying",)),))
    (dev,) = devices(registry, states, {}, appliances=[appliance])
    # the entity the appliance already uses, with its states, is the operation state
    assert dev["proposal"]["entity_id"] == JOB
    assert dev["proposal"]["on_states"] == ["drying"]
    assert dev["appliance"] == {"key": KEY, "name": "Dryer"}
    taught = Appliance(KEY, "Dryer", "x", (), teacher=Teacher(STATE, 1.0, "state", ("run",), JOB))
    (dev,) = devices(registry, states, {}, appliances=[taught])
    assert dev["proposal"]["entity_id"] == STATE


def test_words() -> None:
    assert run_states(["Run", "stop", "running"]) == ["Run", "running"]
    assert is_door(["open", "closed", "locked"]) and not is_door(["open", "run"]) and not is_door([])


# ---------------------------------------------------------------------------
# the websocket commands
# ---------------------------------------------------------------------------
async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


def addon(mock: AiohttpClientMocker, version: str = "0.14.0") -> None:
    mock_addon(mock, two_days(), reg(), health={**HEALTH, "version": version})
    mock.get(f"{URL}/api/teaching", json={"teachers": []})
    mock.post(
        f"{URL}/api/teaching",
        json={
            "signature": LEARNED,
            "created": True,
            "learned": {},
            "programs": [{"signature": OTHER, "created": True}],
        },
    )
    mock.post(f"{URL}/api/review", json={"signature": LEARNED})


async def _prepare(hass: HomeAssistant, mock: AiohttpClientMocker, entry: MockConfigEntry, version="0.14.0"):
    addon(mock, version)
    await setup(hass, entry)
    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("test", "washer")}, name="Washer dryer"
    )
    ent_reg = er.async_get(hass)
    for entity_id, st in _states().items():
        domain, object_id = entity_id.split(".", 1)
        ent_reg.async_get_or_create(
            domain, "test", object_id, suggested_object_id=object_id, device_id=dev.id
        )
        hass.states.async_set(entity_id, st["state"], st["attributes"])
    await settle(hass)


async def test_connected_devices_and_teach_new(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass)
    answer = await _call(client, type="energy_mapper/connected_devices")
    assert answer["success"], answer
    assert answer["result"]["supported"] is True
    (dev,) = answer["result"]["devices"]
    assert dev["name"] == "Washer dryer"
    proposal = dev["proposal"]
    assert (proposal["entity_id"], proposal["phase_entity"], proposal["energy_entity"]) == (
        STATE,
        JOB,
        COUNTER,
    )
    assert {e["entity_id"]: e["recorded"] for e in dev["entities"]}[STATE] is True

    # a new appliance, learning from its state and tracked by it
    answer = await _call(
        client,
        type="energy_mapper/teach",
        name="Dryer",
        entity_id=STATE,
        mode="state",
        on_states=["run"],
        phase_entity=JOB,
        energy_entity=COUNTER,
        program_entity=PROGRAM,
        track=True,
    )
    assert answer["success"], answer
    assert answer["result"]["key"] == KEY
    assert answer["result"]["sources"] == [
        {"type": "switched_load", "entity_id": STATE, "on_states": ["run"]}
    ]
    await settle(hass)
    teacher = subentries(entry)[KEY].data["teacher"]
    assert teacher["mode"] == "state" and teacher["on_states"] == ["run"]
    assert (teacher["phase_entity"], teacher["energy_entity"], teacher["program_entity"]) == (
        JOB,
        COUNTER,
        PROGRAM,
    )
    dryer = next(a for a in pushed(aioclient_mock)[-1] if a["key"] == KEY)
    assert dryer["teacher"]["mode"] == "state"

    # finished: its signatures join its state, which stays its source
    answer = await _call(client, type="energy_mapper/stop_teaching", appliance=KEY, use=True)
    assert answer["success"], answer
    assert answer["result"]["programs"] == [OTHER]
    await settle(hass)
    data = subentries(entry)[KEY].data
    assert [s["type"] for s in data["sources"]] == ["switched_load", "signature", "signature"]
    assert "teacher" not in data
    assert [d["id"] for d in decisions(aioclient_mock)[-2:]] == ["cycle-2000w", "cycle-2100w"]


async def test_teach_existing_appliance(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    entry.runtime_data.coordinator.async_add_appliance("Dryer", [Source("switched_load", STATE, ("run",))])
    await settle(hass)
    client = await hass_ws_client(hass)
    answer = await _call(
        client, type="energy_mapper/teach", appliance=KEY, entity_id=STATE, mode="state", on_states=["run"]
    )
    assert answer["success"], answer
    assert answer["result"]["teacher"] == {
        "entity_id": STATE,
        "since": answer["result"]["teacher"]["since"],
        "mode": "state",
        "on_states": ["run"],
    }
    (dev,) = (await _call(client, type="energy_mapper/connected_devices"))["result"]["devices"]
    assert dev["appliance"] == {"key": KEY, "name": "Dryer"}


async def test_errors(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass)
    base = {"type": "energy_mapper/teach", "name": "Dryer", "entity_id": STATE, "mode": "state"}
    for extra in (
        {},
        {"on_states": ["run"], "phase_entity": "sensor.missing"},
        {"on_states": ["run"], "energy_entity": JOB.replace("sensor", "select")},
    ):
        answer = await _call(client, **base, **extra)
        assert answer["error"]["code"] == "invalid_format", extra
    answer = await _call(client, type="energy_mapper/teach", entity_id=STATE, mode="state", on_states=["run"])
    assert answer["error"]["code"] == "invalid_format"
    # the state is already another appliance's source
    entry.runtime_data.coordinator.async_add_appliance("Washer", [Source("switched_load", STATE)])
    await settle(hass)
    answer = await _call(client, **base, on_states=["run"])
    assert answer["error"]["code"] == "already_linked"


async def test_addon_too_old(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry, version="0.13.0")
    client = await hass_ws_client(hass)
    answer = await _call(client, type="energy_mapper/connected_devices")
    assert answer["result"]["supported"] is False
    answer = await _call(
        client, type="energy_mapper/teach", name="Dryer", entity_id=STATE, mode="state", on_states=["run"]
    )
    assert answer["error"]["code"] == "not_supported"


async def test_for_admins(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    answer = await _call(client, type="energy_mapper/connected_devices")
    assert answer["error"]["code"] == "unauthorized"
