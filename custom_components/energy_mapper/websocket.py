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

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperConnectionError, EnergyMapperError
from .const import CONF_URL, DOMAIN, OPT_REVIEW_REPAIR, OPT_SETTINGS
from .coordinator import EnergyMapperConfigEntry, EnergyMapperCoordinator, ReviewError

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
        ws_settings,
        ws_review,
        ws_validate,
        ws_merge_signature,
        ws_wait,
        ws_ignore,
        ws_restore,
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
