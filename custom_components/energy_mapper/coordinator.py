"""Availability of the add-on, synchronisation of the settings, and the
appliances (config subentries) with their hourly statistics."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperError
from .appliances import (
    KIND_LOAD,
    SUBENTRY_TYPE,
    Appliance,
    Source,
    discovered,
    new_appliance,
    power_of,
    powers,
)
from .const import DOMAIN, HOURLY_CONCURRENCY, OPT_SETTINGS, UPDATE_INTERVAL
from .statistics import (
    async_clear,
    async_import,
    async_rename,
    async_statistic_ids,
    hourly_values,
)
from .statistics import async_merge as async_merge_statistics

_LOGGER = logging.getLogger(__name__)

type EnergyMapperConfigEntry = ConfigEntry[EnergyMapperData]

STORAGE_VERSION = 1
EXCLUDED_KEY = "excluded_switched_loads"


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


def _relays(appliances: Iterable[Appliance]) -> set[str]:
    return {s.id for a in appliances for s in a.sources if s.type == KIND_LOAD}


def appliance_subentries(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    return {s.subentry_id: s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE}


class EnergyMapperCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls ``/api/health``; data is the add-on's health answer.

    The settings of the entry are pushed to the add-on on the first
    successful poll after start, and again after any failed push.

    **Appliances** are the entry's config subentries (the source of truth).
    After each successful poll they are synchronised in the background
    (:meth:`async_schedule_sync`):

    1. appliances the add-on found (learned switched loads, labelled
       signatures) that no subentry covers get a subentry; on the very first
       synchronisation, those the add-on holds from a previous installation
       are adopted first;
    2. new or re-analysed days are imported into the hourly statistics, and a
       new appliance gets every stored day (backfill);
    3. the appliances are pushed to the add-on (``PUT /api/appliances``) when
       they changed: names, links, labels.

    A change of the subentries (flows, deletion) is handled at once by
    :meth:`async_subentries_changed`. What the entities show is kept in
    ``.storage`` so it survives restarts.
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
        # appliance key -> appliance (from the subentries)
        self.appliances: dict[str, Appliance] = {}
        # subentry id -> appliance, as last handled (to see what a change did)
        self._known: dict[str, Appliance] = {}
        # learned power of each source ("<type>:<id>"), from the registry
        self.powers: dict[str, float] = {}
        # imported days -> generated_at of the report that was imported
        self.imported: dict[str, float | None] = {}
        # the last analysed day: {"day", "energy_kwh", "uses", "names"} (hourly answer)
        self.latest: dict[str, Any] = {}
        # appliance key -> end of its last use (unix time)
        self.last_seen: dict[str, float] = {}
        # the appliances were pushed to the add-on at least once (else: adopt its own)
        self.declared = False
        self._pushed: list[dict[str, Any]] | None = None
        # None: unknown yet; False: an add-on older than 0.5.0
        self.appliances_api: bool | None = None
        # keys of renamed appliances whose statistic name must follow
        self._renamed: set[str] = set()
        self._sync_lock = asyncio.Lock()
        self._sync_again = False
        self._sync_scheduled = False
        self._warned_old_addon = False

    # -- persistence ----------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.imported = dict(data.get("imported") or {})
        self.latest = dict(data.get("latest") or {})
        self.last_seen = dict(data.get("last_seen") or {})
        self.powers = dict(data.get("powers") or {})
        self.declared = bool(data.get("declared"))
        self._known = self.appliances_by_subentry()
        self.appliances = {a.key: a for a in self._known.values()}

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "imported": self.imported,
                "latest": self.latest,
                "last_seen": self.last_seen,
                "powers": self.powers,
                "declared": self.declared,
            }
        )

    # -- appliances from the subentries ---------------------------------------
    def appliances_by_subentry(self) -> dict[str, Appliance]:
        out: dict[str, Appliance] = {}
        for sid, sub in appliance_subentries(self.config_entry).items():
            try:
                a = Appliance.from_data(sub.data, sub.title, sid)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Invalid appliance %s ignored: %s", sub.title, err)
                continue
            out[sid] = Appliance(
                a.key, a.name, a.statistic_id, a.sources, sid, power_of(a.sources, self.powers)
            )
        return out

    def appliance(self, subentry_id: str) -> Appliance | None:
        return self.appliances_by_subentry().get(subentry_id)

    @callback
    def async_add_appliance(self, name: str, sources: Iterable[Source]) -> Appliance:
        """Create an appliance (a subentry), e.g. for a signature validated in
        the panel (#10). Its entities and statistics follow at the next
        synchronisation, scheduled here."""
        entry = self.config_entry
        taken = {s.unique_id for s in entry.subentries.values() if s.unique_id}
        appliance = new_appliance(name, sources, taken)
        self.hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(appliance.subentry_data()),
                subentry_type=SUBENTRY_TYPE,
                title=name,
                unique_id=appliance.key,
            ),
        )
        return appliance

    def _covered(self) -> set[tuple[str, str]]:
        return {(s.type, s.id) for a in self.appliances_by_subentry().values() for s in a.sources}

    async def async_subentries_changed(self) -> None:
        """The entry changed (a subentry added, edited or deleted, or the
        options): apply what the change means."""
        current = self.appliances_by_subentry()
        previous, self._known = self._known, current
        if current == previous:
            return
        self.appliances = {a.key: a for a in current.values()}
        removed = [a for sid, a in previous.items() if sid not in current]
        # statistics of deleted appliances (not those of a merge: already moved)
        used = {a.statistic_id for a in current.values()}
        async_clear(self.hass, {a.statistic_id for a in removed} - used)
        for a in removed:
            self.last_seen.pop(a.key, None)
        for sid, a in current.items():
            old = previous.get(sid)
            if old is not None and old.name != a.name:
                self._renamed.add(a.key)
        # relays no appliance uses any more must not come back as appliances
        self._async_update_exclusions(
            _relays(previous.values()) - _relays(current.values()), _relays(current.values())
        )
        # an appliance measured by a sub-meter only has no estimated sensors
        ent_reg = er.async_get(self.hass)
        for sid, a in current.items():
            if a.estimated:
                continue
            for entity in er.async_entries_for_config_entry(ent_reg, self.config_entry.entry_id):
                if entity.config_subentry_id == sid:
                    ent_reg.async_remove(entity.entity_id)
        self._async_update_devices(self.appliances)
        self.async_update_listeners()
        self.async_schedule_sync()

    @callback
    def _async_update_exclusions(self, dropped: set[str], used: set[str]) -> None:
        """Exclude the relays of deleted or re-linked appliances (#33: the add-on
        stops measuring them, the integration stops proposing them), and
        include again the relays an appliance uses."""
        entry = self.config_entry
        settings = dict(entry.options.get(OPT_SETTINGS) or {})
        excluded = set(settings.get(EXCLUDED_KEY) or [])
        new = (excluded | dropped) - used
        loads = [x for x in settings.get("switched_loads") or [] if x.get("entity_id") not in dropped]
        if new == excluded and len(loads) == len(settings.get("switched_loads") or []):
            return
        if new:
            settings[EXCLUDED_KEY] = sorted(new)
        else:
            settings.pop(EXCLUDED_KEY, None)
        if loads:
            settings["switched_loads"] = loads
        else:
            settings.pop("switched_loads", None)
        _LOGGER.info("Switched loads excluded from the analysis: %s", ", ".join(sorted(new)) or "none")
        self.hass.config_entries.async_update_entry(entry, options={**entry.options, OPT_SETTINGS: settings})
        self.settings_synced = False
        entry.async_create_background_task(self.hass, self._async_sync_settings(), f"{DOMAIN} settings")

    async def async_merge(self, target_id: str, other_id: str) -> Appliance:
        """Merge the appliance ``other_id`` into ``target_id``: its sources join
        the target's (their energy is added from now on), its hourly history is
        added hour by hour to the target's statistic, then it is deleted
        (device, entities and statistic). The target keeps its name and ids."""
        entry = self.config_entry
        subs = appliance_subentries(entry)
        async with self._sync_lock:
            target, other = self.appliance(target_id), self.appliance(other_id)
            if target is None or other is None or target_id == other_id:
                raise ValueError("unknown appliance")
            stored = await async_statistic_ids(self.hass)
            others = [other.statistic_id] if other.statistic_id in stored else []
            if others:
                await async_merge_statistics(self.hass, target, others)
            sources = target.sources + tuple(s for s in other.sources if not target.has(s))
            merged = Appliance(target.key, target.name, target.statistic_id, sources, target_id)
            ends = [self.last_seen[k] for k in (target.key, other.key) if k in self.last_seen]
            if ends:
                self.last_seen[target.key] = max(ends)
            self.hass.config_entries.async_update_subentry(
                entry, subs[target_id], data=merged.subentry_data()
            )
            self.hass.config_entries.async_remove_subentry(entry, other_id)
            await self._async_save()
        _LOGGER.info("Appliance %s merged into %s", other.name, target.name)
        return merged

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
        if self._sync_scheduled:
            self._sync_again = True
            return
        self._sync_scheduled = True
        self.config_entry.async_create_background_task(
            self.hass, self.async_sync(), f"{DOMAIN} appliances and statistics"
        )

    async def async_sync(self) -> None:
        try:
            # also waits for a merge in progress
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
        finally:
            self._sync_scheduled = False

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

    @staticmethod
    def _no_endpoint(err: EnergyMapperApiError) -> bool:
        """An add-on older than 0.5.0: no appliances endpoint."""
        return err.status in (404, 405)

    async def _async_adopt(self) -> None:
        """First synchronisation of this entry: take over the appliances the
        add-on holds (a reinstalled integration keeps the user's appliances)."""
        try:
            held = (await self.client.get_appliances()).get("appliances") or []
        except EnergyMapperApiError as err:
            if not self._no_endpoint(err):
                raise
            self.appliances_api = False
            return
        self.appliances_api = True
        taken = {s.unique_id for s in self.config_entry.subentries.values()}
        covered = self._covered()
        for item in held:
            try:
                a = Appliance.from_data(item, str(item["name"]))
            except (KeyError, TypeError, ValueError):
                continue
            if a.key in taken or any((s.type, s.id) in covered for s in a.sources):
                continue
            _LOGGER.info("Appliance adopted from the add-on: %s", a.name)
            self.hass.config_entries.async_add_subentry(
                self.config_entry,
                ConfigSubentry(
                    data=MappingProxyType(a.subentry_data()),
                    subentry_type=SUBENTRY_TYPE,
                    title=a.name,
                    unique_id=a.key,
                ),
            )
            taken.add(a.key)
            covered.update((s.type, s.id) for s in a.sources)

    def _discover(self, registry: dict[str, Any], names: dict[str, str]) -> None:
        """A subentry for each appliance the add-on found that none covers."""
        settings = self.config_entry.options.get(OPT_SETTINGS) or {}
        current = self.appliances_by_subentry()
        covered = {(s.type, s.id) for a in current.values() for s in a.sources}
        # relays of appliances deleted a moment ago (their exclusion may not be saved yet)
        excluded = set(settings.get(EXCLUDED_KEY) or []) | (
            _relays(self._known.values()) - _relays(current.values())
        )
        for name, source in discovered(registry, names, excluded):
            if (source.type, source.id) in covered:
                continue
            a = self.async_add_appliance(name, [source])
            covered.add((source.type, source.id))
            _LOGGER.info("New appliance found by the add-on: %s (%s)", a.name, a.key)

    async def _async_push(self, appliances: Iterable[Appliance]) -> None:
        """Send the appliances to the add-on when they changed."""
        if self.appliances_api is False:
            return
        declarations = sorted((a.declaration() for a in appliances), key=lambda d: d["key"])
        if declarations == self._pushed:
            return
        try:
            await self.client.put_appliances(declarations)
        except EnergyMapperApiError as err:
            if self._no_endpoint(err):
                self.appliances_api = False
                return
            _LOGGER.error("The Energy Mapper add-on refused the appliances: %s", err.message)
            self._pushed = declarations  # retried after the next change, not every poll
            return
        self.appliances_api = True
        self._pushed = declarations
        self.declared = True

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

        # the appliances: adopted, found by the add-on, then from the subentries
        names = (payloads.get(latest_day) or self.latest).get("names") or {} if latest_day else {}
        if not self.declared and self.appliances_api is not False:
            await self._async_adopt()
        self._discover(registry, names)
        self.powers.update(powers(registry))
        self._known = self.appliances_by_subentry()
        current = {a.key: a for a in self._known.values()}
        self.appliances = current

        stored_ids = await async_statistic_ids(self.hass)
        new = {k for k, a in current.items() if a.estimated and a.statistic_id not in stored_ids}
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
            if not appliance.estimated:
                continue  # measured by a sub-meter: its sensor has the energy
            import_days = all_with_data if key in new else changed_with_data
            if import_days:
                await async_import(self.hass, appliance, hourly_values(appliance, payloads, import_days))
            elif key in self._renamed and appliance.statistic_id in stored_ids:
                async_rename(self.hass, appliance)
            ends = [
                (p.get("last_active") or {}).get(s.section, {}).get(s.id)
                for p in payloads.values()
                for s in appliance.estimated
            ]
            ends = [e for e in ends if e is not None]
            if key in self.last_seen:
                ends.append(self.last_seen[key])
            if ends:
                self.last_seen[key] = max(ends)
        self._renamed.clear()

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

        # statistics and devices of appliances that no longer exist
        async_clear(self.hass, stored_ids - {a.statistic_id for a in current.values()})
        self._async_remove_devices(set(current))
        self.last_seen = {k: v for k, v in self.last_seen.items() if k in current}
        self._async_update_devices(current)
        self.imported = {
            d: generated.get(d) for d in days if d in payloads or (d in self.imported and d not in changed)
        }
        await self._async_push(current.values())
        await self._async_save()

    @callback
    def _async_update_devices(self, current: dict[str, Appliance]) -> None:
        """Device names follow the appliance names (a name given by the user
        to the device itself is kept)."""
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            for domain, key in device.identifiers:
                if domain != DOMAIN or (appliance := current.get(key)) is None:
                    continue
                if device.name != appliance.name:
                    registry.async_update_device(device.id, name=appliance.name)

    @callback
    def _async_remove_devices(self, keep: set[str]) -> None:
        """Remove the devices (and so the entities) of appliances that no longer exist."""
        registry = dr.async_get(self.hass)
        entry_id = self.config_entry.entry_id
        for device in dr.async_entries_for_config_entry(registry, entry_id):
            keys = {i[1] for i in device.identifiers if i[0] == DOMAIN}
            if keys and entry_id not in keys and not keys & keep:
                _LOGGER.info("Removing the device of a removed appliance: %s", device.name)
                registry.async_remove_device(device.id)
