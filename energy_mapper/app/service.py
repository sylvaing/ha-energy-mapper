"""The nightly analysis: read Home Assistant, analyse one local day, store it."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

import selftest
from appliances import analysis_options, apply_to_registry, examples_of, teacher_entities, teachers
from const import VERSION
from energy_config import (
    DerivedConfig,
    constant_power,
    derive,
    flat_power_sensors,
    slow_solar_meter,
    tracking_gaps,
    without_flat,
)
from examples import status as examples_status
from ha_client import HAError
from nilm_core import analyze, parse_numeric
from nilm_core.correlation import CorrelationConfig
from nilm_core.correlation import suggest as correlate
from nilm_core.quality import visible_w
from nilm_core.registry import (
    Matcher,
    RegistryConfig,
    cold_models,
    cycle_templates,
    day_max_w,
    empty_registry,
    expected_signature,
    forget_after_days,
    nearest_signature,
    peak_reference,
)
from nilm_core.registry import update as update_registry
from nilm_core.steps import night_threshold
from review import Criteria
from review import annotated as annotated_review
from review import decide as decide_review
from review import listing as review_listing
from review import state as review_state
from settings import SettingsError
from settings import merge as merge_settings
from settings import validate as validate_settings
from statistic_ids import known_statistics
from storage import ReportStore
from suggest import (
    ATTRIBUTE_CANDIDATES,
    CHANGES_VERSION,
    CORRELATION_DAYS,
    SUGGESTIONS_VERSION,
    annotate_listing,
    attribute_keys,
    candidates,
    changes_file,
    day_changes_of,
    day_initial_of,
    devices,
    has_history,
    public_candidates,
    unrecorded,
)
from teaching import GROUND_TRUTH_KEY, STATE, day_state_truth, day_truth, program_signatures
from teaching import apply as apply_teaching
from teaching import status as teaching_status

_LOGGER = logging.getLogger(__name__)

DEFAULT_ANALYSIS_TIME = "04:30"
DEFAULT_BACKFILL_DAYS = 30
DEFAULT_HALF_LIFE_DAYS = 30
DEFAULT_REPORT_RETENTION_DAYS = 90
DEFAULT_PEAK_WINDOW_DAYS = 30
VISIBLE_DAYS = 14  # the noise of the curve: the median of the last days' reports
# the first grid reading must be at most this late after midnight, otherwise
# the recorder no longer holds the whole day (purged) and the day is skipped
MAX_START_GAP_S = 3600
# consecutive days without data after which the backfill stops
MAX_EMPTY_DAYS = 3
# entity suggestions (#41): signatures compared per night, at most
MAX_SUGGESTED_SIGNATURES = 50
SUGGESTED_STATES = ("proposed", "waiting")
# unit factors of an energy sensor to kWh (#79)
ENERGY_UNITS = {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0}


class AnalysisError(Exception):
    """A day that cannot be analysed. ``code`` is part of the HTTP API."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------
def derive_options(options: dict) -> dict:
    """Add-on options -> ``derive()`` options. Empty values mean "derive it
    from the Energy dashboard"."""
    out: dict = {}
    for key in ("grid_entity", "grid_export_entity", "pv_entity", "battery_entity", "house_entity"):
        value = (options.get(key) or "").strip()
        if value:
            out[key] = value
    for key in ("grid_invert", "battery_invert"):
        out[key] = bool(options.get(key))
    if options.get("adaptive_threshold"):  # the noise-adaptive threshold (#88), off by default
        out["adaptive_threshold"] = True
    if options.get("cold_appliances"):  # cold appliances (#89), off by default
        out["cold_appliances"] = True
    submeters = [s.strip() for s in options.get("submeters") or [] if s and s.strip()]
    if options.get("submeters_mode") == "manual":
        # the exact list, possibly empty: "no sub-meters" (#33)
        out["submeters"] = submeters
    else:
        if submeters:
            out["submeters"] = submeters
        excluded_meters = [s.strip() for s in options.get("excluded_submeters") or [] if s and s.strip()]
        if excluded_meters:
            out["excluded_submeters"] = excluded_meters
    loads = []
    for item in options.get("switched_loads") or []:
        entity = (item.get("entity_id") or "").strip()
        if not entity:
            continue
        states = [s for s in item.get("on_states") or [] if s]
        loads.append({"entity_id": entity, "name": item.get("name") or entity, "on_states": states or ["on"]})
    if loads:
        out["switched_loads"] = loads
    excluded = [s.strip() for s in options.get("excluded_switched_loads") or [] if s and s.strip()]
    if excluded:
        out["excluded_switched_loads"] = excluded
    out = analysis_options(out, options.get("appliances") or [])
    for key in ("step_threshold_w", "night_step_threshold_w", "peak_threshold_w"):
        if options.get(key):
            out[key] = float(options[key])
    return out


def parse_hhmm(value: str | None) -> dt.time:
    try:
        h, m = (int(x) for x in (value or DEFAULT_ANALYSIS_TIME).split(":"))
        return dt.time(h, m)
    except ValueError:
        _LOGGER.warning("invalid analysis_time %r, using %s", value, DEFAULT_ANALYSIS_TIME)
        h, m = (int(x) for x in DEFAULT_ANALYSIS_TIME.split(":"))
        return dt.time(h, m)


# ---------------------------------------------------------------------------
# calendar helpers (pure)
# ---------------------------------------------------------------------------
def day_bounds(day: dt.date, tz: dt.tzinfo) -> tuple[float, float]:
    """[00:00, 24:00) of a local day as unix timestamps (23 or 25 h on DST days)."""
    start = dt.datetime.combine(day, dt.time(0), tzinfo=tz)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=tz)
    return start.timestamp(), end.timestamp()


def days_to_analyze(today: dt.date, existing: set[str], backfill_days: int) -> list[dt.date]:
    """Past days without a report, newest first: yesterday and up to
    ``backfill_days`` days back."""
    window = max(1, backfill_days)
    days = [today - dt.timedelta(days=i) for i in range(1, window + 1)]
    return [d for d in days if d.isoformat() not in existing]


def next_run(now: dt.datetime, at: dt.time) -> dt.datetime:
    """Next occurrence of the local time ``at`` strictly after ``now`` (aware)."""
    candidate = dt.datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = dt.datetime.combine(now.date() + dt.timedelta(days=1), at, tzinfo=now.tzinfo)
    return candidate


def local_tz(name: str | None) -> dt.tzinfo:
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # missing tzdata or unknown zone
            _LOGGER.warning("time zone %r unavailable, using the container's", name)
    return dt.datetime.now().astimezone().tzinfo or dt.UTC


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
@dataclass
class Context:
    derived: DerivedConfig
    tz: dt.tzinfo
    entities: list[str] = field(default_factory=list)
    # entities compared with the signatures to review (suggest.py)
    candidates: list[str] = field(default_factory=list)
    # entities whose attributes are compared (#43)
    attribute_entities: list[str] = field(default_factory=list)
    # the Home Assistant device of the candidates and of the appliances' sources
    devices: dict[str, str] = field(default_factory=dict)
    # energy sensors read by the analysis: their unit factor to kWh (#79)
    energy_scales: dict[str, float] = field(default_factory=dict)


class Analyzer:
    def __init__(
        self,
        client,
        store: ReportStore,
        options: dict,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.store = store
        # add-on options, and the settings pushed by the integration on top
        self.base_options = options
        try:
            self.settings = validate_settings(store.load_settings(), options)
        except SettingsError as err:
            _LOGGER.warning("stored settings ignored: %s", err)
            self.settings = {}
        if self.settings:
            _LOGGER.info("settings from the integration: %s", ", ".join(sorted(self.settings)))
        # the user's appliances, declared by the integration (appliances.py)
        self.appliances: list[dict] = store.load_appliances()
        self.clock = clock
        self.lock = threading.Lock()
        # registry writes outside the analysis (labels): never waits for an analysis
        self._registry_lock = threading.Lock()
        self.tz: dt.tzinfo = local_tz(None)
        self._maxima: dict[str, float | None] = {}  # daily maxima already read
        # a PV meter in the last derived configuration (None: not derived yet)
        self._has_pv: bool | None = None

    @property
    def options(self) -> dict:
        """The effective options: the add-on options with the settings on
        top, and the user's appliances."""
        return {**merge_settings(self.base_options, self.settings), "appliances": self.appliances}

    @options.setter
    def options(self, value: dict) -> None:
        self.base_options = value

    def set_settings(self, settings: dict) -> None:
        """Store already validated settings; the next analysis uses them."""
        self.store.save_settings(settings)
        self.settings = settings
        _LOGGER.info("settings updated: %s", ", ".join(sorted(settings)) or "none (add-on options)")

    def set_appliances(self, appliances: list[dict]) -> None:
        """Store already validated appliances and apply them to the registry
        (labels); the next analysis uses their switched loads and sub-meters."""
        with self._registry_lock:
            self.store.save_appliances(appliances)
            self.appliances = appliances
            registry = self.store.load_registry()
            if registry is not None and apply_to_registry(registry, appliances, self.clock()):
                self.store.save_registry(registry)
        _LOGGER.info("appliances updated: %d", len(appliances))

    def expect_signature(self, kind: str, band: str, power_w: float, label: str) -> tuple[dict, bool]:
        """A signature for an appliance the user declares before it is seen
        ("Kettle, 2000 W"): the unlabelled signature it would be matched to
        when there is one, else a new expected signature. Returns (signature,
        created). Raises ``ValueError`` when the nearest signature already
        belongs to an appliance."""
        with self._registry_lock:
            registry = self.store.load_registry() or empty_registry(self.registry_config)
            near = nearest_signature(registry, kind, band, power_w)
            if near is not None:
                if near.get("label"):
                    raise ValueError(near["id"])
                return near, False
            sig = expected_signature(registry, kind, band, power_w, label, self.clock())
            registry["signatures"].append(sig)
            self.store.save_registry(registry)
            return sig, True

    @property
    def review_criteria(self) -> Criteria:
        options = self.options
        base = Criteria()

        def value(key, default, kind):
            try:
                v = kind(options.get(key) or default)
            except (TypeError, ValueError):
                return default
            return v if v > 0 else default

        return Criteria(
            min_uses=value("review_min_uses", base.min_uses, int),
            min_days=value("review_min_days", base.min_days, int),
            wait_days=value("review_wait_days", base.wait_days, int),
            wait_factor=value("review_wait_factor", base.wait_factor, float),
        )

    def _appliance_keys(self) -> set[str]:
        return {a["key"] for a in self.appliances}

    def review(self) -> dict:
        """The signatures to review (review.py), with their entity
        suggestions (suggest.py)."""
        registry = self.store.load_registry() or empty_registry(self.registry_config)
        listing = review_listing(registry, self.clock(), self.review_criteria, self._appliance_keys())
        return annotate_listing(listing, self.store.load_suggestions(), self.appliances)

    @property
    def correlation_config(self) -> CorrelationConfig:
        score = self._int_option("review_suggest_score", 80)
        return CorrelationConfig(min_score=min(max(score, 50), 100) / 100)

    def decide(self, sid: str, action: str, appliance: str | None = None) -> dict | None:
        """Record a review decision on a registry signature; returns it with
        its ``review_state``, or None when there is no such signature.
        Raises ``review.ReviewError``."""
        with self._registry_lock:
            registry = self.store.load_registry()
            sig = next((s for s in (registry or {}).get("signatures") or [] if s.get("id") == sid), None)
            if sig is None:
                return None
            now = self.clock()
            decide_review(sig, action, now, self.review_criteria, appliance)
            self.store.save_registry(registry)
        _LOGGER.info("signature %s: %s", sid, action)
        return annotated_review(sig, now, self.review_criteria, self._appliance_keys())

    def automatic(self) -> dict:
        """The sensors the analysis would use without the settings: the
        Energy dashboard and the add-on options. Reads Home Assistant (HAError)."""
        snap = self.client.snapshot()
        derived = derive(
            snap["prefs"],
            snap["states"],
            snap["registry"],
            derive_options(self.base_options),
            self.known_statistics(save=False),
        )
        cfg = derived.config
        if cfg is None:  # no grid meter found
            return {
                "grid_entity": None,
                "grid_export_entity": None,
                "pv_entity": None,
                "battery_entity": None,
                "house_entity": None,
                "submeters": [],
                "switched_loads": [],
            }
        return {
            "grid_entity": cfg.grid_entity,
            "grid_export_entity": cfg.grid_export_entity,
            "pv_entity": cfg.pv_entity,
            "battery_entity": cfg.battery_entity,
            "house_entity": cfg.house_entity,
            "submeters": [m.entity_id for m in cfg.submeters],
            "switched_loads": [
                {"entity_id": x.entity_id, "name": x.name, "on_states": list(x.on_states)}
                for x in cfg.switched_loads
            ],
        }

    def _int_option(self, key: str, default: int) -> int:
        try:
            value = int(self.options.get(key) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    @property
    def backfill_days(self) -> int:
        try:
            return int(self.options.get("backfill_days", DEFAULT_BACKFILL_DAYS))
        except (TypeError, ValueError):
            return DEFAULT_BACKFILL_DAYS

    @property
    def registry_config(self) -> RegistryConfig:
        return RegistryConfig(
            half_life_days=self._int_option("half_life_days", DEFAULT_HALF_LIFE_DAYS),
            peak_window_days=self._int_option("peak_window_days", DEFAULT_PEAK_WINDOW_DAYS),
        )

    @property
    def report_retention_days(self) -> int:
        return self._int_option("report_retention_days", DEFAULT_REPORT_RETENTION_DAYS)

    def today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self.clock(), self.tz).date()

    def known_statistics(self, save: bool = True) -> dict:
        """The Energy Mapper statistic ids of the appliances of the registry
        and of those declared by the integration, and those of the stored
        mapping (stored back when it changed)."""
        previous = self.store.load_statistic_ids()
        mapping = known_statistics(self.store.load_registry(), previous, self.appliances)
        if save and mapping != previous:
            try:
                self.store.save_statistic_ids(mapping)
            except OSError as err:
                _LOGGER.warning("statistic ids not stored: %s", err)
        return mapping

    def _derive(self, snap: dict, save: bool = True) -> DerivedConfig:
        """The configuration of the analysis from a snapshot of Home Assistant."""
        derived = derive(
            snap["prefs"],
            snap["states"],
            snap["registry"],
            derive_options(self.options),
            self.known_statistics(save),
        )
        if derived.config is None:
            raise AnalysisError(
                "no_grid", "no grid power sensor: configure the Energy dashboard or grid_entity"
            )
        self._has_pv = bool(derived.config.pv_entity)
        # switched loads: the power learned on previous days is used when a
        # day has no transition to measure it
        memory = {x["entity_id"]: x for x in (self.store.load_registry() or {}).get("switched_loads", [])}
        for load in derived.config.switched_loads:
            learned = memory.get(load.entity_id, {}).get("power_w")
            if learned and not load.power_w:
                load.power_w = float(learned)
        return derived

    def live_config(self) -> tuple:
        """The live view's configuration (#17): ``(AnalysisConfig, time zone)``,
        the meters, sub-meters and switched loads of the night. Reads Home
        Assistant; raises ``HAError`` and ``AnalysisError('no_grid')``."""
        snap = self.client.snapshot()
        tz = local_tz((snap.get("config") or {}).get("time_zone"))
        return self._derive(snap, save=False).config, tz

    def live_options(self) -> dict:
        """What the live configuration is derived from, besides Home Assistant:
        the live view restarts when it changes (settings, appliances' sources)."""
        return derive_options(self.options)

    def prepare(self) -> Context:
        """Read the Energy dashboard, states and registry; derive the configuration."""
        snap = self.client.snapshot()
        self.tz = local_tz((snap.get("config") or {}).get("time_zone"))
        derived = self._derive(snap)
        entities = list(derived.config.entities())
        entities += [t.energy_entity for t in derived.tracked if t.role == "switched"]
        entities += derived.teachers  # the ground truth of the taught appliances (#14)
        # the operation state, phases, programme and counter of the connected appliances (#79)
        connected = teacher_entities(self.appliances)
        # entities that may be a signature: not the meters the analysis uses;
        # switched loads and the entities linked to an appliance are (#43): a
        # signature matching one is part of its appliance (a merge)
        loads = {x.entity_id for x in derived.config.switched_loads}
        used = (set(entities) - loads) | set(self.options.get("excluded_switched_loads") or [])
        found = candidates(snap["states"], snap["registry"], used)
        with_attributes = candidates(snap["states"], snap["registry"], used, attributes=True)
        sourced = {
            s["entity_id"] for a in self.appliances for s in a.get("sources") or [] if s.get("entity_id")
        }
        device_of = devices(snap["registry"], set(found) | set(with_attributes) | loads | sourced)
        units = {
            s["entity_id"]: (s.get("attributes") or {}).get("unit_of_measurement") for s in snap["states"]
        }
        energy_scales = {e: ENERGY_UNITS.get(units.get(e) or "", 1.0) for e in connected}
        energy_scales.update(
            {
                t.energy_entity: ENERGY_UNITS.get(units.get(t.energy_entity) or "", 1.0)
                for t in derived.tracked
            }
        )
        return Context(
            derived,
            self.tz,
            list(dict.fromkeys(entities + connected)),
            found,
            with_attributes,
            device_of,
            energy_scales,
        )

    def _day_max(self, day: str) -> float | None:
        if day not in self._maxima:
            summary = self.store.load_summary(day)
            self._maxima[day] = day_max_w(summary) if summary else None
        return self._maxima[day]

    def peak_reference(self, day: dt.date) -> float | None:
        """Highest maximum of the stored days of the peak window before ``day``."""
        window = self.registry_config.peak_window_days
        days = [(day - dt.timedelta(days=i)).isoformat() for i in range(1, window + 1)]
        return peak_reference({d: self._day_max(d) for d in days}, day.isoformat(), window)

    def _run_day(self, day: dt.date, ctx: Context, self_test: bool = False) -> dict:
        """Analyse and store one day; with ``self_test``, then hide a few
        measured devices in a copy of the analysis (#15)."""
        cfg = ctx.derived.config
        start, end = day_bounds(day, ctx.tz)
        histories = self.client.history(ctx.entities, start - cfg.context_s, end)
        # power sensors that stay flat while their device counts energy (#79):
        # not sub-meters for this day
        ctx = self._without_flat(ctx, histories, start, end)
        cfg = ctx.derived.config
        # the meter that drives the timeline: the house meter when there is one (#97)
        timeline = cfg.house_entity or cfg.grid_entity
        grid = [ts for ts, _ in parse_numeric(histories.get(timeline, [])) if ts < end]
        in_day = [ts for ts in grid if ts >= start]
        if not in_day:
            raise AnalysisError("no_data", f"{day}: no grid data in the recorder")
        if min(grid) > start + MAX_START_GAP_S:
            raise AnalysisError("partial_day", f"{day}: the recorder history starts after midnight")
        tz = ctx.tz
        cfg.peak_reference_w = self.peak_reference(day)
        registry = self.store.load_registry()
        cfg.cycle_templates = cycle_templates(registry)
        # the cold appliances learned so far (#89): followed again today
        cfg.cold_models = cold_models(registry) if cfg.cold_config() is not None else []
        report = analyze(
            histories, cfg, start, end, local_hour=lambda ts: dt.datetime.fromtimestamp(ts, tz).hour
        )
        findings = (
            ctx.derived.findings
            + tracking_gaps(report, ctx.derived.tracked, histories)
            # a slow solar meter only matters while the PV is in the house value (#97)
            + (slow_solar_meter(report, cfg.pv_entity) if not cfg.house_entity else [])
        )
        truth = self._ground_truth(cfg, histories, start, end, set(ctx.derived.teachers), report, ctx)
        if truth:
            report[GROUND_TRUTH_KEY] = truth
        report.update(
            {
                "day": day.isoformat(),
                "time_zone": str(tz),
                "addon_version": VERSION,
                "config": ctx.derived.to_dict(),
                "findings": [f.to_dict() for f in findings],
            }
        )
        self.store.save(day.isoformat(), report)
        self._maxima[day.isoformat()] = day_max_w(report)
        self._store_changes_safely(day, ctx)
        self.store.set_state(last_run=self.clock())
        _LOGGER.info(
            "%s analysed: house %.1f kWh, base %d W, %d signatures, %d known loads",
            day,
            report["energy_kwh"].get("house", 0.0),
            report["energy_kwh"].get("base_w", 0),
            len(report["signatures"]),
            len(report["known_loads"]),
        )
        if self_test:
            self._self_test_safely(day, ctx, histories, start, end)
        return report

    def _without_flat(self, ctx: Context, histories: dict, start: float, end: float) -> Context:
        """The day's context without the power sensors that stay flat while
        their energy counter increases (#79): their device is ``energy_only``
        that day, with a ``flat_power_sensor`` finding. Only the energy
        counters of the devices whose power did not move are read (one
        request, rarely)."""
        try:
            flat = constant_power(ctx.derived.tracked, histories, start, end)
            if not flat:
                return ctx
            wanted = [t.energy_entity for t in flat if t.energy_entity not in histories]
            energy = {**histories, **(self.client.history(wanted, start, end) if wanted else {})}
            found = flat_power_sensors(flat, energy, start, end, ctx.derived.config.scales)
        except HAError as err:
            _LOGGER.warning("energy counters of the flat power sensors not read: %s", err)
            return ctx
        if not found:
            return ctx
        for f in found:
            _LOGGER.info("%s", f.detail)
        return replace(ctx, derived=without_flat(ctx.derived, found))

    def _ground_truth(
        self, cfg, histories, start: float, end: float, read: set[str], report: dict, ctx: Context
    ) -> list[dict]:
        """The runs of each teaching sensor (#14) or connected appliance (#79)
        on the day."""
        out = []
        for appliance, teacher in teachers(self.appliances):
            entity = teacher["entity_id"]
            if teacher.get("mode") == STATE:
                if entity not in ctx.entities:
                    continue  # declared after this analysis was prepared
                scale = ctx.energy_scales.get(teacher.get("energy_entity") or "", 1.0)
                out.append(day_state_truth(appliance["key"], teacher, histories, report, start, end, scale))
                continue
            if entity not in read:
                continue  # declared after this analysis was prepared
            scale = cfg.scales.get(entity, 1.0)
            points = [
                (ts, None if v is None else v * scale) for ts, v in parse_numeric(histories.get(entity, []))
            ]
            out.append(day_truth(appliance["key"], entity, points, start, end))
        return out

    # -- nightly self-test (#15) ------------------------------------------------
    @property
    def self_test_devices(self) -> int:
        """Measured devices hidden per night (0: no self-test)."""
        value = self.options.get("self_test_devices")
        if value is None or value == "":
            return selftest.DEFAULT_DEVICES
        try:
            return min(max(int(value), 0), selftest.MAX_DEVICES)
        except (TypeError, ValueError):
            return selftest.DEFAULT_DEVICES

    def _self_test(self, day: dt.date, ctx: Context, histories: dict, start: float, end: float) -> list[dict]:
        """Analyse the day again, in memory, with each chosen measured device
        not subtracted, and store whether its uses were found again. Uses the
        histories already read; never stores the hidden reports nor touches
        the registry."""
        count = self.self_test_devices
        if count <= 0:
            return []
        cfg = ctx.derived.config
        devices = dict(selftest.candidates(cfg.submeters))
        data = self.store.load_self_test() or selftest.empty()
        key = day.isoformat()
        done = selftest.tested_days(data, key)
        chosen = selftest.pick([e for e in devices if e not in done], data, count)
        if not chosen:
            return []
        registry = self.store.load_registry() or empty_registry(self.registry_config)
        appliance_of = {
            s["entity_id"]: a["key"]
            for a in self.appliances
            for s in a.get("sources") or []
            if s.get("type") == "submeter" and s.get("entity_id")
        }
        tz = ctx.tz

        def hour(ts: float) -> int:
            return dt.datetime.fromtimestamp(ts, tz).hour

        visible = self.visible_w(self.store.summaries_all())
        tests = []
        for entity in chosen:
            t0 = time.perf_counter()
            scale = cfg.scales.get(entity, 1.0)
            points = [
                (ts, None if v is None else v * scale) for ts, v in parse_numeric(histories.get(entity, []))
            ]
            hidden = selftest.hidden_config(cfg, entity)
            test = selftest.hide_and_find(
                key,
                entity,
                devices[entity],
                points,
                start,
                end,
                lambda c=hidden: analyze(histories, c, start, end, local_hour=hour),
                registry,
                selftest.previous_runs(data, entity),
                self.clock(),
                appliance_of.get(entity),
                cfg.smallest_step_w(),
                self.registry_config,
                visible_w=visible,
            )
            tests.append(test)
            ev = test.get("evaluation") or {}
            _LOGGER.info(
                "self-test %s on %s: %s, %s of %s use(s) found, %s detection(s) in %.2f s",
                entity,
                key,
                test["status"],
                ev.get("found", "-"),
                ev.get("uses", len(test["runs"])),
                ev.get("detections", "-"),
                time.perf_counter() - t0,
            )
        self.store.save_self_test(selftest.record(data, tests, self.today()))
        return tests

    def _self_test_safely(
        self, day: dt.date, ctx: Context, histories: dict, start: float, end: float
    ) -> None:
        """The self-test never costs the real analysis."""
        try:
            self._self_test(day, ctx, histories, start, end)
        except Exception:
            _LOGGER.exception("self-test of %s failed", day)

    def self_test(self) -> dict:
        """``GET /api/self_test``: the success rate of each tested device."""
        return selftest.summary(self.store.load_self_test(), self.today(), self.self_test_devices)

    # -- learning from a measured device (#14) ----------------------------------
    @property
    def step_threshold_w(self) -> float:
        try:
            return float(self.options.get("step_threshold_w") or 300.0)
        except (TypeError, ValueError):
            return 300.0

    @property
    def smallest_step_w(self) -> float:
        """The smallest step the analysis tracks: the night threshold when
        it applies (#77), else the step threshold. Under it an appliance is
        not tracked; whether the curve can show it at all is decided from
        the measured noise (:meth:`visible_w`)."""
        step = self.step_threshold_w
        try:
            night = float(self.options.get("night_step_threshold_w") or 0.0) or None
        except (TypeError, ValueError):
            night = None
        has_pv = self._has_pv if self._has_pv is not None else bool(self.options.get("pv_entity"))
        low = night_threshold(step, night, has_pv)
        return low if low is not None else step

    @staticmethod
    def visible_w(summaries: Mapping) -> float | None:
        """The smallest step the whole-house curve can show, from the noise
        measured on the last analysed days (#88); None before any report
        has it. What "too low for the whole-house curve" means."""
        return visible_w(list(summaries.values())[-VISIBLE_DAYS:])

    def teaching(self) -> dict:
        """The learning status of each appliance with a teacher."""
        summaries = self.store.summaries_all()
        registry = self.store.load_registry() or empty_registry(self.registry_config)
        now = self.clock()
        return {
            "teachers": [
                teaching_status(
                    a,
                    summaries,
                    registry,
                    now,
                    self.smallest_step_w,
                    self.registry_config,
                    visible_w=self.visible_w(summaries),
                )
                for a, _ in teachers(self.appliances)
            ]
        }

    def apply_teaching(self, key: str) -> dict | None:
        """The signature learned for the appliance ``key`` (created in the
        registry when needed): ``{"signature", "created", "learned"}``, None
        when the appliance has no teacher. Raises ``teaching.TeachingError``."""
        appliance = next((a for a, _ in teachers(self.appliances) if a["key"] == key), None)
        if appliance is None:
            return None
        summaries = self.store.summaries_all()
        with self._registry_lock:
            registry = self.store.load_registry() or empty_registry(self.registry_config)
            sig, created, learned = apply_teaching(
                appliance,
                summaries,
                registry,
                self.clock(),
                self.smallest_step_w,
                self.registry_config,
                self.visible_w(summaries),
            )
            # the other programmes of a connected appliance (#79)
            others = program_signatures(appliance, learned, registry, self.clock(), self.registry_config)
            if created or any(new for _, new in others):
                self.store.save_registry(registry)
        _LOGGER.info("signature %s learned for %s (%s)", sig["id"], key, "new" if created else "existing")
        return {
            "signature": sig,
            "created": created,
            "learned": learned,
            "programs": [{"signature": s, "created": new} for s, new in others],
        }

    # -- labelled examples (#17) ------------------------------------------------
    def examples(self) -> dict:
        """``GET /api/examples``: what the analysed days say about the
        labelled examples of each appliance (``examples.py``)."""
        summaries = self.store.summaries_all()
        registry = self.store.load_registry() or empty_registry(self.registry_config)
        matcher = Matcher(registry)
        visible = self.visible_w(summaries)
        return {
            "last_day": next(reversed(summaries), None) if summaries else None,
            "appliances": [
                examples_status(a, summaries, registry, self.smallest_step_w, matcher, visible_w=visible)
                for a, _ in examples_of(self.appliances)
            ],
        }

    def analyze_day(self, day: dt.date) -> dict:
        """Analyse one past day now (overwrites its report)."""
        with self.lock:
            ctx = self.prepare()
            if day >= self.today():
                raise AnalysisError("invalid_day", f"{day} is not over yet")
            report = self._run_day(day, ctx)
            self._maintain_safely()
            self._suggest_safely(ctx)
            return report

    # -- entity suggestions (#41) -----------------------------------------------
    def _store_changes(self, day: dt.date, ctx: Context) -> None:
        """Read and keep the state changes of the candidate entities on one day."""
        if not ctx.candidates and not ctx.attribute_entities:
            return
        start, end = day_bounds(day, ctx.tz)
        histories = self.client.history(ctx.candidates, start, end) if ctx.candidates else {}
        attributes = {}
        if ctx.attribute_entities:
            # a separate request with attributes, for a few entities only
            wanted = {e: ATTRIBUTE_CANDIDATES[e.split(".", 1)[0]] for e in ctx.attribute_entities}
            attributes = self.client.history_attributes(wanted, start, end)
        cfg = self.correlation_config
        data = changes_file(day.isoformat(), histories, ctx.candidates, start, end, cfg, attributes)
        old = self.store.load_changes(day.isoformat())
        if old is not None and has_history(old) and not has_history(data):
            return  # re-read after the recorder purged it: keep what was read before
        self.store.save_changes(day.isoformat(), data)

    def _store_changes_safely(self, day: dt.date, ctx: Context) -> None:
        """Changes of a day just analysed: a failure only costs its suggestions."""
        try:
            self._store_changes(day, ctx)
        except Exception as err:  # HAError, or a malformed history
            _LOGGER.warning("%s: state changes of the candidate entities not read: %s", day, err)

    def _needs_suggestions(self) -> bool:
        stored = self.store.load_suggestions() or {}
        return stored.get("day") != self.today().isoformat() and bool(self.store.summary_days())

    def suggest(self, ctx: Context | None) -> dict:
        """Score the candidate entities of the signatures to review over the
        last ``CORRELATION_DAYS`` days and store the suggestions. With a
        context, the days of the window without stored changes are read
        first (one history request per day); without one, the candidates of
        the previous run and the stored changes only are used."""
        today = self.today()
        t0 = time.perf_counter()
        summaries = self.store.summaries_all()
        days = [d for d in summaries if 0 < (today - dt.date.fromisoformat(d)).days <= CORRELATION_DAYS]
        previous = self.store.load_suggestions() or {}
        if ctx is not None:
            found = ctx.candidates + attribute_keys(ctx.attribute_entities)
            device_of = ctx.devices
        else:
            found = list(previous.get("entities") or [])
            device_of = dict(previous.get("devices") or {})
        fetched = 0
        if ctx is not None and found:
            have = set(self.store.changes_days())
            for day in sorted(days, reverse=True):
                # files written before 0.8.0 lack the attributes and the
                # initial states: read again once while the recorder has them
                if day in have and (self.store.load_changes(day) or {}).get("version", 1) >= CHANGES_VERSION:
                    continue
                try:
                    self._store_changes(dt.date.fromisoformat(day), ctx)
                except HAError as err:
                    _LOGGER.warning("state changes of the candidate entities not read: %s", err)
                    break
                fetched += 1
        files = {d: f for d in days if (f := self.store.load_changes(d)) is not None}
        registry = self.store.load_registry() or empty_registry(self.registry_config)
        now, criteria, keys = self.clock(), self.review_criteria, self._appliance_keys()
        sids = [
            s["id"]
            for s in registry.get("signatures") or []
            if review_state(s, now, criteria, keys) in SUGGESTED_STATES
        ][:MAX_SUGGESTED_SIGNATURES]
        cfg = self.correlation_config
        wanted = set(found)
        changes = {
            day: {e: ch for e, ch in per.items() if e in wanted} for day, per in day_changes_of(files).items()
        }
        initial = day_initial_of(files)
        result = (
            correlate({d: summaries[d] for d in days}, registry, sids, changes, cfg, initial) if sids else {}
        )
        plain = [e for e in found if "::" not in e]
        data = {
            "version": SUGGESTIONS_VERSION,
            "day": today.isoformat(),
            "computed_at": now,
            "days": len(files),
            "candidates": len(plain),
            "attribute_candidates": len(found) - len(plain),
            "min_score": cfg.min_score,
            "unrecorded": unrecorded(files, plain),
            "entities": found,
            "devices": device_of,
            "signatures": public_candidates(result),
        }
        self.store.save_suggestions(data)
        _LOGGER.info(
            "suggestions: %d of %d signature(s) matched with %d candidate entities over %d day(s) "
            "(%d read) in %.2f s",
            len(result),
            len(sids),
            len(found),
            len(files),
            fetched,
            time.perf_counter() - t0,
        )
        return data

    def _suggest_safely(self, ctx: Context | None) -> None:
        try:
            self.suggest(ctx)
        except Exception:
            _LOGGER.exception("entity suggestions failed")

    # -- registry and retention ----------------------------------------------
    def _summary_tz(self, summaries: dict[str, dict]) -> dt.tzinfo:
        for day in sorted(summaries, reverse=True):
            name = summaries[day].get("time_zone")
            if name:
                return local_tz(name)
        return self.tz

    def maintain(self) -> dict:
        with self.lock:
            return self._maintain()

    def _maintain(self) -> dict:
        """Prune the storage, then update the signature registry from the
        stored summaries (rebuilt from them when missing or corrupt)."""
        today = self.today()
        config = self.registry_config
        removed = self.store.prune(
            today,
            self.report_retention_days,
            forget_after_days(config.half_life_days, config.forget_weight),
            CORRELATION_DAYS + 1,
        )
        for day in removed["summaries"]:
            self._maxima.pop(day, None)
        t0 = time.perf_counter()
        previous = self.store.load_registry()
        if previous is None and self.store.registry_path.exists():
            _LOGGER.warning("signature registry unreadable, rebuilding it from the day summaries")
        summaries = self.store.summaries_all()
        tz = self._summary_tz(summaries)
        now = dt.datetime.combine(today, dt.time(0), tzinfo=tz).timestamp()
        registry = update_registry(
            previous, summaries, now, config, local_hour=lambda ts: dt.datetime.fromtimestamp(ts, tz).hour
        )
        with self._registry_lock:
            # review decisions taken during the update win over those it carried
            stored = self.store.load_registry()
            reviews = {s.get("id"): s.get("review") for s in (stored or {}).get("signatures") or []}
            for sig in registry["signatures"]:
                if sig["id"] in reviews:
                    if reviews[sig["id"]]:
                        sig["review"] = reviews[sig["id"]]
                    else:
                        sig.pop("review", None)
            # the appliances may have changed during the update
            apply_to_registry(registry, self.appliances, now)
            self.store.save_registry(registry)
        self.store.set_state(last_maintenance=today.isoformat())
        _LOGGER.info(
            "registry %s: %d signature(s) from %d day(s) in %.2f s; pruned %d report(s), %d summary(ies)",
            "updated" if previous else "rebuilt",
            len(registry["signatures"]),
            len(summaries),
            time.perf_counter() - t0,
            len(removed["reports"]),
            len(removed["summaries"]),
        )
        return {"registry": registry, "removed": removed}

    def _maintain_safely(self) -> None:
        """Maintenance after an analysis: its failure must not lose the reports."""
        try:
            self._maintain()
        except Exception:
            _LOGGER.exception("registry update or pruning failed")

    def _needs_maintenance(self) -> bool:
        if not self.store.summary_days():
            return False
        if self.store.state().get("last_maintenance") != self.today().isoformat():
            return True
        registry = self.store.load_registry()
        # a registry written before 0.9.0 has no occurrences (the day raster):
        # updated at once from the stored days rather than the next night
        return registry is None or any(
            "occurrences" not in s for s in registry.get("signatures") or [] if not s.get("expected")
        )

    def catch_up(self) -> dict:
        """Analyse yesterday and the missing days of the backfill window,
        newest first, until the recorder runs out of history.

        Raises ``HAError`` and ``AnalysisError('no_grid')``: nothing can be
        done before they are fixed. Any other failure only skips its day."""
        with self.lock:
            missing = days_to_analyze(self.today(), set(self.store.summary_days()), self.backfill_days)
            done, failed = [], []
            if not missing:
                if self._needs_maintenance():
                    self._maintain_safely()
                if self._needs_suggestions():
                    # from the stored changes only: no Home Assistant request
                    self._suggest_safely(None)
                return {"analysed": done, "failed": failed}
            ctx = self.prepare()
            # the time zone may have changed with prepare()
            missing = days_to_analyze(self.today(), set(self.store.summary_days()), self.backfill_days)
            empty = 0
            for day in missing:
                try:
                    # the newest day analysed by the pass hides a measured device (#15)
                    self._run_day(day, ctx, self_test=not done)
                    done.append(day.isoformat())
                    empty = 0
                except AnalysisError as err:
                    # a day without data may be an outage: look further back,
                    # but a partial day or a run of empty days is the start
                    # of the recorder history
                    empty = empty + 1 if err.code == "no_data" else 0
                    if err.code == "partial_day" or empty >= MAX_EMPTY_DAYS:
                        _LOGGER.info("%s; older days are not analysed", err.message)
                        break
                    _LOGGER.warning("%s", err.message)
                    failed.append(day.isoformat())
                except HAError:
                    raise
                except Exception:
                    _LOGGER.exception("analysis of %s failed", day)
                    failed.append(day.isoformat())
            if done or self._needs_maintenance():
                self._maintain_safely()
            if done or self._needs_suggestions():
                self._suggest_safely(ctx)
            return {"analysed": done, "failed": failed}


class Scheduler:
    """Catch up on start, then every day at ``analysis_time``."""

    RETRY_S = (60, 300, 900, 3600)

    def __init__(self, analyzer: Analyzer, analysis_time: str | None):
        self.analyzer = analyzer
        self._at = parse_hhmm(analysis_time)
        self.stop = threading.Event()
        self._failures = 0

    @property
    def at(self) -> dt.time:
        """Time of the nightly analysis (a pushed setting wins; it applies
        from the next pass)."""
        value = self.analyzer.settings.get("analysis_time")
        return parse_hhmm(value) if value else self._at

    def run_once(self) -> float:
        """One pass; returns the number of seconds to wait before the next."""
        try:
            result = self.analyzer.catch_up()
            if result["analysed"] or result["failed"]:
                _LOGGER.info(
                    "pass done: %d day(s) analysed, %d failed", len(result["analysed"]), len(result["failed"])
                )
            self._failures = 0
        except HAError as err:
            self._failures += 1
            delay = self.RETRY_S[min(self._failures, len(self.RETRY_S)) - 1]
            _LOGGER.warning("Home Assistant unavailable (%s); retrying in %d s", err, delay)
            return delay
        except AnalysisError as err:
            _LOGGER.error("%s", err.message)
        except Exception:
            _LOGGER.exception("analysis pass failed")
        now = dt.datetime.fromtimestamp(self.analyzer.clock(), self.analyzer.tz)
        nxt = next_run(now, self.at)
        _LOGGER.info("next analysis at %s", nxt.isoformat(timespec="minutes"))
        return max((nxt - now).total_seconds(), 1.0)

    def loop(self) -> None:
        while not self.stop.is_set():
            delay = self.run_once()
            self.stop.wait(delay)
