"""Availability of the add-on, synchronisation of the settings, and the
appliances (config subentries) with their hourly statistics."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperError
from .appliances import (
    KIND_LOAD,
    KIND_SIGNATURE,
    KIND_SUBMETER,
    SUBENTRY_TYPE,
    TEACHER_POWER,
    TEACHER_STATE,
    Appliance,
    Source,
    Teacher,
    add_example,
    discovered,
    new_appliance,
    power_of,
    powers,
    relay,
    signature,
    signature_group,
)
from .const import (
    CONNECTED_MIN_ADDON,
    DOMAIN,
    HOURLY_CONCURRENCY,
    ISSUE_REVIEW,
    OPT_REVIEW_REPAIR,
    OPT_SETTINGS,
    PANEL_REVIEW_PATH,
    POWER_UNITS,
    STATE_SENSOR_CLASS,
    SWITCH_DOMAINS,
    UPDATE_INTERVAL,
)
from .live import LiveHub
from .statistics import (
    async_clear,
    async_import,
    async_rename,
    async_statistic_ids,
    hourly_values,
)
from .statistics import async_merge as async_merge_statistics
from .takeover import TakeoverError, in_dashboard, label, pickable, restore, suggestion, take_over

_LOGGER = logging.getLogger(__name__)

type EnergyMapperConfigEntry = ConfigEntry[EnergyMapperData]

STORAGE_VERSION = 1
EXCLUDED_KEY = "excluded_switched_loads"
# Repairs issue offering to restore the device an appliance took over in the
# Energy dashboard, once that appliance is deleted (#13)
ISSUE_TAKEOVER_PREFIX = "takeover_removed_"


class ReviewError(Exception):
    """A review decision that cannot be applied; ``code`` is a websocket error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class EnergyMapperData:
    """Runtime data of a loaded entry."""

    client: EnergyMapperClient
    coordinator: EnergyMapperCoordinator
    # the live view, polled while a panel watches (#17)
    live: LiveHub | None = None
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
        # keys of appliances whose stored days must be imported again (a
        # signature merged into them, #43)
        self._reimport: set[str] = set()
        self._sync_lock = asyncio.Lock()
        self._sync_again = False
        self._sync_scheduled = False
        self._warned_old_addon = False
        # signatures waiting for a review (None: unknown, or an add-on before 0.6.0)
        self.review_count: int | None = None
        # appliance key -> the Energy dashboard device its statistic replaced
        # (#13): {"statistic_id", "replaced", "children", "name", "at"}
        self.takeovers: dict[str, dict[str, Any]] = {}
        self._energy_lock = asyncio.Lock()
        # appliance key -> its labelled examples found again by the add-on
        # (GET /api/examples, #17); empty without examples or an older add-on
        self.examples_status: dict[str, dict[str, Any]] = {}
        # open examples being completed from the live view: (session, event id)
        self._completing: set[tuple[int, int]] = set()

    # -- persistence ----------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.imported = dict(data.get("imported") or {})
        self.latest = dict(data.get("latest") or {})
        self.last_seen = dict(data.get("last_seen") or {})
        self.powers = dict(data.get("powers") or {})
        self.declared = bool(data.get("declared"))
        self._reimport = set(data.get("reimport") or [])
        self.takeovers = {
            k: v
            for k, v in (data.get("takeovers") or {}).items()
            if isinstance(v, dict) and v.get("replaced")
        }
        self._known = self.appliances_by_subentry()
        self.appliances = {a.key: a for a in self._known.values()}
        # the Repairs issues of deleted appliances are not persistent
        keys = {a.key for a in self._known.values()}
        for key in self.takeovers:
            if key not in keys:
                self._async_takeover_issue(key)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "imported": self.imported,
                "latest": self.latest,
                "last_seen": self.last_seen,
                "powers": self.powers,
                "declared": self.declared,
                "reimport": sorted(self._reimport),
                "takeovers": self.takeovers,
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
            out[sid] = replace(a, subentry_id=sid, power_w=power_of(a.sources, self.powers))
        return out

    def appliance(self, subentry_id: str) -> Appliance | None:
        return self.appliances_by_subentry().get(subentry_id)

    def appliance_of_key(self, key: str) -> Appliance | None:
        return next((a for a in self.appliances_by_subentry().values() if a.key == key), None)

    @callback
    def async_add_appliance(
        self,
        name: str,
        sources: Iterable[Source],
        examples: Iterable[Mapping[str, Any]] = (),
        teacher: Teacher | None = None,
    ) -> Appliance:
        """Create an appliance (a subentry), e.g. for a signature validated in
        the panel (#10) or labelled in the live view (#17, possibly without a
        source yet). Its entities and statistics follow at the next
        synchronisation, scheduled here."""
        entry = self.config_entry
        taken = {s.unique_id for s in entry.subentries.values() if s.unique_id}
        appliance = replace(
            new_appliance(name, sources, taken), examples=tuple(dict(e) for e in examples), teacher=teacher
        )
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
        self._async_update_issue()  # the Repairs notice may have been turned off
        if current == previous:
            return
        self.appliances = {a.key: a for a in current.values()}
        removed = [a for sid, a in previous.items() if sid not in current]
        # statistics of deleted appliances (not those of a merge: already moved)
        used = {a.statistic_id for a in current.values()}
        async_clear(self.hass, {a.statistic_id for a in removed} - used)
        for a in removed:
            self.last_seen.pop(a.key, None)
        # a deleted appliance that had taken over a device of the Energy
        # dashboard: its statistic is gone, offer to put the device back
        for a in removed:
            if a.key in self.takeovers and a.key not in {c.key for c in current.values()}:
                self._async_takeover_issue(a.key)
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
            examples = target.examples
            for example in other.examples:  # the labelled examples follow (#17)
                examples = add_example(examples, example)
            merged = replace(target, sources=sources, examples=examples)
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

    # -- review of the signatures (#10) ----------------------------------------
    @callback
    def _async_update_issue(self) -> None:
        """One Repairs notice while signatures wait for a review (unless
        turned off in the options); it only links to the panel."""
        enabled = self.config_entry.options.get(OPT_REVIEW_REPAIR, True)
        if self.review_count and enabled:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_REVIEW,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_REVIEW,
                translation_placeholders={"count": str(self.review_count), "url": PANEL_REVIEW_PATH},
                learn_more_url=PANEL_REVIEW_PATH,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_REVIEW)

    async def async_review(self) -> dict[str, Any]:
        """The review listing of the add-on (``GET /api/review``); the
        Repairs notice follows its count."""
        listing = await self.client.review()
        self.review_count = int((listing.get("counts") or {}).get("proposed") or 0)
        self._async_update_issue()
        return listing

    async def async_update_review(self) -> None:
        """Count the signatures to review and update the notice."""
        try:
            await self.async_review()
        except EnergyMapperApiError as err:
            if not self._no_endpoint(err):
                raise
            self.review_count = None  # an add-on before 0.6.0
            self._async_update_issue()

    def entity_source(self, entity_id: str, on_states: list[str] | None = None) -> Source:
        """The source of an entity linked to an appliance: a switch or any
        entity whose state tells when it runs (a switched load, with
        ``on_states`` when given), or a power sensor (a sub-meter). A sensor of
        the ``enum`` device class (an operation state) is a switched load."""
        domain = entity_id.split(".", 1)[0]
        if domain == "sensor":
            state = self.hass.states.get(entity_id)
            if state is not None and state.attributes.get("device_class") == STATE_SENSOR_CLASS:
                return relay(entity_id, on_states)
            return Source(KIND_SUBMETER, entity_id)
        if domain in SWITCH_DOMAINS:
            return relay(entity_id, on_states)
        raise ReviewError("invalid_format", f"{entity_id}: expected a switch or a power sensor")

    def check_free(self, *sources: Source) -> None:
        """Raise ``already_linked`` when an appliance already has one of them."""
        current = self.appliances_by_subentry().values()
        for wanted in dict.fromkeys(sources):
            owner = next((a for a in current if a.has(wanted)), None)
            if owner is not None:
                raise ReviewError("already_linked", f"{wanted.id} is already linked to {owner.name}")

    def check_teacher_free(self, entity_id: str) -> None:
        """Raise ``already_linked`` when an appliance already uses the sensor."""
        for other in self.appliances_by_subentry().values():
            if (other.teacher and other.teacher.entity_id == entity_id) or any(
                s.id == entity_id for s in other.sources
            ):
                raise ReviewError("already_linked", f"{entity_id} is already linked to {other.name}")

    async def async_push(self) -> None:
        """Push the appliances to the add-on (when they changed)."""
        await self._async_push(self.appliances_by_subentry().values())

    def check_power_sensor(self, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state is not None else {}
        if (
            entity_id.split(".", 1)[0] != "sensor"
            or state is None
            or (attrs.get("device_class") != "power" and attrs.get("unit_of_measurement") not in POWER_UNITS)
        ):
            raise ReviewError("invalid_format", f"{entity_id}: expected a power sensor")

    async def async_validate(
        self,
        sid: str,
        name: str,
        entity_id: str | None = None,
        on_states: list[str] | None = None,
        examples: Iterable[Mapping[str, Any]] = (),
    ) -> Appliance:
        """Validate a signature of the review: a new appliance with this
        signature as its source, or linked to an entity instead: a switch or
        any entity whose state tells when it runs (a switched load, with the
        ``on_states`` of the suggestion when given), or a power sensor (a
        sub-meter). A sensor of the ``enum`` device class (an operation
        state) is a switched load. The appliance is a subentry like any other
        (devices, sensors and statistics follow at the next synchronisation);
        the add-on records the decision with its key."""
        name = name.strip()
        if not name or len(name) > 100:
            raise ReviewError("invalid_format", "expected a name of 1 to 100 characters")
        registry = await self.client.signatures()
        sig = next((s for s in registry.get("signatures") or [] if s.get("id") == sid), None)
        if sig is None:
            raise ReviewError("not_found", f"no signature {sid}")
        source = signature(sig) if entity_id is None else self.entity_source(entity_id, on_states)
        self.check_free(source, signature(sig))
        appliance = self.async_add_appliance(name, [source], examples)
        _LOGGER.info("Signature %s validated as %s (%s)", sid, name, appliance.key)
        # the add-on must know the appliance before the decision names it
        await self._async_push(self.appliances_by_subentry().values())
        await self.client.review_decision(sid, "validate", appliance.key)
        await self.async_update_review()
        return appliance

    async def async_merge_signature(
        self,
        sid: str,
        key: str,
        change: Callable[[Appliance], Appliance] | None = None,
        drop: Iterable[str] = (),
    ) -> Appliance:
        """Validate a signature of the review as a part of an existing
        appliance (#43): the signature becomes one more source of the
        appliance with this key, like a merge (#38): its energy is added to
        the appliance's from now on, and the stored days are imported again
        so that the appliance's hourly statistic includes it for them too.
        The appliance keeps its name, key and statistic; the add-on records
        the decision with its key. ``change`` also updates the appliance (its
        labelled examples, #17) and the signature sources ``drop`` are
        removed (expected signatures the examples replaced)."""
        registry = await self.client.signatures()
        sig = next((s for s in registry.get("signatures") or [] if s.get("id") == sid), None)
        if sig is None:
            raise ReviewError("not_found", f"no signature {sid}")
        source = signature(sig)
        entry = self.config_entry
        async with self._sync_lock:
            current = self.appliances_by_subentry()
            target_id = next((i for i, a in current.items() if a.key == key), None)
            if target_id is None:
                raise ReviewError("not_found", f"no appliance {key}")
            owner = next((a for a in current.values() if a.has(source)), None)
            if owner is not None:
                raise ReviewError("already_linked", f"{sid} is already linked to {owner.name}")
            target = current[target_id]
            kept = tuple(s for s in target.sources if not (s.type == KIND_SIGNATURE and s.id in set(drop)))
            merged = replace(target, sources=(*kept, source))
            if change is not None:
                merged = change(merged)
            self._reimport.add(target.key)
            self.hass.config_entries.async_update_subentry(
                entry, appliance_subentries(entry)[target_id], data=merged.subentry_data()
            )
            await self._async_save()
        _LOGGER.info("Signature %s merged into %s", sid, target.name)
        # the add-on must know the new source before the decision names the appliance
        await self._async_push(self.appliances_by_subentry().values())
        await self.client.review_decision(sid, "validate", target.key)
        await self.async_update_review()
        return merged

    async def async_review_decision(self, sid: str, action: str) -> dict[str, Any]:
        """Wait, ignore or restore a signature (recorded by the add-on)."""
        answer = await self.client.review_decision(sid, action)
        await self.async_update_review()
        return answer

    # -- labelled examples (#17) -------------------------------------------------
    async def async_update_appliance(self, key: str, change: Callable[[Appliance], Appliance]) -> Appliance:
        """Change one appliance (its subentry) and push the appliances."""
        entry = self.config_entry
        async with self._sync_lock:
            target_id, target = self._target(key)
            changed = change(target)
            if changed != target:
                self.hass.config_entries.async_update_subentry(
                    entry, appliance_subentries(entry)[target_id], data=changed.subentry_data()
                )
        await self._async_push(self.appliances_by_subentry().values())
        return changed

    async def async_add_example(self, key: str, example: Mapping[str, Any]) -> Appliance:
        """Keep one more labelled example of an appliance (at most 20)."""
        return await self.async_update_appliance(
            key, lambda a: replace(a, examples=add_example(a.examples, example))
        )

    async def async_track_in_curve(
        self, key: str, example: Mapping[str, Any], cycling: bool = False
    ) -> dict[str, Any] | None:
        """An appliance created from the live view before its stop was known
        (no source yet), now that a whole use is known: the signature of that
        use (``POST /api/signatures``: an unnamed one of the registry at that
        power, else a new expected one) becomes its source, and the review
        records the decision. Returns ``{"signature", "created"}``, or None
        when the use is too small to make a signature. Raises
        ``ReviewError('conflict')`` when another appliance has that power."""
        appliance = self.appliance_of_key(key)
        t_off, power = example.get("t_off"), float(example.get("power_w") or 0.0)
        if appliance is None or not appliance.awaits_signature or t_off is None or power < 50:
            return None
        kind, band = signature_group(float(example["t_on"]), float(t_off), cycling)
        try:
            answer = await self.client.expect_signature(kind, band, round(power), appliance.name)
        except EnergyMapperApiError as err:
            if err.code == "conflict":
                raise ReviewError("conflict", err.message) from err
            raise
        sid = answer["signature"]["id"]
        await self.async_merge_signature(
            sid, key, lambda a: replace(a, examples=add_example(a.examples, {**example, "signature_id": sid}))
        )
        return answer

    @callback
    def async_live_events(self, session: int | None, events: Iterable[Mapping[str, Any]]) -> None:
        """Events of the live view (``LiveHub``): an open labelled example is
        completed when its event stops, dropped when it is cancelled."""
        if session is None:
            return
        waiting = {
            (int(e["live"]["session"]), int(e["live"]["id"])): a.key
            for a in self.appliances.values()
            for e in a.examples
            if e.get("t_off") is None and isinstance(e.get("live"), Mapping)
        }
        if not waiting:
            return
        for event in events:
            ref = (session, event.get("id"))
            key = waiting.get(ref)  # type: ignore[arg-type]
            if key is None or ref in self._completing:
                continue
            stopped = not event.get("running") and event.get("t_off") is not None
            if event.get("state") == "cancelled" or stopped:
                self._completing.add(ref)  # type: ignore[arg-type]
                self.config_entry.async_create_background_task(
                    self.hass, self._async_complete(key, ref, dict(event)), f"{DOMAIN} labelled example"
                )

    async def _async_complete(self, key: str, ref: tuple[int, int], event: dict[str, Any]) -> None:
        session, event_id = ref
        cancelled = event.get("state") == "cancelled"
        done: dict[str, Any] = {}

        def change(a: Appliance) -> Appliance:
            examples = []
            for e in a.examples:
                if e.get("live") != {"id": event_id, "session": session} or e.get("t_off") is not None:
                    examples.append(e)
                elif not cancelled:
                    e = {k: v for k, v in e.items() if k != "live"}
                    e.update(t_off=round(float(event["t_off"]), 1), power_w=round(float(event["power_w"])))
                    examples.append(e)
                    done.update(e)
            return replace(a, examples=tuple(examples))

        try:
            await self.async_update_appliance(key, change)
            if done:
                await self.async_track_in_curve(key, done, bool(event.get("session")))
            _LOGGER.info("Labelled example of %s %s", key, "dropped" if cancelled else "completed")
        except (EnergyMapperError, ReviewError) as err:
            _LOGGER.warning("Cannot complete the labelled example of %s: %s", key, err)
        finally:
            self._completing.discard(ref)

    async def _async_apply_examples(self) -> None:
        """What the nightly analysis found of the labelled examples
        (``GET /api/examples``): the signature they were found as becomes the
        appliance's when it has none seen yet (the review's validate
        decision; expected ones it replaces are dropped), and open examples
        get the stop of the activation found."""
        if not any(a.examples for a in self.appliances_by_subentry().values()):
            self.examples_status = {}
            return
        try:
            listing = await self.client.examples()
        except EnergyMapperApiError as err:
            if self._no_endpoint(err):
                self.examples_status = {}
                return
            raise
        self.examples_status = {i["key"]: i for i in listing.get("appliances") or [] if i.get("key")}
        for item in list(self.examples_status.values()):
            try:
                await self._async_follow_examples(item)
            except ReviewError as err:
                _LOGGER.info("Labelled examples of %s not linked: %s", item["key"], err.message)

    async def _async_follow_examples(self, item: Mapping[str, Any]) -> None:
        key = item["key"]
        found = {
            float(e["t_on"]): e["activation"]
            for e in item.get("examples") or []
            if e.get("t_off") is None and e.get("activation") and e.get("status") in ("found", "other")
        }

        def complete(a: Appliance) -> Appliance:
            examples = []
            for e in a.examples:
                act = found.get(float(e["t_on"])) if e.get("t_off") is None else None
                if act:
                    e = {k: v for k, v in e.items() if k != "live"}
                    e.update(t_off=float(max(act["end"], e["t_on"])), power_w=act["power_w"] or e["power_w"])
                examples.append(e)
            return replace(a, examples=tuple(examples))

        link = item.get("link")
        if link and self.appliance_of_key(key) is not None:
            await self.async_merge_signature(link["id"], key, complete, item.get("replace") or ())
            _LOGGER.info("Signature %s linked to %s by its labelled examples", link["id"], key)
        elif found and self.appliance_of_key(key) is not None:
            await self.async_update_appliance(key, complete)

    # -- learning from a measured device (#14) ---------------------------------
    async def async_teaching(self) -> dict[str, Any]:
        """The learning status of the taught appliances (``GET /api/teaching``);
        ``supported`` is false for an add-on before 0.11.0."""
        try:
            listing = await self.client.teaching()
        except EnergyMapperApiError as err:
            if self._no_endpoint(err):
                return {"supported": False, "teachers": []}
            raise
        return {"supported": True, "teachers": listing.get("teachers") or []}

    # -- nightly self-test (#15) ------------------------------------------------
    async def async_self_test(self) -> dict[str, Any]:
        """The success rate of the measured devices the add-on hides from its
        analysis at night (``GET /api/self_test``); ``supported`` is false for
        an add-on before 0.11.0."""
        try:
            listing = await self.client.self_test()
        except EnergyMapperApiError as err:
            if self._no_endpoint(err):
                return {"supported": False, "enabled": False, "devices": []}
            raise
        return {**listing, "supported": True, "devices": listing.get("devices") or []}

    def _target(self, key: str) -> tuple[str, Appliance]:
        current = self.appliances_by_subentry()
        found = next(((i, a) for i, a in current.items() if a.key == key), None)
        if found is None:
            raise ReviewError("not_found", f"no appliance {key}")
        return found

    def addon_at_least(self, version: str) -> bool:
        """Whether the add-on's last health answer says ``version`` or newer
        (unknown: assumed so, the add-on refuses what it does not know)."""
        current = (self.data or {}).get("version") if isinstance(self.data, Mapping) else None
        if not current:
            return True
        try:
            return tuple(int(x) for x in str(current).split(".")[:3]) >= tuple(
                int(x) for x in version.split(".")
            )
        except ValueError:
            return True

    def check_state_teacher(self, teacher: Teacher) -> None:
        """A connected appliance's teacher (#79): existing entities, the
        states in which it runs, a counter that is an energy sensor."""
        for entity_id in teacher.entities:
            if self.hass.states.get(entity_id) is None:
                raise ReviewError("invalid_format", f"{entity_id}: no such entity")
        if not teacher.on_states or any(not s or len(s) > 100 for s in teacher.on_states):
            raise ReviewError("invalid_format", "expected the states in which the appliance runs")
        if teacher.energy_entity and teacher.energy_entity.split(".", 1)[0] != "sensor":
            raise ReviewError("invalid_format", f"{teacher.energy_entity}: expected an energy sensor")

    async def async_teach(
        self,
        key: str | None,
        entity_id: str,
        *,
        mode: str = TEACHER_POWER,
        on_states: Iterable[str] = (),
        phase_entity: str | None = None,
        energy_entity: str | None = None,
        program_entity: str | None = None,
        name: str | None = None,
        track: bool = False,
    ) -> Appliance:
        """Learn the appliance ``key`` from what measures it: a power sensor
        (a temporary smart plug, #14), whose power the add-on stops
        subtracting and keeps as the appliance's ground truth; or the
        operation state of a connected appliance (#79, ``mode`` ``state``),
        whose runs the add-on measures on the house curve. Without ``key``, a
        new appliance ``name`` is created with this teacher (and, with
        ``track``, its operation state as a switched-load source)."""
        now = dt_util.utcnow().timestamp()
        if mode == TEACHER_STATE:
            states = tuple(dict.fromkeys(s.strip() for s in on_states if s and s.strip()))
            teacher = Teacher(
                entity_id, now, TEACHER_STATE, states, phase_entity, energy_entity, program_entity
            )
            self.check_state_teacher(teacher)
        else:
            teacher = Teacher(entity_id, now)
            self.check_power_sensor(entity_id)
        if not (await self.async_teaching())["supported"] or (
            teacher.is_state and not self.addon_at_least(CONNECTED_MIN_ADDON)
        ):
            raise ReviewError("not_supported", "the Energy Mapper add-on is too old to learn from this")
        entry = self.config_entry
        async with self._sync_lock:
            if key is None:
                name = (name or "").strip()
                if not name or len(name) > 100:
                    raise ReviewError("invalid_format", "expected a name of 1 to 100 characters")
                self.check_teacher_free(entity_id)
                sources = [relay(entity_id, teacher.on_states)] if track and teacher.is_state else []
                self.check_free(*sources)
                taught = self.async_add_appliance(name, sources, (), teacher)
            else:
                target_id, target = self._target(key)
                for other in self.appliances_by_subentry().values():
                    if other.key == key:
                        continue
                    if (other.teacher and other.teacher.entity_id == entity_id) or any(
                        s.id == entity_id for s in other.sources
                    ):
                        raise ReviewError("already_linked", f"{entity_id} is already linked to {other.name}")
                taught = replace(target, teacher=teacher)
                self.hass.config_entries.async_update_subentry(
                    entry, appliance_subentries(entry)[target_id], data=taught.subentry_data()
                )
        _LOGGER.info("%s learns from %s (%s)", taught.name, entity_id, teacher.mode)
        await self._async_push(self.appliances_by_subentry().values())
        return taught

    async def async_stop_teaching(self, key: str, use: bool) -> tuple[Appliance, dict[str, Any] | None]:
        """End the learning of the appliance ``key``. With ``use``, the
        learned signature (created by the add-on when needed) becomes a
        source of the appliance and the teaching sensor stops being one (the
        plug goes away): the appliance is tracked from the whole-house curve,
        and its stored days are imported again with the signature's energy."""
        _, target = self._target(key)
        if target.teacher is None:
            raise ReviewError("not_found", f"{target.name} is not learning from a sensor")
        answer = await self.client.apply_teaching(key) if use else None
        entry = self.config_entry
        async with self._sync_lock:
            target_id, target = self._target(key)
            sources = target.sources
            if answer is not None and target.teacher is not None:
                # the learned signature, and those of the other programmes of
                # a connected appliance (#79)
                learned = [signature(answer["signature"])] + [
                    signature(p["signature"]) for p in answer.get("programs") or [] if p.get("signature")
                ]
                others = [a for a in self.appliances_by_subentry().values() if a.key != key]
                for source in learned:
                    owner = next((a for a in others if a.has(source)), None)
                    if owner is not None:
                        raise ReviewError("already_linked", f"{source.id} is already linked to {owner.name}")
                teacher = target.teacher.entity_id
                if not target.teacher.is_state:
                    # the plug goes away; a connected appliance keeps its state source
                    sources = tuple(s for s in sources if not (s.type == KIND_SUBMETER and s.id == teacher))
                for source in learned:
                    if not any((s.type, s.id) == (source.type, source.id) for s in sources):
                        sources = (*sources, source)
                self._reimport.add(key)
            done = replace(target, sources=sources, teacher=None)
            self.hass.config_entries.async_update_subentry(
                entry, appliance_subentries(entry)[target_id], data=done.subentry_data()
            )
            await self._async_save()
        _LOGGER.info("%s stopped learning (learned signature used: %s)", target.name, answer is not None)
        await self._async_push(self.appliances_by_subentry().values())
        if answer is not None:
            await self.client.review_decision(answer["signature"]["id"], "validate", key)
            for item in answer.get("programs") or []:
                if (item.get("signature") or {}).get("id"):
                    await self.client.review_decision(item["signature"]["id"], "validate", key)
            await self.async_update_review()
        return done, answer

    # -- take over tracking in the Energy dashboard (#13) ---------------------
    async def _async_energy_manager(self) -> Any:
        # imported here: the energy integration is only needed for this
        from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

        return await async_get_manager(self.hass)

    def _entity_maps(self, entity_ids: Iterable[str]) -> tuple[dict[str, str | None], dict[str, str | None]]:
        """(source_entity, device id) of these entities and of their sources."""
        ent_reg = er.async_get(self.hass)
        source_of: dict[str, str | None] = {}
        device_of: dict[str, str | None] = {}
        todo = list(entity_ids)
        while todo:
            entity_id = todo.pop()
            if entity_id in device_of:
                continue
            entity = ent_reg.async_get(entity_id)
            device_of[entity_id] = entity.device_id if entity else None
            state = self.hass.states.get(entity_id)
            source = state.attributes.get("source_entity") if state else None
            source_of[entity_id] = source if isinstance(source, str) else None
            if source_of[entity_id]:
                todo.append(source_of[entity_id])
        return source_of, device_of

    def _names(self, devices: Iterable[dict[str, Any]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for d in devices:
            stat = d.get("stat_consumption")
            state = self.hass.states.get(stat) if stat else None
            if state is not None and state.name:
                out[stat] = state.name
        return out

    async def async_tracking(self) -> dict[str, Any]:
        """For the panel: the individual devices of the Energy dashboard that
        an appliance may take over, and for each appliance with a statistic
        of its own whether that statistic is in the dashboard, which device
        it took over, and the devices that are probably it."""
        manager = await self._async_energy_manager()
        devices = [dict(d) for d in (manager.data or {}).get("device_consumption") or []]
        candidates = pickable(devices)
        appliances = [a for a in self.appliances_by_subentry().values() if a.estimated]
        relays = {a.key: [s.id for s in a.sources if s.type == KIND_LOAD] for a in appliances}
        source_of, device_of = self._entity_maps(
            [*(d["stat_consumption"] for d in candidates), *(r for rs in relays.values() for r in rs)]
        )
        names = self._names(devices)
        out: list[dict[str, Any]] = []
        for a in sorted(appliances, key=lambda a: (a.name.casefold(), a.key)):
            tracked = in_dashboard(devices, a.statistic_id)
            record = self.takeovers.get(a.key)
            suggested = []
            if not tracked:
                for d in candidates:
                    reason = suggestion(d, relays[a.key], source_of, device_of)
                    if reason:
                        suggested.append({"stat_consumption": d["stat_consumption"], "reason": reason})
            out.append(
                {
                    "key": a.key,
                    "statistic_id": a.statistic_id,
                    "in_dashboard": tracked,
                    "taken_over": (
                        {
                            "stat_consumption": record["replaced"]["stat_consumption"],
                            "name": label(record["replaced"], names),
                        }
                        if record and tracked
                        else None
                    ),
                    "suggested": suggested,
                }
            )
        return {
            "devices": [
                {
                    "stat_consumption": d["stat_consumption"],
                    "name": label(d, names),
                    "included_in_stat": d.get("included_in_stat"),
                }
                for d in candidates
            ],
            "appliances": out,
        }

    async def async_take_over(self, key: str, stat_consumption: str) -> dict[str, Any]:
        """Replace the Energy dashboard device ``stat_consumption`` by the
        statistic of the appliance ``key``, in one update of the prefs (never
        both), and remember what was replaced for *Undo*. The user confirmed
        it in the panel."""
        async with self._energy_lock:
            appliance = next((a for a in self.appliances_by_subentry().values() if a.key == key), None)
            if appliance is None:
                raise TakeoverError("not_found", f"no appliance {key}")
            if not appliance.estimated:
                raise TakeoverError("no_statistic", f"{appliance.name} has no statistic of its own")
            if appliance.statistic_id not in await async_statistic_ids(self.hass):
                raise TakeoverError("no_statistic", f"{appliance.name}: no hourly energy imported yet")
            manager = await self._async_energy_manager()
            devices = list((manager.data or {}).get("device_consumption") or [])
            new_devices, record = take_over(devices, stat_consumption, appliance.statistic_id)
            record.update(name=appliance.name, at=dt.datetime.now(dt.UTC).isoformat())
            await manager.async_update({"device_consumption": new_devices})
            self.takeovers[key] = record
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_TAKEOVER_PREFIX + key)
            await self._async_save()
        _LOGGER.info("%s takes over %s in the Energy dashboard", appliance.name, stat_consumption)
        return {"key": key, "statistic_id": appliance.statistic_id, "replaced": record["replaced"]}

    async def async_undo_take_over(self, key: str) -> dict[str, Any]:
        """Put back the Energy dashboard device the appliance ``key`` took
        over, in place of its statistic (never both). Also the fix of the
        Repairs issue of a deleted appliance."""
        async with self._energy_lock:
            record = self.takeovers.get(key)
            if record is None:
                raise TakeoverError("not_found", f"{key} took over no device")
            manager = await self._async_energy_manager()
            devices = list((manager.data or {}).get("device_consumption") or [])
            await manager.async_update({"device_consumption": restore(devices, record)})
            del self.takeovers[key]
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_TAKEOVER_PREFIX + key)
            await self._async_save()
        _LOGGER.info("%s restored in the Energy dashboard", record["replaced"].get("stat_consumption"))
        return {"key": key, "restored": record["replaced"]}

    @callback
    def _async_takeover_issue(self, key: str) -> None:
        """A fixable Repairs issue: the appliance ``key`` is deleted, its
        statistic with it; restore the device it took over?"""
        record = self.takeovers[key]
        replaced = record["replaced"]
        state = self.hass.states.get(replaced.get("stat_consumption") or "")
        device = str(replaced.get("name") or (state.name if state else None) or replaced["stat_consumption"])
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_TAKEOVER_PREFIX + key,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="takeover_removed",
            translation_placeholders={"name": str(record.get("name") or key), "device": device},
            data={"entry_id": self.config_entry.entry_id, "key": key},
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
        # after the lock (a link takes it) and the flag (a link schedules a sync)
        try:
            await self._async_apply_examples()
        except EnergyMapperError as err:
            _LOGGER.warning("Cannot read the labelled examples from the Energy Mapper add-on: %s", err)
        except Exception:
            _LOGGER.exception("Unexpected error while applying the labelled examples")

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
        # appliances with a merged signature: every stored day again (#43)
        new |= {k for k in self._reimport if k in current and current[k].estimated}
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
        self._reimport.clear()

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
        await self.async_update_review()

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
