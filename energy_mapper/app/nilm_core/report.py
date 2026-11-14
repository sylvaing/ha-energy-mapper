"""Analyse one period (usually one day) and build a JSON-friendly report."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise

from .activations import find_cycling_sessions, pair_steps, validate_activations
from .known import KnownLoad, SwitchedLoadConfig, resolve_switched_load
from .signal import build_samples, parse_numeric, scaled, sum_series
from .signatures import cluster
from .steps import detect_steps

REPORT_VERSION = 1


@dataclass
class SubmeterConfig:
    entity_id: str
    name: str


@dataclass
class AnalysisConfig:
    grid_entity: str  # signed (+ import), or import only when grid_export_entity is set
    grid_export_entity: str | None = None
    grid_invert: bool = False  # the meter reports + for export
    pv_entity: str | None = None
    # more meters of the same kind, summed (one per phase, several inverters...)
    extra_grid_entities: list[str] = field(default_factory=list)
    extra_pv_entities: list[str] = field(default_factory=list)
    battery_entity: str | None = None  # + discharge
    battery_invert: bool = False
    submeters: list[SubmeterConfig] = field(default_factory=list)
    switched_loads: list[SwitchedLoadConfig] = field(default_factory=list)
    # unit factors to W, per entity (1000 for kW sensors); missing = 1
    scales: dict[str, float] = field(default_factory=dict)
    step_threshold_w: float = 300.0
    # None: relative to the period's maximum (peak_relative x max)
    peak_threshold_w: float | None = None
    peak_relative: float = 0.8
    top_peaks: int = 5
    curve_resolution_s: int = 60
    context_s: int = 3 * 3600  # history read before the period (loads already on)

    def entities(self) -> list[str]:
        ids = [self.grid_entity, *self.extra_grid_entities, *self.extra_pv_entities]
        for e in (self.grid_export_entity, self.pv_entity, self.battery_entity):
            if e:
                ids.append(e)
        ids += [s.entity_id for s in self.submeters]
        ids += [s.entity_id for s in self.switched_loads]
        return ids


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _minute_means(points: list[tuple[float, float]]) -> list[float]:
    buckets: dict[int, list[float]] = {}
    for t, v in points:
        buckets.setdefault(int(t // 60), []).append(v)
    return [_mean(v) for v in buckets.values()]


def base_load(values: list[float], percentile: float = 0.05) -> float:
    """Always-on consumption: a low percentile of the load (fridges, network,
    standby). Zero and negative values (sensor glitches) are ignored."""
    v = sorted(x for x in values if x > 0)
    if not v:
        return 0.0
    return v[int(percentile * (len(v) - 1))]


def analyze(
    histories: dict[str, list[tuple[float, str]]],
    config: AnalysisConfig,
    start: float,
    end: float,
    local_hour: Callable[[float], int] | None = None,
) -> dict:
    """``histories`` maps entity ids to raw ``(timestamp, state)`` lists that
    cover at least ``[start - config.context_s, end]``."""
    local_hour = local_hour or (lambda ts: time.localtime(ts).tm_hour)
    t0 = time.perf_counter()

    def series(entity, invert=False):
        if not entity:
            return None
        return scaled(parse_numeric(histories.get(entity, [])), config.scales.get(entity, 1.0), invert)

    samples = build_samples(
        grid=sum_series(
            [series(config.grid_entity, config.grid_invert)]
            + [series(e, config.grid_invert) for e in config.extra_grid_entities]
        ),
        pv=sum_series([series(config.pv_entity)] + [series(e) for e in config.extra_pv_entities])
        if config.pv_entity
        else None,
        submeters=[series(s.entity_id) for s in config.submeters],
        grid_export=series(config.grid_export_entity),
        battery=series(config.battery_entity, config.battery_invert),
    )
    points = [(s.ts, s.residual) for s in samples if s.residual is not None]

    steps = detect_steps(points, threshold=config.step_threshold_w)
    known: list[KnownLoad] = [
        resolve_switched_load(c, histories.get(c.entity_id, []), steps, end, config.step_threshold_w)
        for c in config.switched_loads
    ]
    sessions = find_cycling_sessions(steps)
    pairs = pair_steps(steps)
    in_period = [p for p in points if start <= p[0] < end]
    base_est = base_load(_minute_means(in_period))
    pairs = validate_activations(pairs, points, base_est)
    acts = [a for a in sessions + pairs if a.end > start and a.start < end]
    signatures = cluster(acts, local_hour=local_hour)

    curve, totals = _curve(samples, config, known, acts, start, end)
    peaks = _peaks(samples, config, known, acts, start, end)

    in_period = [s for s in samples if start <= s.ts < end]
    pv_unknown_s = 0.0
    for a, b in pairwise(in_period):
        if a.pv is None:
            pv_unknown_s += min(b.ts - a.ts, 300)
    gaps = [b.ts - a.ts for a, b in pairwise(in_period)]

    return {
        "version": REPORT_VERSION,
        "start": start,
        "end": end,
        "generated_at": time.time(),
        "energy_kwh": totals,
        "curve": curve,
        "peaks": peaks,
        "signatures": [s.to_dict() for s in signatures],
        "known_loads": [
            {
                "name": k.name,
                "entity_id": k.entity_id,
                "power_w": round(k.power_w),
                "transitions": k.transitions,
                "matched": k.matched,
                "on_states": list(k.on_states),
                "inverted": k.inverted,
                "on_s": round(sum(max(0, min(b, end) - max(a, start)) for a, b in k.intervals)),
            }
            for k in known
        ],
        "activations": [
            {
                "start": a.start,
                "end": a.end,
                "power_w": round(a.power_w),
                "cycling": a.cycling,
                "duty": a.duty,
                "signature": a.signature,
            }
            for a in acts
        ],
        "quality": {
            "samples": len(in_period),
            "steps": sum(1 for s in steps if start <= s.ts < end),
            "pv_unknown_s": round(pv_unknown_s),
            "max_gap_s": round(max(gaps)) if gaps else None,
            "compute_s": round(time.perf_counter() - t0, 2),
        },
    }


def _curve(samples, config, known, acts, start, end):
    """Per-bucket stacked breakdown of the house load.

    Measured parts (sub-meters) are kept as measured; estimated parts (known
    loads, signatures) are scaled down if they explain more than what is left;
    the base load is a low percentile of the unmetered load.
    """
    res = config.curve_resolution_s
    buckets: dict[int, list] = {}
    for s in samples:
        if start <= s.ts < end:
            buckets.setdefault(int((s.ts - start) // res), []).append(s)

    rows = []
    for k in sorted(buckets):
        b0 = start + k * res
        b1 = b0 + res
        group = buckets[k]
        houses = [s.house for s in group if s.house is not None]
        if not houses:
            continue
        house = _mean(houses)
        row = {
            "t": b0,
            "house": round(house),
            "grid": round(_mean([s.grid for s in group])),
            "pv": round(_mean([s.pv for s in group if s.pv is not None])),
            "submetered": round(_mean([s.submetered for s in group])),
            "known": {},
            "signatures": {},
        }
        for kl in known:
            p = kl.power_w * kl.overlap(b0, b1) / res
            if p > 0:
                row["known"][kl.name] = p
        for a in acts:
            e = a.overlap_energy_wh(b0, b1)
            if e > 0:
                key = a.signature or "?"
                row["signatures"][key] = row["signatures"].get(key, 0.0) + e * 3600 / res
        row["_room"] = max(house - row["submetered"], 0.0)
        rows.append(row)

    base = base_load([r["_room"] for r in rows])
    totals = {
        "house": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "pv": 0.0,
        "submetered": 0.0,
        "base": 0.0,
        "other": 0.0,
        "known": {},
        "signatures": {},
    }
    h = res / 3600 / 1000
    for row in rows:
        room = row.pop("_room")
        row["base"] = min(base, room)
        avail = room - row["base"]
        # never explain more than what is left; switch-backed loads first
        k_sum = sum(row["known"].values())
        if k_sum > avail:
            f = avail / k_sum if k_sum else 0.0
            row["known"] = {n: v * f for n, v in row["known"].items()}
            k_sum = avail
        s_sum = sum(row["signatures"].values())
        left = avail - k_sum
        if s_sum > left:
            f = left / s_sum if s_sum else 0.0
            row["signatures"] = {n: v * f for n, v in row["signatures"].items()}
            s_sum = left
        row["other"] = round(max(avail - k_sum - s_sum, 0.0))
        row["base"] = round(row["base"])
        row["known"] = {n: round(v) for n, v in row["known"].items() if v >= 0.5}
        row["signatures"] = {n: round(v) for n, v in row["signatures"].items() if v >= 0.5}
        totals["house"] += row["house"] * h
        totals["grid_import"] += max(row["grid"], 0) * h
        totals["grid_export"] += max(-row["grid"], 0) * h
        totals["pv"] += row["pv"] * h
        totals["submetered"] += row["submetered"] * h
        totals["base"] += row["base"] * h
        totals["other"] += row["other"] * h
        for n, v in row["known"].items():
            totals["known"][n] = totals["known"].get(n, 0.0) + v * h
        for n, v in row["signatures"].items():
            totals["signatures"][n] = totals["signatures"].get(n, 0.0) + v * h

    def rnd(x):
        return {k: rnd(v) for k, v in x.items()} if isinstance(x, dict) else round(x, 3)

    totals = rnd(totals)
    totals["base_w"] = round(base)
    return rows, totals


def _peaks(samples, config, known, acts, start, end):
    """Episodes above the threshold, with what was running at the maximum."""
    period = [s for s in samples if start <= s.ts < end and s.house is not None]
    if not period:
        return []
    threshold = config.peak_threshold_w
    if threshold is None:
        threshold = config.peak_relative * max(s.house for s in period)
    episodes = []
    cur = None
    for s in period:
        if s.house >= threshold:
            if cur and s.ts - cur["end"] <= 120:
                cur["end"] = s.ts
                if s.house > cur["max"].house:
                    cur["max"] = s
            else:
                cur = {"start": s.ts, "end": s.ts, "max": s}
                episodes.append(cur)
    episodes.sort(key=lambda e: -e["max"].house)

    out = []
    for e in episodes[: config.top_peaks]:
        m = e["max"]
        parts = []
        if m.submetered > 0:
            parts.append({"kind": "submetered", "name": "submetered", "power_w": round(m.submetered)})
        for kl in known:
            if kl.active_at(m.ts):
                parts.append({"kind": "known", "name": kl.name, "power_w": round(kl.power_w)})
        for a in acts:
            p = a.power_at(m.ts)
            if p > 0:
                parts.append(
                    {
                        "kind": "signature",
                        "name": a.signature or "?",
                        "power_w": round(p),
                        "since": a.start,
                    }
                )
        explained = sum(p["power_w"] for p in parts)
        out.append(
            {
                "start": e["start"],
                "end": e["end"],
                "max_ts": m.ts,
                "house_w": round(m.house),
                "grid_w": round(m.grid),
                "pv_w": round(m.pv or 0),
                "parts": parts,
                "unexplained_w": round(m.house - explained),
            }
        )
    return out
