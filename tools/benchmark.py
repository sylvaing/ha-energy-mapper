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

Standard library only, not shipped in the add-on image.
"""

from __future__ import annotations

import argparse
import bisect
import csv
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
from nilm_core.groundtruth import merge_evaluations, runs_of  # noqa: E402
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
    """Every use the analysis found: its activations and its small uses."""
    return [*report.get("activations", ()), *(report.get("small_uses") or ())]


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
# the benchmark
# ---------------------------------------------------------------------------
def _energy(reports: Mapping[str, Mapping]) -> dict:
    """The house energy of the days by category, and each one's share."""
    total = defaultdict(float)
    for r in reports.values():
        e = r["energy_kwh"]
        total["house"] += e["house"]
        total["submetered"] += e["submetered"]
        total["switched"] += sum(e["known"].values())
        total["signatures"] += sum(e["signatures"].values())
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

    # 1. the real analysis of each day (every sub-meter subtracted)
    windows: dict[str, Histories] = {}
    reports: dict[str, dict] = {}
    quality: dict[str, dict] = {}
    for day, start, end in days:
        w = windows[day] = window(hist, start - config.context_s - MARGIN_S, end + MARGIN_S)
        reports[day] = analyze(w, config, start, end, local_hour=hour)
        quality[day] = reports[day]["quality"]["meters"]
        log(f"{day}: analysed in {reports[day]['quality']['compute_s']} s")
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
        every = [r for runs in truth[entity].values() for r in runs]
        usual = statistics.median(r.power_w for r in every) if every else None
        for day, start, end in days:
            others = [r for d, runs in truth[entity].items() if d != day for r in runs]
            found: dict = {}  # the hidden analysis of the day, made once

            def analysis(w=windows[day], c=hidden, a=start, b=end, found=found) -> dict:
                if "report" not in found:
                    found["report"] = analyze(w, c, a, b, local_hour=hour)
                return found["report"]

            if truth[entity][day]:
                free.append(
                    free_score(
                        truth[entity][day], usual, analysis(), reports[day], _solar_hours(reports[day])
                    )
                )
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
        log(f"{names[entity]}: {d['status']}, {d['found']} of {d['uses']} use(s) found")

    return {
        "version": VERSION,
        "days": [d for d, _, _ in days],
        "thresholds": {
            "step_w": config.step_threshold_w,
            "night_step_w": config.low_step_threshold(),
            # only with the option: the result is unchanged without it
            **({"adaptive": True} if config.adaptive_threshold else {}),
        },
        "meters": {**_merge_quality(list(quality.values())), "per_day": quality},
        "energy": _energy(reports),
        "signatures": len(registry.get("signatures") or []),
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
        + (", adaptive to the noise (smaller uses kept apart)" if th.get("adaptive") else ""),
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
    ap.add_argument("--device", action="append", metavar="ENTITY", help="hide only these measured devices")
    ap.add_argument("--tz", help="time zone of the days (default: this machine's)")
    ap.add_argument("--json", help="write the result to this file")
    ap.add_argument("--compare", help="a result written earlier with --json")
    args = ap.parse_args(argv)

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
