"""Websocket commands used by the Energy Mapper panel.

Every command relays one request to the add-on and returns its JSON answer.
Errors: ``not_loaded`` (no entry, or the entry is not set up), ``addon_unavailable``
(the add-on cannot be reached), or the add-on's own error code (``invalid_day``,
``not_found``, ``no_grid``, ``no_data``, ``partial_day``, ``ha_error``,
``internal``...), with its message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperConnectionError, EnergyMapperError
from .appliances import TEACHER_POWER, TEACHER_STATE
from .connected import devices as connected_devices
from .connected import recorder_filter
from .const import CONF_URL, CONNECTED_MIN_ADDON, DOMAIN, OPT_REVIEW_REPAIR, OPT_SETTINGS
from .coordinator import EnergyMapperConfigEntry, EnergyMapperCoordinator, ReviewError
from .labels import TARGETS, TRACKS, async_label
from .takeover import TakeoverError

DAY = vol.Match(r"^\d{4}-\d{2}-\d{2}$")

# day_stats: days per request, reports read at once, days kept in memory
MAX_STATS_DAYS = 62
STATS_CONCURRENCY = 4
MAX_CACHED_STATS = 400

ERR_NOT_LOADED = "not_loaded"
ERR_ADDON_UNAVAILABLE = "addon_unavailable"


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the commands of the panel."""
    for handler in (
        ws_status,
        ws_days,
        ws_report,
        ws_analyze,
        ws_day_stats,
        ws_signatures,
        ws_appliances,
        ws_settings,
        ws_review,
        ws_validate,
        ws_merge_signature,
        ws_wait,
        ws_ignore,
        ws_restore,
        ws_tracking,
        ws_take_over,
        ws_undo_take_over,
        ws_teaching,
        ws_teach,
        ws_stop_teaching,
        ws_connected_devices,
        ws_self_test,
        ws_live_subscribe,
        ws_live_label,
    ):
        websocket_api.async_register_command(hass, handler)


def _entry(hass: HomeAssistant) -> EnergyMapperConfigEntry | None:
    entries = hass.config_entries.async_entries(DOMAIN, include_ignore=False, include_disabled=True)
    return entries[0] if entries else None


async def _relay(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    call: Callable[[EnergyMapperClient], Awaitable[dict[str, Any]]],
) -> None:
    entry = _entry(hass)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Energy Mapper is not set up")
        return
    try:
        result = await call(entry.runtime_data.client)
    except EnergyMapperConnectionError as err:
        connection.send_error(msg["id"], ERR_ADDON_UNAVAILABLE, str(err))
    except EnergyMapperApiError as err:
        connection.send_error(msg["id"], err.code, err.message)
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/status"})
@websocket_api.async_response
async def ws_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """State of the entry and live health of the add-on; never an error."""
    entry = _entry(hass)
    if entry is None:
        connection.send_result(
            msg["id"],
            {
                "configured": False,
                "state": None,
                "url": None,
                "available": False,
                "health": None,
                "error": None,
                "settings": None,
            },
        )
        return
    health: dict[str, Any] | None = None
    error: str | None = None
    if entry.state is ConfigEntryState.LOADED:
        coordinator = entry.runtime_data.coordinator
        try:
            health = await entry.runtime_data.client.health()
        except EnergyMapperError as err:
            error = str(err)
        else:
            coordinator.async_set_updated_data(health)
    else:
        error = entry.reason or f"entry {entry.state.value}"
    connection.send_result(
        msg["id"],
        {
            "configured": True,
            "state": entry.state.value,
            "url": entry.data[CONF_URL],
            "available": health is not None,
            "health": health,
            "error": error,
            "settings": entry.options.get(OPT_SETTINGS, {}),
        },
    )


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/days"})
@websocket_api.async_response
async def ws_days(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Days with a full report, and older days with a summary only."""
    await _relay(hass, connection, msg, lambda client: client.days())


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/report", vol.Required("day"): DAY})
@websocket_api.async_response
async def ws_report(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The report of one day."""
    await _relay(hass, connection, msg, lambda client: client.report(msg["day"]))


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/analyze", vol.Required("day"): DAY})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_analyze(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Analyse one past day now (admin only; may take a minute)."""

    async def analyze(client: EnergyMapperClient) -> dict[str, Any]:
        try:
            return await client.analyze(msg["day"])
        finally:
            # the day's report changes (or appears)
            if (entry := _entry(hass)) is not None and entry.state is ConfigEntryState.LOADED:
                entry.runtime_data.day_stats.pop(msg["day"], None)
                # its hourly statistics and the appliance sensors follow
                entry.runtime_data.coordinator.async_schedule_sync()

    await _relay(hass, connection, msg, analyze)


def day_stats(report: dict[str, Any]) -> dict[str, Any]:
    """The totals of a report used by the presets of the panel's day picker."""
    notable = [p for p in report.get("peaks") or [] if p.get("notable")]
    return {
        "house_kwh": (report.get("energy_kwh") or {}).get("house"),
        "max_house_w": report.get("max_house_w"),
        "notable_peaks": len(notable),
        "last_notable": max((p["max_ts"] for p in notable if p.get("max_ts") is not None), default=None),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/day_stats",
        vol.Required("days"): vol.All([DAY], vol.Length(max=MAX_STATS_DAYS)),
    }
)
@websocket_api.async_response
async def ws_day_stats(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Totals of some days, without their curve: the integration reads the
    reports from the add-on (and remembers the totals) so the browser does not
    download whole reports. Days without a report are left out."""
    entry = _entry(hass)

    async def stats(client: EnergyMapperClient) -> dict[str, Any]:
        cache: dict[str, dict[str, Any]] = entry.runtime_data.day_stats if entry else {}
        semaphore = asyncio.Semaphore(STATS_CONCURRENCY)

        async def one(day: str) -> None:
            if day in cache:
                return
            async with semaphore:
                try:
                    cache[day] = day_stats(await client.report(day))
                except EnergyMapperApiError:
                    return  # no report for that day

        await asyncio.gather(*(one(day) for day in dict.fromkeys(msg["days"])))
        for day in sorted(cache)[: max(len(cache) - MAX_CACHED_STATS, 0)]:
            del cache[day]
        return {"days": {day: cache[day] for day in msg["days"] if day in cache}}

    await _relay(hass, connection, msg, stats)


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/signatures"})
@websocket_api.async_response
async def ws_signatures(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The signature registry."""
    await _relay(hass, connection, msg, lambda client: client.signatures())


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/appliances"})
@callback
def ws_appliances(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The appliances (config subentries) for the panel's Appliances tab (#54):
    read-only, for every user, without asking the add-on. ``statistic_id`` is
    the id of its hourly energy statistic, null when none is imported (an
    appliance measured by a power sensor only)."""
    entry = _entry(hass)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Energy Mapper is not set up")
        return
    coordinator = entry.runtime_data.coordinator
    appliances = sorted(
        coordinator.appliances_by_subentry().values(), key=lambda a: (a.name.casefold(), a.key)
    )
    connection.send_result(
        msg["id"],
        {
            "entry_id": entry.entry_id,
            "appliances": [
                {
                    "key": a.key,
                    "name": a.name,
                    "subentry_id": a.subentry_id,
                    "statistic_id": a.statistic_id if a.estimated else None,
                    "power_w": a.power_w,
                    "sources": [s.as_dict() for s in a.sources],
                    "teacher": a.teacher.as_dict() if a.teacher else None,
                    # labelled examples (#17), and what the analysed days found of them
                    "examples": [dict(e) for e in a.examples],
                    "examples_status": coordinator.examples_status.get(a.key),
                }
                for a in appliances
            ],
        },
    )


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/settings"})
@websocket_api.async_response
async def ws_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The settings as the add-on knows them (they are changed in the options)."""
    await _relay(hass, connection, msg, lambda client: client.get_settings())


# ---------------------------------------------------------------------------
# review of the signatures (#10): administrators only
# ---------------------------------------------------------------------------
SIGNATURE_ID = vol.All(str, vol.Length(min=1, max=64))


async def _review(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    call: Callable[[EnergyMapperCoordinator], Awaitable[dict[str, Any]]],
) -> None:
    async def run(client: EnergyMapperClient) -> dict[str, Any]:
        entry = _entry(hass)
        if entry is None:  # removed meanwhile
            raise ReviewError(ERR_NOT_LOADED, "Energy Mapper is not set up")
        return await call(entry.runtime_data.coordinator)

    try:
        await _relay(hass, connection, msg, run)
    except ReviewError as err:
        connection.send_error(msg["id"], err.code, err.message)


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/review"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_review(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The signatures to review, waiting and ignored (``GET /api/review``),
    whether the Repairs notice is on, and the appliances a signature can be
    merged into (#43)."""

    async def review(coordinator: EnergyMapperCoordinator) -> dict[str, Any]:
        listing = await coordinator.async_review()
        appliances = sorted(
            ({"key": a.key, "name": a.name} for a in coordinator.appliances_by_subentry().values()),
            key=lambda a: (a["name"].casefold(), a["key"]),
        )
        return {
            **listing,
            "repair": coordinator.config_entry.options.get(OPT_REVIEW_REPAIR, True),
            "appliances": appliances,
        }

    await _review(hass, connection, msg, review)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/validate",
        vol.Required("signature_id"): SIGNATURE_ID,
        vol.Required("name"): vol.All(str, vol.Length(min=1, max=100)),
        vol.Optional("entity_id"): cv.entity_id,
        # the states in which the linked entity's appliance runs (#41)
        vol.Optional("on_states"): vol.All([vol.All(str, vol.Length(min=1, max=100))], vol.Length(max=20)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_validate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Validate a signature: a new appliance (config subentry) named by the
    user, from the signature, or linked to a switch, an entity whose state
    tells when it runs (with its on-states) or a power sensor."""

    async def validate(coordinator: EnergyMapperCoordinator) -> dict[str, Any]:
        appliance = await coordinator.async_validate(
            msg["signature_id"], msg["name"], msg.get("entity_id"), msg.get("on_states")
        )
        return {
            "appliance": {
                "key": appliance.key,
                "name": appliance.name,
                "sources": [s.as_dict() for s in appliance.sources],
            }
        }

    await _review(hass, connection, msg, validate)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/merge_signature",
        vol.Required("signature_id"): SIGNATURE_ID,
        # the key of the appliance (its subentry's unique id)
        vol.Required("appliance"): vol.All(str, vol.Length(min=1, max=200)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_merge_signature(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Validate a signature as a part of an existing appliance (#43): the
    signature joins its sources and its energy is added to the appliance's."""

    async def merge(coordinator: EnergyMapperCoordinator) -> dict[str, Any]:
        appliance = await coordinator.async_merge_signature(msg["signature_id"], msg["appliance"])
        return {
            "appliance": {
                "key": appliance.key,
                "name": appliance.name,
                "sources": [s.as_dict() for s in appliance.sources],
            }
        }

    await _review(hass, connection, msg, merge)


def _decision(action: str):
    @websocket_api.websocket_command(
        {vol.Required("type"): f"energy_mapper/{action}", vol.Required("signature_id"): SIGNATURE_ID}
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def handler(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
    ) -> None:
        await _review(
            hass,
            connection,
            msg,
            lambda coordinator: coordinator.async_review_decision(msg["signature_id"], action),
        )

    handler.__name__ = f"ws_{action}"
    handler.__doc__ = f"{action.capitalize()} a signature of the review (recorded by the add-on)."
    return handler


# wait: shown again when there is clearly more evidence; ignore: never proposed
# again; restore: undo an ignore (or a wait)
ws_wait = _decision("wait")
ws_ignore = _decision("ignore")
ws_restore = _decision("restore")


# ---------------------------------------------------------------------------
# take over tracking in the Energy dashboard (#13): administrators only
# ---------------------------------------------------------------------------
APPLIANCE_KEY = vol.All(str, vol.Length(min=1, max=200))


async def _energy(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    call: Callable[[EnergyMapperCoordinator], Awaitable[dict[str, Any]]],
) -> None:
    """Answered by the integration alone (the Energy dashboard prefs and its
    store), without the add-on."""
    entry = _entry(hass)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Energy Mapper is not set up")
        return
    try:
        result = await call(entry.runtime_data.coordinator)
    except TakeoverError as err:
        connection.send_error(msg["id"], err.code, err.message)
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/tracking"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tracking(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The individual devices of the Energy dashboard an appliance may take
    over, and per appliance with a statistic: whether it is in the dashboard,
    what it took over, and the devices that are probably it."""
    await _energy(hass, connection, msg, lambda coordinator: coordinator.async_tracking())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/take_over",
        vol.Required("appliance"): APPLIANCE_KEY,
        vol.Required("stat_consumption"): vol.All(str, vol.Length(min=1, max=255)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_take_over(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace a device of the Energy dashboard by the appliance's statistic
    (the panel asked the user to confirm)."""
    await _energy(
        hass,
        connection,
        msg,
        lambda coordinator: coordinator.async_take_over(msg["appliance"], msg["stat_consumption"]),
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "energy_mapper/undo_take_over", vol.Required("appliance"): APPLIANCE_KEY}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_undo_take_over(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Put back the device the appliance took over (the panel asked the user
    to confirm)."""
    await _energy(
        hass, connection, msg, lambda coordinator: coordinator.async_undo_take_over(msg["appliance"])
    )


# ---------------------------------------------------------------------------
# learning from a measured device (#14): the status for every user, the
# actions for administrators
# ---------------------------------------------------------------------------
@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/teaching"})
@websocket_api.async_response
async def ws_teaching(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The learning status of the appliances taught by a measured device
    (``GET /api/teaching``), and whether the add-on supports it."""
    await _review(hass, connection, msg, lambda coordinator: coordinator.async_teaching())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/teach",
        # an existing appliance, or a new one named ``name`` (#79)
        vol.Exclusive("appliance", "target"): APPLIANCE_KEY,
        vol.Exclusive("name", "target"): vol.All(str, vol.Length(min=1, max=100)),
        vol.Required("entity_id"): cv.entity_id,
        # power: a power sensor (#14); state: a connected appliance's operation state (#79)
        vol.Optional("mode", default=TEACHER_POWER): vol.In([TEACHER_POWER, TEACHER_STATE]),
        vol.Optional("on_states", default=list): vol.All(
            [vol.All(str, vol.Length(min=1, max=100))], vol.Length(max=20)
        ),
        vol.Optional("phase_entity"): vol.Any(None, cv.entity_id),
        vol.Optional("energy_entity"): vol.Any(None, cv.entity_id),
        vol.Optional("program_entity"): vol.Any(None, cv.entity_id),
        # a new appliance: also tracked by its operation state (a switched load)
        vol.Optional("track", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_teach(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Learn an appliance from a power sensor that measures it, or from a
    connected appliance's state (#79)."""
    if "appliance" not in msg and "name" not in msg:
        connection.send_error(msg["id"], "invalid_format", "expected an appliance or a name")
        return

    async def teach(coordinator: EnergyMapperCoordinator) -> dict[str, Any]:
        appliance = await coordinator.async_teach(
            msg.get("appliance"),
            msg["entity_id"],
            mode=msg["mode"],
            on_states=msg["on_states"],
            phase_entity=msg.get("phase_entity"),
            energy_entity=msg.get("energy_entity"),
            program_entity=msg.get("program_entity"),
            name=msg.get("name"),
            track=msg["track"],
        )
        return {
            "key": appliance.key,
            "name": appliance.name,
            "sources": [s.as_dict() for s in appliance.sources],
            "teacher": appliance.teacher.as_dict() if appliance.teacher else None,
        }

    await _review(hass, connection, msg, teach)


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/connected_devices"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_connected_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The devices that can teach an appliance from their state (#79), with
    the entities proposed for each and whether the recorder keeps them."""
    entry = _entry(hass)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Energy Mapper is not set up")
        return
    coordinator = entry.runtime_data.coordinator
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    registry = [
        {
            "entity_id": e.entity_id,
            "device_id": e.device_id,
            "entity_category": e.entity_category.value if e.entity_category else None,
            "disabled_by": e.disabled_by,
        }
        for e in ent_reg.entities.values()
        if e.device_id and e.platform != DOMAIN
    ]
    states = {
        s.entity_id: {"state": s.state, "attributes": dict(s.attributes)}
        for s in hass.states.async_all(["sensor", "select"])
    }
    names = {d.id: d.name_by_user or d.name or d.id for d in dev_reg.devices.values()}
    result = connected_devices(
        registry, states, names, recorder_filter(hass), coordinator.appliances_by_subentry().values()
    )
    connection.send_result(
        msg["id"], {"supported": coordinator.addon_at_least(CONNECTED_MIN_ADDON), "devices": result}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/stop_teaching",
        vol.Required("appliance"): APPLIANCE_KEY,
        # link the learned signature to the appliance (the panel asked to confirm)
        vol.Optional("use", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_stop_teaching(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """End the learning of an appliance, using the learned signature or not."""

    async def stop(coordinator: EnergyMapperCoordinator) -> dict[str, Any]:
        appliance, answer = await coordinator.async_stop_teaching(msg["appliance"], msg["use"])
        return {
            "key": appliance.key,
            "sources": [s.as_dict() for s in appliance.sources],
            "signature": (answer or {}).get("signature"),
            "created": bool((answer or {}).get("created")),
            # the other programmes of a connected appliance (#79)
            "programs": [p["signature"] for p in (answer or {}).get("programs") or [] if p.get("signature")],
        }

    await _review(hass, connection, msg, stop)


# ---------------------------------------------------------------------------
# nightly self-test (#15): every user
# ---------------------------------------------------------------------------
@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/self_test"})
@websocket_api.async_response
async def ws_self_test(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The success rate of the measured devices hidden from the nightly
    analysis (``GET /api/self_test``), and whether the add-on supports it."""
    await _review(hass, connection, msg, lambda coordinator: coordinator.async_self_test())


@websocket_api.websocket_command({vol.Required("type"): "energy_mapper/live/subscribe"})
@callback
def ws_live_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The live view (#17), for every user, read-only: a subscription that
    streams ``snapshot``, ``update`` and ``status`` messages (``live.py``)."""
    entry = _entry(hass)
    if entry is None or entry.state is not ConfigEntryState.LOADED or entry.runtime_data.live is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Energy Mapper is not set up")
        return

    @callback
    def forward(message: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], message))

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = entry.runtime_data.live.subscribe(forward)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_mapper/live/label",
        vol.Required("target"): vol.In(TARGETS),
        # the live event (``id`` of an event and ``session`` of the live stream)
        vol.Optional("event_id"): vol.All(int, vol.Range(min=0)),
        vol.Optional("session"): vol.All(int, vol.Range(min=0)),
        # or the use itself (guided mode); they win over the event's
        vol.Optional("t_on"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("t_off"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0))),
        vol.Optional("power_w"): vol.All(vol.Coerce(float), vol.Range(min=1, max=100000)),
        vol.Optional("source", default="live"): vol.In(["live", "guided"]),
        vol.Optional("signature_id"): SIGNATURE_ID,
        vol.Optional("appliance"): APPLIANCE_KEY,
        vol.Optional("name"): vol.All(str, vol.Length(min=1, max=100)),
        vol.Optional("track", default="curve"): vol.In(TRACKS),
        vol.Optional("entity_id"): cv.entity_id,
        vol.Optional("on_states"): vol.All([vol.All(str, vol.Length(min=1, max=100))], vol.Length(max=20)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_live_label(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Label a live event (#17, ``labels.py``): name a signature, add an
    example to an appliance, create a new appliance, or never propose a
    signature again. Answers what it did (``effect``)."""
    await _review(hass, connection, msg, lambda coordinator: async_label(coordinator, msg))
