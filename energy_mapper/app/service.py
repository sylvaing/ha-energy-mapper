"""The nightly analysis: read Home Assistant, analyse one local day, store it."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from const import VERSION
from energy_config import DerivedConfig, derive, tracking_gaps
from ha_client import HAError
from nilm_core import analyze, parse_numeric
from nilm_core.registry import RegistryConfig, day_max_w, forget_after_days, peak_reference
from nilm_core.registry import update as update_registry
from settings import SettingsError
from settings import merge as merge_settings
from settings import validate as validate_settings
from statistic_ids import known_statistics
from storage import ReportStore

_LOGGER = logging.getLogger(__name__)

DEFAULT_ANALYSIS_TIME = "04:30"
DEFAULT_BACKFILL_DAYS = 30
DEFAULT_HALF_LIFE_DAYS = 30
DEFAULT_REPORT_RETENTION_DAYS = 90
DEFAULT_PEAK_WINDOW_DAYS = 30
# the first grid reading must be at most this late after midnight, otherwise
# the recorder no longer holds the whole day (purged) and the day is skipped
MAX_START_GAP_S = 3600
# consecutive days without data after which the backfill stops
MAX_EMPTY_DAYS = 3


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
    for key in ("grid_entity", "grid_export_entity", "pv_entity", "battery_entity"):
        value = (options.get(key) or "").strip()
        if value:
            out[key] = value
    for key in ("grid_invert", "battery_invert"):
        out[key] = bool(options.get(key))
    submeters = [s.strip() for s in options.get("submeters") or [] if s and s.strip()]
    if submeters:
        out["submeters"] = submeters
    loads = []
    for item in options.get("switched_loads") or []:
        entity = (item.get("entity_id") or "").strip()
        if not entity:
            continue
        states = [s for s in item.get("on_states") or [] if s]
        loads.append({"entity_id": entity, "name": item.get("name") or entity, "on_states": states or ["on"]})
    if loads:
        out["switched_loads"] = loads
    for key in ("step_threshold_w", "peak_threshold_w"):
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
            self.settings = validate_settings(store.load_settings())
        except SettingsError as err:
            _LOGGER.warning("stored settings ignored: %s", err)
            self.settings = {}
        if self.settings:
            _LOGGER.info("settings from the integration: %s", ", ".join(sorted(self.settings)))
        self.clock = clock
        self.lock = threading.Lock()
        self.tz: dt.tzinfo = local_tz(None)
        self._maxima: dict[str, float | None] = {}  # daily maxima already read

    @property
    def options(self) -> dict:
        """The effective options: the add-on options with the settings on top."""
        return merge_settings(self.base_options, self.settings)

    @options.setter
    def options(self, value: dict) -> None:
        self.base_options = value

    def set_settings(self, settings: dict) -> None:
        """Store already validated settings; the next analysis uses them."""
        self.store.save_settings(settings)
        self.settings = settings
        _LOGGER.info("settings updated: %s", ", ".join(sorted(settings)) or "none (add-on options)")

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
                "submeters": [],
                "switched_loads": [],
            }
        return {
            "grid_entity": cfg.grid_entity,
            "grid_export_entity": cfg.grid_export_entity,
            "pv_entity": cfg.pv_entity,
            "battery_entity": cfg.battery_entity,
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
        """The Energy Mapper statistic ids of the appliances of the registry,
        and those of the stored mapping (stored back when it changed)."""
        previous = self.store.load_statistic_ids()
        mapping = known_statistics(self.store.load_registry(), previous)
        if save and mapping != previous:
            try:
                self.store.save_statistic_ids(mapping)
            except OSError as err:
                _LOGGER.warning("statistic ids not stored: %s", err)
        return mapping

    def prepare(self) -> Context:
        """Read the Energy dashboard, states and registry; derive the configuration."""
        snap = self.client.snapshot()
        self.tz = local_tz((snap.get("config") or {}).get("time_zone"))
        derived = derive(
            snap["prefs"],
            snap["states"],
            snap["registry"],
            derive_options(self.options),
            self.known_statistics(),
        )
        if derived.config is None:
            raise AnalysisError(
                "no_grid", "no grid power sensor: configure the Energy dashboard or grid_entity"
            )
        entities = list(derived.config.entities())
        entities += [t.energy_entity for t in derived.tracked if t.role == "switched"]
        # switched loads: the power learned on previous days is used when a
        # day has no transition to measure it
        memory = {x["entity_id"]: x for x in (self.store.load_registry() or {}).get("switched_loads", [])}
        for load in derived.config.switched_loads:
            learned = memory.get(load.entity_id, {}).get("power_w")
            if learned and not load.power_w:
                load.power_w = float(learned)
        return Context(derived, self.tz, list(dict.fromkeys(entities)))

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

    def _run_day(self, day: dt.date, ctx: Context) -> dict:
        cfg = ctx.derived.config
        start, end = day_bounds(day, ctx.tz)
        histories = self.client.history(ctx.entities, start - cfg.context_s, end)
        grid = [ts for ts, _ in parse_numeric(histories.get(cfg.grid_entity, [])) if ts < end]
        in_day = [ts for ts in grid if ts >= start]
        if not in_day:
            raise AnalysisError("no_data", f"{day}: no grid data in the recorder")
        if min(grid) > start + MAX_START_GAP_S:
            raise AnalysisError("partial_day", f"{day}: the recorder history starts after midnight")
        tz = ctx.tz
        cfg.peak_reference_w = self.peak_reference(day)
        report = analyze(
            histories, cfg, start, end, local_hour=lambda ts: dt.datetime.fromtimestamp(ts, tz).hour
        )
        findings = ctx.derived.findings + tracking_gaps(report, ctx.derived.tracked, histories)
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
        self.store.set_state(last_run=self.clock())
        _LOGGER.info(
            "%s analysed: house %.1f kWh, base %d W, %d signatures, %d known loads",
            day,
            report["energy_kwh"].get("house", 0.0),
            report["energy_kwh"].get("base_w", 0),
            len(report["signatures"]),
            len(report["known_loads"]),
        )
        return report

    def analyze_day(self, day: dt.date) -> dict:
        """Analyse one past day now (overwrites its report)."""
        with self.lock:
            ctx = self.prepare()
            if day >= self.today():
                raise AnalysisError("invalid_day", f"{day} is not over yet")
            report = self._run_day(day, ctx)
            self._maintain_safely()
            return report

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
            today, self.report_retention_days, forget_after_days(config.half_life_days, config.forget_weight)
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
        return (
            self.store.state().get("last_maintenance") != self.today().isoformat()
            or self.store.load_registry() is None
        ) and bool(self.store.summary_days())

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
                return {"analysed": done, "failed": failed}
            ctx = self.prepare()
            # the time zone may have changed with prepare()
            missing = days_to_analyze(self.today(), set(self.store.summary_days()), self.backfill_days)
            empty = 0
            for day in missing:
                try:
                    self._run_day(day, ctx)
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
