"""Benchmark the engine on real history, with the measured devices as ground truth.

An installation with sub-meters already knows when each measured device ran.
This tool replays days of raw history through ``nilm_core`` and, for each
measured device in turn, hides it (the device is left in the whole-house
curve, as if it had no meter) and scores how well the analysis finds its
uses again: the nightly self-test (#15), for every device and every day at
once. It also gives the meters' quality (reporting interval, noise of the
curve: the report's ``quality.meters``) and the share of the house energy in
each category.

A second score needs no signature (``free``): a use of the hidden device is
found when at least half of its duration is covered by activations of
similar power (within 30 %), whatever the signature they were given, the
small uses of the adaptive threshold (``--adaptive``, #88) among them. The
score through the registry compares with the power read on the device's own
meter and depends on the other signatures near it; this one tells whether
the curve showed the use at all.

Run it before and after a change of the engine and compare the two results:

    python3 tools/benchmark.py ~/export.csv --grid sensor.grid_power \\
        --pv sensor.pv_power --submeter sensor.freezer_power=Freezer \\
        --submeter sensor.pump_power --switch switch.water_heater=on \\
        --json before.json
    ... change the engine ...
    python3 tools/benchmark.py ~/export.csv ... --json after.json --compare before.json

The history is the CSV export of ``tools/replay_csv.py`` (one row per state:
``entity_id,state,last_updated_ts``), or a folder of ``.json`` / ``.json.gz``
files, each ``{entity_id: [[timestamp, state], ...]}``. Keep such exports
OUTSIDE the repository: they describe a real household.

What a device's line means:

- ``tested``: the signature learned from its own runs (all the days of the
  export) is above the smallest step threshold; each day was analysed again
  without its sub-meter and scored (precision, recall, energy ratio).
- ``below_threshold``: the curve cannot show it with these thresholds. Its
  uses and energy are still counted, with nothing found, so that a lower
  threshold (``--threshold``, ``--night-threshold``) or a later change of
  the engine shows its gain.
- ``no_use``: the device never ran in the export.

The registry-free score is given for every device that ran, whatever its
status, overall and apart from / during the solar hours.

With ``--cold`` (cold appliances, #89; needs ``--adaptive``) the days are
analysed in order, each one with the cold appliances the days before it
taught (as the add-on does from its registry), and a hidden device also gets
the score of the cold appliance its absence adds (``cold``): the one the
real analysis does not have. Its runs against the device's (found, right,
apart from and during the solar hours), and its daily energy against the
device's own meter, the estimated part included. The runs of the cold
appliances count as uses in the registry-free score too.

Standard library only, not shipped in the add-on image.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import dataclasses
import datetime as dt
import gzip
import json
import pathlib
import statistics
import sys
import zoneinfo
from collections import defaultdict
from collections.abc import Mapping, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))

import selftest  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig, SwitchedLoadConfig, analyze  # noqa: E402
from nilm_core.cold import GROUP as COLD  # noqa: E402
from nilm_core.cold import alike  # noqa: E402
from nilm_core.groundtruth import merge_evaluations, runs_of  # noqa: E402
from nilm_core.registry import cold_models  # noqa: E402
from nilm_core.registry import update as update_registry  # noqa: E402
from nilm_core.signal import parse_numeric  # noqa: E402
from nilm_core.steps import PV_QUIET_W  # noqa: E402

VERSION = 1
BELOW_THRESHOLD = "below_threshold"
FREE_POWER_TOL = 0.3  # the registry-free score: an activation of similar power
FREE_COVER = 0.5  # ... covering this share of a use finds it
MARGIN_S = 1800.0  # history kept after a day (a run ending after midnight, baselines)

Histories = dict[str, list[tuple[float, str]]]


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------
def load(path: str) -> Histories:
    """The raw histories of a CSV export or of a folder of JSON files."""
    p = pathlib.Path(path)
    hist: dict[str, list[tuple[float, str]]] = defaultdict(list)
    if p.is_dir():
        for f in sorted(p.iterdir()):
            if f.name.endswith(".json.gz"):
                with gzip.open(f, "rt") as fh:
                    data = json.load(fh)
            elif f.name.endswith(".json"):
                data = json.loads(f.read_text())
            else:
                continue
            for entity, rows in data.items():
                hist[entity].extend((float(t), str(s)) for t, s in rows)
    else:
        with open(p, newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 3:
                    continue
                try:
                    ts = float(row[2])
                except ValueError:  # a header
                    continue
                hist[row[0]].append((ts, row[1]))
    # files of consecutive days overlap (each one starts before midnight)
    return {e: sorted(set(v)) for e, v in hist.items()}


def window(hist: Mapping[str, Sequence[tuple[float, str]]], a: float, b: float) -> Histories:
    """The states within ``[a, b]``, with the last one before ``a`` (the
    state at ``a``: Home Assistant records a state when it changes)."""
    out: Histories = {}
    for entity, rows in hist.items():
        i = bisect.bisect_left(rows, (a,))
        j = bisect.bisect_right(rows, (b, "￿"))
        out[entity] = list(rows[max(i - 1, 0) : j])
    return out


def days_of(ts: Sequence[float], tz: dt.tzinfo | None) -> list[tuple[str, float, float]]:
    """The whole local days the readings cover: ``(day, start, end)``."""
    if not ts:
        return []
    first = dt.datetime.fromtimestamp(ts[0], tz).date() + dt.timedelta(days=1)
    last = dt.datetime.fromtimestamp(ts[-1], tz).date()
    out = []
    day = first
    while day < last:
        nxt = day + dt.timedelta(days=1)
        start = dt.datetime.combine(day, dt.time(), tz).timestamp()
        end = dt.datetime.combine(nxt, dt.time(), tz).timestamp()
        out.append((day.isoformat(), start, end))
        day = nxt
    return out


# ---------------------------------------------------------------------------
# the meters
# ---------------------------------------------------------------------------
def _merge_quality(parts: Sequence[Mapping]) -> dict:
    """The meters over the days: the median of each day's
    ``quality.meters`` (:func:`nilm_core.quality.meter_quality`)."""

    def med(path: tuple[str, str]) -> float | None:
        values = [p[path[0]][path[1]] for p in parts if (p.get(path[0]) or {}).get(path[1]) is not None]
        return round(statistics.median(values), 1) if values else None

    return {
        "grid": {"median_s": med(("grid", "median_s")), "p90_s": med(("grid", "p90_s"))},
        "solar": {"median_s": med(("solar", "median_s")), "p90_s": med(("solar", "p90_s"))},
        "noise_w": {"no_solar": med(("noise_w", "no_solar")), "solar": med(("noise_w", "solar"))},
    }


# ---------------------------------------------------------------------------
# the registry-free score
# ---------------------------------------------------------------------------
def _spans(activation: Mapping) -> list[tuple[float, float]]:
    """When an activation was on: its blocks (a burst series), else its span."""
    blocks = activation.get("blocks")
    return [(a, b) for a, b in blocks] if blocks else [(activation["start"], activation["end"])]


def _uses_of(report: Mapping) -> list[dict]:
    """Every use the analysis found: its activations, its small uses and
    the runs of its cold appliances (#89)."""
    runs = [
        {"start": c["start"], "end": c["end"], "power_w": item["power_w"]}
        for item in report.get("cold") or ()
        for c in item.get("cycles") or ()
    ]
    return [*report.get("activations", ()), *(report.get("small_uses") or ()), *runs]


def _covered(a: float, b: float, spans: Sequence[tuple[float, float]]) -> float:
    """The seconds of ``[a, b)`` within the union of ``spans``."""
    total, at = 0.0, a
    for x, y in sorted(spans):
        x, y = max(x, at), min(y, b)
        if y > x:
            total += y - x
            at = y
    return total


def _similar(power: float, ref: float) -> bool:
    return abs(power - ref) <= FREE_POWER_TOL * ref


def _same(u: Mapping, v: Mapping) -> bool:
    """Two analyses found the same use: similar power, each covering half of
    the other."""
    shared = min(u["end"], v["end"]) - max(u["start"], v["start"])
    return (
        _similar(u["power_w"], v["power_w"])
        and shared >= FREE_COVER * (u["end"] - u["start"])
        and shared >= FREE_COVER * (v["end"] - v["start"])
    )


def free_score(
    runs: Sequence,
    power_w: float,
    hidden: Mapping,
    visible: Mapping,
    solar=lambda ts: False,
) -> dict:
    """One device on one day, without the registry.

    ``runs``: its true uses; ``power_w``: its usual power; ``hidden``: the
    report of the day with the device left in the curve; ``visible``: the
    real report (the device subtracted). A use is **found** when the
    hidden analysis' uses of similar power (``FREE_POWER_TOL`` of the use's
    own) cover ``FREE_COVER`` of it. The **detections** are the uses of the
    hidden analysis of the device's power that the real analysis does not
    have (those are other appliances); one is **true** when the device's
    runs cover ``FREE_COVER`` of it. ``solar(ts)``: the panels produce at
    ``ts``; the counts are also given apart for the uses (and detections)
    that start with and without solar production.
    """
    uses = _uses_of(hidden)
    others = _uses_of(visible)
    out = {k: {"uses": 0, "found": 0, "detections": 0, "true": 0} for k in ("all", "no_solar", "solar")}

    def count(ts: float, what: str) -> None:
        out["all"][what] += 1
        out["solar" if solar(ts) else "no_solar"][what] += 1

    for r in runs:
        count(r.start, "uses")
        spans = [x for u in uses if _similar(u["power_w"], r.power_w) for x in _spans(u)]
        if _covered(r.start, r.end, spans) >= FREE_COVER * (r.end - r.start):
            count(r.start, "found")
    truth = [(r.start, r.end) for r in runs]
    for u in uses:
        if not _similar(u["power_w"], power_w) or any(_same(u, v) for v in others):
            continue
        count(u["start"], "detections")
        on = _spans(u)
        if sum(_covered(a, b, truth) for a, b in on) >= FREE_COVER * sum(b - a for a, b in on):
            count(u["start"], "true")
    return out


def _free_merge(parts: Sequence[Mapping]) -> dict:
    """The registry-free score over the days: recall and precision overall
    (top level) and under ``no_solar`` / ``solar``."""

    def total(key: str) -> dict:
        t = {w: sum(p[key][w] for p in parts) for w in ("uses", "found", "detections", "true")}
        t["recall"] = round(t["found"] / t["uses"], 3) if t["uses"] else None
        t["precision"] = round(t["true"] / t["detections"], 3) if t["detections"] else None
        return t

    return {**total("all"), "no_solar": total("no_solar"), "solar": total("solar")}


def _solar_hours(report: Mapping):
    """``solar(ts)`` from a report's curve: the production of the bucket of
    ``ts`` is above ``PV_QUIET_W``."""
    rows = report.get("curve") or []
    ts = [r["t"] for r in rows]

    def solar(t: float) -> bool:
        i = bisect.bisect_right(ts, t) - 1
        return i >= 0 and (rows[i].get("pv") or 0) > PV_QUIET_W

    return solar


# ---------------------------------------------------------------------------
# cold appliances (#89)
# ---------------------------------------------------------------------------
def _in_order(windows, config, days, hour, log=None) -> tuple[dict[str, dict], dict]:
    """``(reports, registry)``: the days analysed in order, each one with
    the cold appliances the days before it taught."""
    reports: dict[str, dict] = {}
    summaries: dict[str, dict] = {}
    registry = None
    for day, start, end in days:
        cfg = dataclasses.replace(config, cold_models=cold_models(registry))
        reports[day] = analyze(windows[day], cfg, start, end, local_hour=hour)
        summaries[day] = {k: v for k, v in reports[day].items() if k != "curve"}
        registry = update_registry(registry, summaries, end, local_hour=hour)
        if log:
            log(f"{day}: analysed in {reports[day]['quality']['compute_s']} s")
    return reports, registry or {}


def _cold_energy_kwh(item: Mapping, a: float, b: float) -> tuple[float, float]:
    """``(energy, estimated part)`` of a cold appliance within ``[a, b)``."""
    runs = sum(max(0.0, min(b, c["end"]) - max(a, c["start"])) for c in item.get("cycles") or ())
    est = sum(
        e["power_w"] * max(0.0, min(b, e["end"]) - max(a, e["start"])) for e in item.get("estimated") or ()
    )
    return (item["power_w"] * runs + est) / 3.6e6, est / 3.6e6


def cold_score(
    runs: Sequence,
    span: tuple[float, float] | None,
    hidden: Mapping,
    visible: Mapping,
    solar=lambda ts: False,
) -> dict | None:
    """The cold appliance a hidden device adds, on one day.

    Among the cold appliances of the hidden analysis that the real one does
    not have (other steps, :func:`nilm_core.cold.alike`, or runs that do
    not cover the device's), the one whose runs cover the device's the
    most; None when there is none. A run of the
    device is **found** when the appliance's runs cover ``FREE_COVER`` of
    it, a run of the appliance is **true** when the device's cover
    ``FREE_COVER`` of it (overall, and apart from / during the solar
    hours). ``span``: when the device's meter reported on that day; the
    energies are those within it."""
    truth = [(r.start, r.end) for r in runs]

    def cover(item: Mapping) -> float:
        spans = [(c["start"], c["end"]) for c in item.get("cycles") or ()]
        return sum(_covered(a, b, spans) for a, b in truth)

    # not the device's: the real analysis has it too, and its runs there
    # cover the device's as well (half as much at least)
    others = visible.get("cold") or ()
    new = [
        c
        for c in hidden.get("cold") or ()
        if not any(alike(c, o) is not None and cover(o) >= 0.5 * cover(c) for o in others)
    ]
    if not new:
        return None
    item = max(new, key=cover)
    spans = [(c["start"], c["end"]) for c in item.get("cycles") or ()]
    out = {k: {"uses": 0, "found": 0, "detections": 0, "true": 0} for k in ("all", "no_solar", "solar")}

    def count(ts: float, what: str) -> None:
        out["all"][what] += 1
        out["solar" if solar(ts) else "no_solar"][what] += 1

    for a, b in truth:
        count(a, "uses")
        if _covered(a, b, spans) >= FREE_COVER * (b - a):
            count(a, "found")
    a, b = span or (hidden["start"], hidden["end"])
    for x, y in spans:
        if not a <= x < b:
            continue  # the device's meter did not report yet (or no longer): nothing to compare with
        count(x, "detections")
        if _covered(x, y, truth) >= FREE_COVER * (y - x):
            count(x, "true")
    energy, estimated = _cold_energy_kwh(item, a, b)
    out["energy_kwh"] = energy
    out["estimated_kwh"] = estimated
    out["truth_kwh"] = sum(r.energy_kwh for r in runs)
    out["whole_day"] = b - a >= 0.95 * (hidden["end"] - hidden["start"])
    out["model"] = {k: item.get(k) for k in ("power_w", "on_w", "off_w", "on_s", "period_s", "duty")}
    return out


def _cold_merge(parts: Sequence[Mapping | None], days: int, signature: Mapping | None) -> dict:
    """The cold score over the days. ``energy_ratio``: over the days the
    device's meter covered; ``worst_day_ratio``: the daily ratio farthest
    from 1 among them."""
    found = [p for p in parts if p]
    out = _free_merge(found) if found else {}
    whole = [p for p in found if p["whole_day"] and p["truth_kwh"] > 0]
    ratios = [p["energy_kwh"] / p["truth_kwh"] for p in whole]
    truth = sum(p["truth_kwh"] for p in whole)
    energy = sum(p["energy_kwh"] for p in whole)
    return {
        **out,
        "days": days,  # with runs of the device
        "days_followed": len(found),
        "energy_days": len(whole),
        "truth_kwh": round(truth, 3),
        "energy_kwh": round(energy, 3),
        "estimated_share": round(sum(p["estimated_kwh"] for p in whole) / energy, 3) if energy else None,
        "energy_ratio": round(energy / truth, 3) if truth else None,
        "worst_day_ratio": round(max(ratios, key=lambda r: abs(r - 1)), 3) if ratios else None,
        "model": found[-1]["model"] if found else None,
        # what the registry of the hidden analyses proposes for it
        "signature": signature,
    }


def _cold_signatures(registry: Mapping) -> list[dict]:
    """The cold signatures of a registry, as the result gives them."""
    keys = ("id", "power_w", "on_w", "off_w", "on_s", "period_s", "duty", "count", "days_seen")
    keys = (*keys, "energy_kwh", "estimated_kwh")
    return [
        {k: s.get(k) for k in keys}
        for s in registry.get("signatures") or []
        if (s.get("kind"), s.get("band")) == COLD
    ]


def _meter_span(points: Sequence, start: float, end: float) -> tuple[float, float] | None:
    """From the first to the last reading of a device's meter within the
    day (the whole day when it reported before and after it)."""
    known = [t for t, v in points if v is not None]
    i, j = bisect.bisect_left(known, start), bisect.bisect_right(known, end)
    if i >= j:
        return None
    return (start if i > 0 else known[i]), (end if j < len(known) else known[j - 1])


# ---------------------------------------------------------------------------
# the benchmark
# ---------------------------------------------------------------------------
def _energy(reports: Mapping[str, Mapping]) -> dict:
    """The house energy of the days by category, and each one's share. The
    cold appliances (#89) are apart from the other signatures."""
    total = defaultdict(float)
    for r in reports.values():
        e = r["energy_kwh"]
        cold = {c["id"] for c in r.get("cold") or ()}
        total["house"] += e["house"]
        total["submetered"] += e["submetered"]
        total["switched"] += sum(e["known"].values())
        total["signatures"] += sum(v for k, v in e["signatures"].items() if k not in cold)
        if "cold" in r:
            total["cold"] += sum(v for k, v in e["signatures"].items() if k in cold)
        total["base"] += e["base"]
        total["unexplained"] += e["other"]
    house = total["house"]
    out = {k: round(v, 2) for k, v in total.items()}
    out["shares"] = {k: round(v / house, 3) for k, v in total.items() if k != "house"} if house > 0 else {}
    return out


def run(
    hist: Histories,
    config: AnalysisConfig,
    tz: dt.tzinfo | None = None,
    devices: Sequence[str] | None = None,
    log=None,
) -> dict:
    """Analyse every whole day of ``hist``, then hide each measured device
    of ``config`` (``devices``: only those) on each day and score it."""
    log = log or (lambda *_: None)

    def hour(ts: float) -> int:
        return dt.datetime.fromtimestamp(ts, tz).hour

    days = days_of([t for t, _ in hist.get(config.grid_entity, ())], tz)
    names = dict(selftest.candidates(config.submeters))
    chosen = [e for e in names if devices is None or e in devices]
    cold = config.cold_config() is not None

    # 1. the real analysis of each day (every sub-meter subtracted)
    windows: dict[str, Histories] = {}
    reports: dict[str, dict] = {}
    for day, start, end in days:
        windows[day] = window(hist, start - config.context_s - MARGIN_S, end + MARGIN_S)
    if cold:  # in order: each day with the cold appliances learned before it
        reports, _ = _in_order(windows, config, days, hour, log)
    else:
        for day, start, end in days:
            reports[day] = analyze(windows[day], config, start, end, local_hour=hour)
            log(f"{day}: analysed in {reports[day]['quality']['compute_s']} s")
    quality = {day: r["quality"]["meters"] for day, r in reports.items()}
    summaries = {d: {k: v for k, v in r.items() if k != "curve"} for d, r in reports.items()}
    now = days[-1][2] if days else 0.0
    registry = update_registry(None, summaries, now, local_hour=hour)

    # 2. the truth: each device's runs, day by day
    truth: dict[str, dict[str, list]] = {}
    points: dict[str, list] = {}
    for entity in chosen:
        scale = config.scales.get(entity, 1.0)
        points[entity] = [
            (t, None if v is None else v * scale) for t, v in parse_numeric(hist.get(entity, ()))
        ]
        truth[entity] = {day: runs_of(points[entity], start, end)[0] for day, start, end in days}

    # 3. hide and find, each device on each day; its signature is learned
    # from its runs of all the days (the best a perfect teacher would give)
    out_devices = []
    for entity in chosen:
        hidden = selftest.hidden_config(config, entity)
        tests = []
        free = []
        chilled = []
        every = [r for runs in truth[entity].values() for r in runs]
        usual = statistics.median(r.power_w for r in every) if every else None
        # with the cold appliances, the hidden analyses are made in order too
        hidden_reports, hidden_registry = _in_order(windows, hidden, days, hour) if cold else ({}, {})
        for day, start, end in days:
            others = [r for d, runs in truth[entity].items() if d != day for r in runs]
            # the hidden analysis of the day, made once
            found: dict = {"report": hidden_reports[day]} if cold else {}

            def analysis(w=windows[day], c=hidden, a=start, b=end, found=found) -> dict:
                if "report" not in found:
                    found["report"] = analyze(w, c, a, b, local_hour=hour)
                return found["report"]

            if truth[entity][day]:
                solar = _solar_hours(reports[day])
                free.append(free_score(truth[entity][day], usual, analysis(), reports[day], solar))
                if cold:
                    span = _meter_span(points[entity], start, end)
                    chilled.append(cold_score(truth[entity][day], span, analysis(), reports[day], solar))
            tests.append(
                selftest.hide_and_find(
                    day,
                    entity,
                    names[entity],
                    points[entity],
                    start,
                    end,
                    analysis,
                    registry,
                    others,
                    now,
                    step_threshold_w=config.smallest_step_w(),
                )
            )
        out_devices.append({**_device(entity, names[entity], tests), "free": _free_merge(free)})
        d = out_devices[-1]
        if cold and chilled:
            last = next((c for c in reversed(chilled) if c), None)
            sig = next(
                (
                    s
                    for s in _cold_signatures(hidden_registry)
                    if last and alike(s, last["model"]) is not None
                ),
                None,
            )
            if sig is not None:
                # the real analysis has it too: the device was not metered on some of the days
                sig["in_real_analysis"] = any(alike(sig, o) is not None for o in _cold_signatures(registry))
            d["cold"] = _cold_merge(chilled, len(chilled), sig)
        log(f"{names[entity]}: {d['status']}, {d['found']} of {d['uses']} use(s) found")

    return {
        "version": VERSION,
        "days": [d for d, _, _ in days],
        "thresholds": {
            "step_w": config.step_threshold_w,
            "night_step_w": config.low_step_threshold(),
            # only with the option: the result is unchanged without it
            **({"adaptive": True} if config.adaptive_threshold else {}),
            **({"cold": True} if cold else {}),
        },
        "meters": {**_merge_quality(list(quality.values())), "per_day": quality},
        "energy": _energy(reports),
        # the cold signatures (#89) are counted apart
        "signatures": sum(
            1 for s in registry.get("signatures") or [] if (s.get("kind"), s.get("band")) != COLD
        ),
        **({"cold": _cold_signatures(registry)} if cold else {}),
        "steps_per_day": round(statistics.mean(r["quality"]["steps"] for r in reports.values()), 1)
        if reports
        else None,
        "small_uses_per_day": round(
            statistics.mean(len(r.get("small_uses") or ()) for r in reports.values()), 1
        )
        if reports
        else None,
        "devices": out_devices,
    }


def _device(entity: str, name: str, tests: Sequence[Mapping]) -> dict:
    """One device over the days: the merged evaluation of its tested days;
    a device the curve cannot show keeps its uses and energy, none found."""
    learned = next((t["learned"] for t in reversed(tests) if t.get("learned")), None)
    tested = [t for t in tests if t["status"] == selftest.TESTED]
    if tested:
        status = selftest.TESTED
        merged = merge_evaluations(t["evaluation"] for t in tested)
    elif learned is None:
        status = selftest.NO_USE
        merged = merge_evaluations([])
    else:
        status = BELOW_THRESHOLD
        runs = [r for t in tests for r in t.get("runs") or []]
        merged = merge_evaluations(
            [{"uses": len(runs), "missed": len(runs), "truth_kwh": sum(r["energy_kwh"] for r in runs)}]
        )
    return {
        "entity_id": entity,
        "name": name,
        "status": status,
        "kind": (learned or {}).get("kind"),
        "band": (learned or {}).get("band"),
        "power_w": (learned or {}).get("power_w"),
        "days_tested": len(tested),
        **merged,
        "per_day": [
            {
                "day": t["day"],
                "status": t["status"],
                "runs": len(t.get("runs") or []),
                **(t.get("evaluation") or {}),
            }
            for t in tests
        ],
    }


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _pct(value) -> str:
    return "   –" if value is None else f"{100 * value:3.0f}%"


def _num(value, unit: str = "") -> str:
    return "–" if value is None else f"{value:g}{unit}"


def _every(meter: Mapping) -> str:
    return f"every {_num(meter['median_s'], ' s')} (90 %: {_num(meter['p90_s'], ' s')})"


def table(result: Mapping) -> str:
    """The result as text."""
    m, e = result["meters"], result["energy"]
    th = result["thresholds"]
    night = f", {th['night_step_w']:g} W without solar" if th.get("night_step_w") else ""
    lines = [
        f"{len(result['days'])} day(s): {result['days'][0]} .. {result['days'][-1]}"
        if result["days"]
        else "no whole day in the export",
        f"thresholds: {th['step_w']:g} W{night}"
        + (", adaptive to the noise (smaller uses kept apart)" if th.get("adaptive") else "")
        + (", cold appliances followed" if th.get("cold") else ""),
        f"grid meter: {_every(m['grid'])} | solar meter: {_every(m['solar'])}",
        f"noise of the curve: {_num(m['noise_w']['no_solar'], ' W')} without solar,"
        f" {_num(m['noise_w']['solar'], ' W')} with solar",
        f"house {e.get('house', 0):.1f} kWh: "
        + ", ".join(f"{k} {_pct(v).strip()}" for k, v in (e.get("shares") or {}).items()),
        f"signatures: {result['signatures']} | steps per day: {_num(result['steps_per_day'])}"
        f" | small uses per day: {_num(result.get('small_uses_per_day'))}",
        "",
        f"{'device':28} {'status':16} {'power':>7} {'uses':>5} {'found':>5} {'recall':>6} "
        f"{'detect.':>7} {'precis.':>7} {'energy':>6}",
    ]
    for d in result["devices"]:
        lines.append(
            f"{d['name'][:28]:28} {d['status']:16} {_num(d['power_w'], ' W'):>7} {d['uses']:5d} "
            f"{d['found']:5d} {_pct(d['recall']):>6} {d['detections']:7d} {_pct(d['precision']):>7} "
            f"{_pct(d['energy_ratio']):>6}"
        )
    lines += [
        "",
        "without the registry (a use is found when activations of similar power cover half of it):",
        f"{'device':28} {'uses':>5} {'found':>5} {'recall':>6} {'detect.':>7} {'precis.':>7} "
        f"{'no solar: rec. / prec.':>24} {'solar: rec. / prec.':>24}",
    ]
    for d in result["devices"]:
        f = d.get("free")
        if not f or not f["uses"]:
            continue
        lines.append(
            f"{d['name'][:28]:28} {f['uses']:5d} {f['found']:5d} {_pct(f['recall']):>6} "
            f"{f['detections']:7d} {_pct(f['precision']):>7} "
            f"{_free_part(f['no_solar']):>24} {_free_part(f['solar']):>24}"
        )
    if th.get("cold"):
        lines += ["", "cold appliances of the real analysis (regular low-power cycles):"]
        for c in result.get("cold") or []:
            run, period = round((c["on_s"] or 0) / 60, 1), round((c["period_s"] or 0) / 60, 1)
            lines.append(
                f"  {c['id']:12} {_num(c['power_w'], ' W'):>6},"
                f" {_num(run, ' min')} every {_num(period, ' min')},"
                f" {c['count']} run(s) on {c['days_seen']} day(s),"
                f" {c['energy_kwh']:.2f} kWh ({c['estimated_kwh']:.2f} estimated)"
            )
        if not result.get("cold"):
            lines.append("  none")
        lines += [
            "",
            "the cold appliance a hidden device adds"
            " (its runs against the device's, its energy against its meter):",
            f"{'device':28} {'followed':>9} {'uses':>5} {'found':>5} {'recall':>6} {'detect.':>7} "
            f"{'precis.':>7} {'no solar: rec. / prec.':>24} {'solar: rec. / prec.':>24} "
            + f"{'energy':>7} {'worst day':>9} {'estim.':>6}",
        ]
        for d in result["devices"]:
            c = d.get("cold")
            if not c:
                continue
            if not c.get("uses"):
                lines.append(
                    f"{d['name'][:28]:28} {'0/' + str(c['days']):>9}  no cold appliance found for it"
                )
                continue
            lines.append(
                f"{d['name'][:28]:28} {str(c['days_followed']) + '/' + str(c['days']):>9} {c['uses']:5d} "
                f"{c['found']:5d} {_pct(c['recall']):>6} {c['detections']:7d} {_pct(c['precision']):>7} "
                f"{_free_part(c['no_solar']):>24} {_free_part(c['solar']):>24} {_pct(c['energy_ratio']):>7} "
                f"{_pct(c['worst_day_ratio']):>9} {_pct(c['estimated_share']):>6}"
            )
    return "\n".join(lines)


def _free_part(part: Mapping) -> str:
    return (
        f"{part['found']}/{part['uses']} {_pct(part['recall']).strip()}"
        f" / {part['true']}/{part['detections']} {_pct(part['precision']).strip()}"
    )


def compare(before: Mapping, after: Mapping) -> str:
    """What changed between two results (same export, another engine or
    other thresholds)."""

    def delta(a, b, pct: bool = True) -> str:
        if a is None and b is None:
            return "–"
        fmt = (lambda v: _pct(v).strip()) if pct else _num
        return f"{fmt(a)} -> {fmt(b)}"

    old = {d["entity_id"]: d for d in before.get("devices") or []}
    lines = [
        f"signatures: {delta(before.get('signatures'), after.get('signatures'), False)}"
        f" | steps per day: {delta(before.get('steps_per_day'), after.get('steps_per_day'), False)}"
        f" | small uses per day: "
        f"{delta(before.get('small_uses_per_day'), after.get('small_uses_per_day'), False)}",
        "shares: "
        + ", ".join(
            f"{k} {delta((before.get('energy', {}).get('shares') or {}).get(k), v)}"
            for k, v in (after.get("energy", {}).get("shares") or {}).items()
        ),
        "",
        f"{'device':28} {'recall':>14} {'precision':>14} {'energy':>14} "
        f"{'free recall':>14} {'free precis.':>14}",
    ]
    for d in after.get("devices") or []:
        o = old.get(d["entity_id"], {})
        of, f = o.get("free") or {}, d.get("free") or {}
        lines.append(
            f"{d['name'][:28]:28} {delta(o.get('recall'), d['recall']):>14} "
            f"{delta(o.get('precision'), d['precision']):>14} "
            f"{delta(o.get('energy_ratio'), d['energy_ratio']):>14} "
            f"{delta(of.get('recall'), f.get('recall')):>14} "
            f"{delta(of.get('precision'), f.get('precision')):>14}"
        )
    if any(d.get("cold") for d in after.get("devices") or []):
        lines += ["", f"{'cold appliance of':28} {'recall':>14} {'precision':>14} {'energy':>14}"]
        for d in after.get("devices") or []:
            c = d.get("cold")
            if not c:
                continue
            oc = old.get(d["entity_id"], {}).get("cold") or {}
            lines.append(
                f"{d['name'][:28]:28} {delta(oc.get('recall'), c.get('recall')):>14} "
                f"{delta(oc.get('precision'), c.get('precision')):>14} "
                f"{delta(oc.get('energy_ratio'), c.get('energy_ratio')):>14}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def _pair(text: str, default: str) -> tuple[str, str]:
    key, _, value = text.partition("=")
    return key, value or default


def build_config(args: argparse.Namespace) -> AnalysisConfig:
    submeters = [SubmeterConfig(*_pair(s, s.partition("=")[0])) for s in args.submeter]
    submeters += [SubmeterConfig(*_pair(s, s.partition("=")[0]), estimated=True) for s in args.estimated]
    return AnalysisConfig(
        grid_entity=args.grid,
        grid_export_entity=args.grid_export,
        pv_entity=args.pv,
        submeters=submeters,
        switched_loads=[
            SwitchedLoadConfig(entity, entity, (state,))
            for entity, state in (_pair(s, "on") for s in args.switch)
        ],
        scales={entity: float(factor) for entity, factor in (_pair(s, "1") for s in args.scale)},
        step_threshold_w=args.threshold,
        night_step_threshold_w=args.night_threshold,
        adaptive_threshold=getattr(args, "adaptive", False),
        cold_appliances=getattr(args, "cold", False),
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("history", help="CSV export, or a folder of .json / .json.gz files")
    ap.add_argument("--grid", required=True, help="grid power sensor (signed, or the import)")
    ap.add_argument("--grid-export", help="export power sensor, when the grid has two")
    ap.add_argument("--pv", help="solar power sensor")
    ap.add_argument(
        "--submeter", action="append", default=[], metavar="ENTITY[=NAME]", help="measured device"
    )
    ap.add_argument(
        "--estimated",
        action="append",
        default=[],
        metavar="ENTITY[=NAME]",
        help="estimated device: subtracted, never hidden",
    )
    ap.add_argument("--switch", action="append", default=[], metavar="ENTITY[=STATE]", help="switched load")
    ap.add_argument(
        "--scale", action="append", default=[], metavar="ENTITY=FACTOR", help="1000 for a kW sensor"
    )
    ap.add_argument("--threshold", type=float, default=300.0, help="step threshold, W")
    ap.add_argument("--night-threshold", type=float, help="step threshold without solar production, W")
    ap.add_argument(
        "--adaptive",
        action="store_true",
        help="noise-adaptive threshold: smaller uses kept apart (#88)",
    )
    ap.add_argument(
        "--cold",
        action="store_true",
        help="cold appliances: the regular low-power cycles followed (#89; needs --adaptive)",
    )
    ap.add_argument("--device", action="append", metavar="ENTITY", help="hide only these measured devices")
    ap.add_argument("--tz", help="time zone of the days (default: this machine's)")
    ap.add_argument("--json", help="write the result to this file")
    ap.add_argument("--compare", help="a result written earlier with --json")
    args = ap.parse_args(argv)
    if args.cold and not args.adaptive:
        ap.error("--cold needs --adaptive")

    hist = load(args.history)
    config = build_config(args)
    if not hist.get(config.grid_entity):
        print(f"no reading of {config.grid_entity} in {args.history}", file=sys.stderr)
        return 1
    tz = zoneinfo.ZoneInfo(args.tz) if args.tz else None
    result = run(hist, config, tz, args.device, log=lambda m: print(m, file=sys.stderr))
    print(table(result))
    if args.compare:
        print()
        print(compare(json.loads(pathlib.Path(args.compare).read_text()), result))
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
