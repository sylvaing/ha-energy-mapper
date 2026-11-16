"""Websocket commands used by the Energy Mapper panel.

Every command relays one request to the add-on and returns its JSON answer.
Errors: ``not_loaded`` (no entry, or the entry is not set up), ``addon_unavailable``
(the add-on cannot be reached), or the add-on's own error code (``invalid_day``,
``not_found``, ``no_grid``, ``no_data``, ``partial_day``, ``ha_error``,
``internal``...), with its message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperConnectionError, EnergyMapperError
from .const import CONF_URL, DOMAIN, OPT_SETTINGS
from .coordinator import EnergyMapperConfigEntry

DAY = vol.Match(r"^\d{4}-\d{2}-\d{2}$")

ERR_NOT_LOADED = "not_loaded"
ERR_ADDON_UNAVAILABLE = "addon_unavailable"


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the commands of the panel."""
    for handler in (ws_status, ws_days, ws_report, ws_analyze, ws_signatures, ws_settings):
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
    await _relay(hass, connection, msg, lambda client: client.analyze(msg["day"]))


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
