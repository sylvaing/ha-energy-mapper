"""Availability of the add-on, and synchronisation of the settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperError
from .const import DOMAIN, OPT_SETTINGS, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type EnergyMapperConfigEntry = ConfigEntry[EnergyMapperData]


@dataclass
class EnergyMapperData:
    """Runtime data of a loaded entry."""

    client: EnergyMapperClient
    coordinator: EnergyMapperCoordinator
    # totals of the days already read (websocket energy_mapper/day_stats)
    day_stats: dict[str, dict[str, Any]] = field(default_factory=dict)


class EnergyMapperCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls ``/api/health``; data is the add-on's health answer.

    The settings of the entry are pushed to the add-on on the first
    successful poll after start, and again after any failed push.
    """

    config_entry: EnergyMapperConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: EnergyMapperConfigEntry, client: EnergyMapperClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.settings_synced = False

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            health = await self.client.health()
        except EnergyMapperError as err:
            raise UpdateFailed(f"Energy Mapper add-on unavailable: {err}") from err
        if not self.settings_synced:
            await self._async_sync_settings()
        return health

    async def _async_sync_settings(self) -> None:
        """Push the settings of the entry to the add-on.

        An entry that never saved settings adopts the ones the add-on already
        holds (a reinstalled integration keeps the user's settings)."""
        entry = self.config_entry
        try:
            if OPT_SETTINGS not in entry.options:
                stored = (await self.client.get_settings()).get("settings") or {}
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, OPT_SETTINGS: stored}
                )
                if stored:
                    _LOGGER.info("Settings adopted from the add-on: %s", ", ".join(sorted(stored)))
            else:
                await self.client.put_settings(entry.options[OPT_SETTINGS])
        except EnergyMapperApiError as err:
            if err.code != "invalid_settings":
                _LOGGER.warning("Cannot send the settings to the Energy Mapper add-on: %s", err)
                return
            # retrying would not help: the user has to fix them in the options
            _LOGGER.error("The Energy Mapper add-on refused the settings: %s", err.message)
        except EnergyMapperError as err:
            _LOGGER.warning("Cannot send the settings to the Energy Mapper add-on: %s", err)
            return
        self.settings_synced = True
