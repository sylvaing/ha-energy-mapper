"""Availability of the add-on, synchronisation of the settings, and the
appliances with their hourly statistics."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperError
from .appliances import Appliance, appliances_from
from .const import DOMAIN, HOURLY_CONCURRENCY, OPT_SETTINGS, UPDATE_INTERVAL
from .statistics import async_clear, async_import, async_statistic_ids, hourly_values

_LOGGER = logging.getLogger(__name__)

type EnergyMapperConfigEntry = ConfigEntry[EnergyMapperData]

STORAGE_VERSION = 1


@dataclass
class EnergyMapperData:
    """Runtime data of a loaded entry."""

    client: EnergyMapperClient
    coordinator: EnergyMapperCoordinator
    # totals of the days already read (websocket energy_mapper/day_stats)
    day_stats: dict[str, dict[str, Any]] = field(default_factory=dict)


def _neighbours(day: str) -> tuple[str, str]:
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=1)).isoformat(), (d + dt.timedelta(days=1)).isoformat()


class EnergyMapperCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls ``/api/health``; data is the add-on's health answer.

    The settings of the entry are pushed to the add-on on the first
    successful poll after start, and again after any failed push.

    After each successful poll the appliances and their statistics are
    synchronised in the background (:meth:`async_schedule_sync`): new or
    re-analysed days are imported, new appliances get the stored days
    (backfill), removed ones lose their device and statistics. What the
    entities show is kept in ``.storage`` so it survives restarts.
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
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        # appliance key -> appliance
        self.appliances: dict[str, Appliance] = {}
        # imported days -> generated_at of the report that was imported
        self.imported: dict[str, float | None] = {}
        # the last analysed day: {"day", "energy_kwh", "uses", "names"} (hourly answer)
        self.latest: dict[str, Any] = {}
        # appliance key -> end of its last use (unix time)
        self.last_seen: dict[str, float] = {}
        self._sync_lock = asyncio.Lock()
        self._sync_again = False
        self._warned_old_addon = False

    # -- persistence ----------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.appliances = {}
        for raw in data.get("appliances") or []:
            try:
                a = Appliance.from_dict(raw)
            except (KeyError, TypeError):
                continue
            self.appliances[a.key] = a
        self.imported = dict(data.get("imported") or {})
        self.latest = dict(data.get("latest") or {})
        self.last_seen = dict(data.get("last_seen") or {})

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "appliances": [a.as_dict() for a in self.appliances.values()],
                "imported": self.imported,
                "latest": self.latest,
                "last_seen": self.last_seen,
            }
        )

    # -- polling --------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            health = await self.client.health()
        except EnergyMapperError as err:
            raise UpdateFailed(f"Energy Mapper add-on unavailable: {err}") from err
        if not self.settings_synced:
            await self._async_sync_settings()
        self.async_schedule_sync()
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

    # -- appliances and statistics -------------------------------------------
    @callback
    def async_schedule_sync(self) -> None:
        """Synchronise the appliances and statistics in the background (once
        more after the running synchronisation, if one is running)."""
        if self._sync_lock.locked():
            self._sync_again = True
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_sync(), f"{DOMAIN} appliances and statistics"
        )

    async def async_sync(self) -> None:
        async with self._sync_lock:
            while True:
                self._sync_again = False
                try:
                    await self._async_sync()
                except EnergyMapperError as err:
                    _LOGGER.warning("Cannot update the appliances from the Energy Mapper add-on: %s", err)
                except Exception:
                    _LOGGER.exception("Unexpected error while updating the Energy Mapper appliances")
                self.async_update_listeners()
                if not self._sync_again:
                    break

    async def _async_fetch(self, payloads: dict[str, dict[str, Any]], days) -> None:
        """Hourly answers of ``days`` into ``payloads`` (days without a curve are left out)."""
        semaphore = asyncio.Semaphore(HOURLY_CONCURRENCY)

        async def one(day: str) -> None:
            async with semaphore:
                try:
                    payloads[day] = await self.client.hourly(day)
                except EnergyMapperApiError as err:
                    if err.code != "not_found":
                        raise
                    _LOGGER.debug("No hourly breakdown for %s: %s", day, err.message)

        await asyncio.gather(*(one(day) for day in sorted(set(days) - set(payloads))))

    async def _async_sync(self) -> None:
        listing = await self.client.days()
        if "generated" not in listing:
            if not self._warned_old_addon:
                _LOGGER.warning(
                    "The Energy Mapper add-on is too old for the appliance statistics: "
                    "update it to 0.4.0 or later"
                )
                self._warned_old_addon = True
            return
        registry = await self.client.signatures()
        days: list[str] = sorted(listing.get("days") or [])
        generated: dict[str, float | None] = listing.get("generated") or {}
        changed = [d for d in days if d not in self.imported or self.imported[d] != generated.get(d)]
        latest_day = days[-1] if days else None

        payloads: dict[str, dict[str, Any]] = {}
        wanted = set(changed)
        if latest_day and self.latest.get("day") != latest_day:
            wanted.add(latest_day)
        await self._async_fetch(payloads, wanted)

        names = (payloads.get(latest_day) or self.latest).get("names") or {} if latest_day else {}
        current = appliances_from(registry, names)
        # a registry never written (new or unreadable add-on data) removes nothing
        trusted = registry.get("updated_at") is not None
        if not trusted:
            current = {**self.appliances, **current}

        stored_ids = await async_statistic_ids(self.hass)
        new = {k for k, a in current.items() if a.statistic_id not in stored_ids}
        if new:
            await self._async_fetch(payloads, days)  # backfill: every day with a curve
        # hours shared with a neighbouring day (half-hour time zones)
        known_days = set(days)
        partial = [
            d for d, p in payloads.items() if int(p.get("start") or 0) % 3600 or int(p.get("end") or 0) % 3600
        ]
        await self._async_fetch(payloads, {n for d in partial for n in _neighbours(d) if n in known_days})

        changed_with_data = [d for d in changed if d in payloads]
        all_with_data = [d for d in days if d in payloads]
        for key, appliance in current.items():
            import_days = all_with_data if key in new else changed_with_data
            if import_days:
                await async_import(self.hass, appliance, hourly_values(appliance, payloads, import_days))
            ends = [
                (p.get("last_active") or {}).get(appliance.hourly_kind, {}).get(appliance.source_id)
                for p in payloads.values()
            ]
            ends = [e for e in ends if e is not None]
            if key in self.last_seen:
                ends.append(self.last_seen[key])
            if ends:
                self.last_seen[key] = max(ends)

        if latest_day in payloads:
            p = payloads[latest_day]
            self.latest = {
                "day": latest_day,
                "energy_kwh": p.get("energy_kwh") or {},
                "uses": p.get("uses") or {},
                "names": p.get("names") or {},
            }
        elif latest_day is None:
            self.latest = {}

        if trusted:
            ours = {a.statistic_id for a in current.values()}
            async_clear(self.hass, stored_ids - ours)
            self._async_remove_devices(set(current))
            self.last_seen = {k: v for k, v in self.last_seen.items() if k in current}
        self._async_update_devices(current)
        self.appliances = current
        self.imported = {
            d: generated.get(d) for d in days if d in payloads or (d in self.imported and d not in changed)
        }
        await self._async_save()

    @callback
    def _async_update_devices(self, current: dict[str, Appliance]) -> None:
        """Follow renames on the devices that already exist."""
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            for domain, key in device.identifiers:
                if domain != DOMAIN or (appliance := current.get(key)) is None:
                    continue
                old = self.appliances.get(key)
                if old is not None and old.name != appliance.name:
                    registry.async_update_device(device.id, name=appliance.name)

    @callback
    def _async_remove_devices(self, keep: set[str]) -> None:
        """Remove the devices (and so the entities) of removed appliances."""
        registry = dr.async_get(self.hass)
        entry_id = self.config_entry.entry_id
        for device in dr.async_entries_for_config_entry(registry, entry_id):
            keys = {i[1] for i in device.identifiers if i[0] == DOMAIN}
            if keys and entry_id not in keys and not keys & keep:
                _LOGGER.info("Removing the device of a removed appliance: %s", device.name)
                registry.async_remove_device(device.id)
