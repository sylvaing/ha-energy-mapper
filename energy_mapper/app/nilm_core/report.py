"""Analyse one period (usually one day) and build a JSON-friendly report."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as _copy
from itertools import pairwise

from .activations import (
    Activation,
    find_cycling_sessions,
    group_bursts,
    pair_steps,
    validate_activations,
)
from .cycles import CycleConfig, Phase, attribute, find_cycles
from .known import RUN, KnownLoad, SwitchedLoadConfig, resolve_switched_load
from .quality import meter_quality
from .signal import build_samples, parse_numeric, scaled, sum_series
from .signatures import cluster
from .steps import (
    MERGE_S,
    NOISE,
    PV_QUIET_W,
    WINDOW_S,
    NoiseConfig,
    PvQuiet,
    Step,
    adapted_windows,
    detect_steps,
    hold_doubtful,
    meter_interval,
    night_threshold,
    solar_doubt_limit,
)
from .submeters import SubmeterEchoes

REPORT_VERSION = 1


@dataclass
class SubmeterConfig:
    entity_id: str
    name: str
    # an estimate (e.g. an estimation integration's power sensor), not a
    # measurement: subtracted the same way, shown apart
    estimated: bool = False


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
    # the night threshold (#77): smaller steps, down to this, are kept where
    # the PV production is known and stays near zero around them (no effect
    # without a PV meter; None: off)
    night_step_threshold_w: float | None = None
    # the noise-adaptive threshold (#88, off by default): changes under the
    # thresholds above that stand out of the measured noise of the curve are
    # kept as low steps and paired into small uses, reported apart
    # (``small_uses``); everything else is found exactly as without it
    adaptive_threshold: bool = False
    noise: NoiseConfig = NOISE
    # peaks: the top episodes above peak_relative x the period's maximum;
    # a peak is "notable" above peak_threshold_w, or when None above
    # peak_relative x the highest of the period's maximum and
    # peak_reference_w, the maximum of the previous days (see registry)
    peak_threshold_w: float | None = None
    peak_relative: float = 0.8
    peak_reference_w: float | None = None
    top_peaks: int = 5
    curve_resolution_s: int = 60
    context_s: int = 3 * 3600  # history read before the period (loads already on)
    # multi-phase appliances (#26): the templates a candidate cycle is matched
    # to, ``{registry id: [phase dicts]}`` (none: candidates are only listed)
    cycle_templates: dict[str, list[dict]] = field(default_factory=dict)
    cycles: CycleConfig = field(default_factory=CycleConfig)

    def low_step_threshold(self) -> float | None:
        """The night threshold when it applies (a PV meter, lower than the
        step threshold), else None."""
        return night_threshold(self.step_threshold_w, self.night_step_threshold_w, bool(self.pv_entity))

    def smallest_step_w(self) -> float:
        """The smallest step the analysis can keep (at least at night)."""
        low = self.low_step_threshold()
        return low if low is not None else self.step_threshold_w

    def noise_config(self) -> NoiseConfig | None:
        """The adaptive threshold's settings when it is on, else None."""
        return self.noise if self.adaptive_threshold else None

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


def find_steps(samples, config: AnalysisConfig, held: list[float] | None = None) -> list[Step]:
    """The steps of the residual load: ``step_threshold_w``, and the night
    threshold (#77) where the PV production of the samples stays near zero
    around the step. With a PV meter, the samples whose production is in
    doubt (a late solar reading, #86) are set aside first
    (:func:`~.steps.hold_doubtful`; their times are added to ``held``). The
    steps a sub-meter's misaligned reports left are marked ``explained_by``
    it (#85, :mod:`.submeters`). With the adaptive threshold (#88), the
    ``low`` steps are among them (never explained here: see
    :func:`small_uses`)."""
    return explained_steps(samples, config, held)[0]


def explained_steps(
    samples, config: AnalysisConfig, held: list[float] | None = None
) -> tuple[list[Step], SubmeterEchoes | None, float]:
    """``(steps, echoes, window_s)``: the steps of :func:`find_steps`, what
    explained those of the sub-meters (None without a sub-meter that
    measures) and the detector's window on these samples. The sub-meters
    are read against the points the detector read (the solar doubt set
    aside), each with what the sub-meters took out of its sample."""
    steps, echoes, window_s, _, _ = _detect(samples, config, held)
    return steps, echoes, window_s


def _detect(samples, config: AnalysisConfig, held: list[float] | None = None):
    """:func:`explained_steps`, with the points the detector read and the
    samples they come from (one per point)."""
    known = [s for s in samples if s.residual is not None]
    points = [(s.ts, s.residual) for s in known]
    low = None
    if config.pv_entity:
        # a known residual has a known production: one value per point
        pv = [s.pv for s in known]
        points = hold_doubtful(points, pv, solar_doubt_limit(config.step_threshold_w), held)
        low = config.low_step_threshold()
    noise = config.noise_config()
    if low is None:
        steps = detect_steps(points, threshold=config.step_threshold_w, noise=noise)
    else:
        quiet = PvQuiet([(s.ts, s.pv) for s in samples if s.pv is not None])
        steps = detect_steps(
            points, threshold=config.step_threshold_w, low_threshold=low, low_ok=quiet, noise=noise
        )
    window_s, merge_s = adapted_windows(meter_interval([t for t, _ in points]), WINDOW_S, MERGE_S)
    echoes = SubmeterEchoes.for_config(config)
    if echoes is not None:
        for (ts, value), sample in zip(points, known, strict=True):
            echoes.add(ts, value, sample.subs)
        for step in steps:
            if not step.low:  # explained as without the low steps
                echoes.feed(step, window_s, merge_s)
    return steps, echoes, window_s, points, known


SMALL_TOL_W = 20.0  # a small use's two steps agree within this, or the 25 % of pair_steps


def small_uses(
    steps: list[Step],
    points: list[tuple[float, float]],
    samples: list,
    config: AnalysisConfig,
    base_w: float,
    window_s: float,
) -> list[Activation]:
    """The small uses (#88): the pairs the low steps make, among themselves
    or with a step of the fixed thresholds that nothing else used.

    ``steps``: every step, once the uses of the fixed thresholds are made
    (their steps are ``used``); ``points`` and ``samples``: what the
    detector read (:func:`_detect`). The low steps a sub-meter's misaligned
    reports left are first marked ``explained_by`` it, as the others were
    (#85), in a pass of their own: what explains the other steps does not
    change. The pairs are those of :func:`~.activations.pair_steps`, with a
    tolerance fit for small powers, and are validated on the load as any
    use. They exist only because of the adaptive threshold: they are
    reported apart and never reach the signatures.
    """
    low = [s for s in steps if s.low]
    if not low:
        return []
    echoes = SubmeterEchoes.for_config(config)
    if echoes is not None:
        _, merge_s = adapted_windows(meter_interval([t for t, _ in points]), WINDOW_S, MERGE_S)
        for (ts, value), sample in zip(points, samples, strict=True):
            echoes.add(ts, value, sample.subs)
        for step in steps:
            # the other steps are fed as copies: their own explanation stays
            echoes.feed(step if step.low else _copy(step), window_s, merge_s)
    free = [s for s in steps if s.low or not (s.used or s.explained_by)]
    pairs = pair_steps(free, tol_w=SMALL_TOL_W)
    # two steps of the fixed thresholds were already refused as a use
    pairs = [a for a in pairs if any(s.low for s in a.steps)]
    return validate_activations(pairs, points, base_w)


def solar_interval(
    pv: list[tuple[float, float | None]],
    start: float,
    end: float,
    change_w: float,
    min_changes: int = 20,
) -> float | None:
    """How long the solar meter takes to tell that the production moved:
    the time since its previous reading, for the readings of the period
    that moved by ``change_w`` or more while producing; their lower
    quartile. Home Assistant records a state when it changes, so a stable
    production gives long intervals whatever the meter (the night is one):
    only the changes that matter are looked at, and the lower quartile
    leaves out those that follow a stable production. None under
    ``min_changes`` such readings (a clear or a dull day)."""
    valid = [(t, v) for t, v in pv if v is not None and start <= t < end]
    gaps = sorted(
        b[0] - a[0] for a, b in pairwise(valid) if a[1] > PV_QUIET_W and abs(b[1] - a[1]) >= change_w
    )
    return gaps[(len(gaps) - 1) // 4] if len(gaps) >= min_changes else None


def submeter_quality(
    config: AnalysisConfig,
    echoes: SubmeterEchoes | None,
    readings: Callable[[str], list | None],
    window_s: float,
    start: float,
    end: float,
) -> list[dict]:
    """Per sub-meter that measures, within ``[start, end)``: the steps its
    reports explained and the lag of its isolated changes
    (:meth:`SubmeterEchoes.lags`). Only those with something to say."""
    out = []
    if echoes is None:
        return out
    for i, m in enumerate(config.submeters):
        if m.estimated:
            continue
        sub = echoes.indices.index(i)
        explained = sum(1 for t, n in echoes.explained if n == sub and start <= t < end)
        lag = echoes.lags(i, readings(m.entity_id) or [], window_s, config.smallest_step_w(), start, end)
        if explained or lag.changes:
            out.append(
                {"entity_id": m.entity_id, "name": m.name, "explained_steps": explained, **lag.to_dict()}
            )
    return out


def resolve_known(
    histories: dict[str, list[tuple[float, str]]],
    config: AnalysisConfig,
    steps: list[Step],
    points: list[tuple[float, float]],
    end: float,
) -> list[KnownLoad]:
    """The switched loads of the day, in the configured order. Relays (step
    mode) first; the operation-state loads (run mode, #79) are then measured
    on the residual load without the relays' power."""
    out: dict[int, KnownLoad] = {}
    runs = []
    for i, c in enumerate(config.switched_loads):
        if c.measure == RUN:
            runs.append(i)
        else:
            out[i] = resolve_switched_load(
                c, histories.get(c.entity_id, []), steps, end, config.step_threshold_w
            )
    if runs:
        relays = [k for k in out.values() if k.intervals and k.power_w > 0]
        free = [(t, v - sum(k.power_at(t) for k in relays)) for t, v in points] if relays else points
        for i in runs:
            c = config.switched_loads[i]
            out[i] = resolve_switched_load(
                c, histories.get(c.entity_id, []), steps, end, config.step_threshold_w, points=free
            )
    return [out[i] for i in range(len(config.switched_loads))]


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

    pv = None
    if config.pv_entity:
        pv = sum_series([series(config.pv_entity)] + [series(e) for e in config.extra_pv_entities])
    samples = build_samples(
        grid=sum_series(
            [series(config.grid_entity, config.grid_invert)]
            + [series(e, config.grid_invert) for e in config.extra_grid_entities]
        ),
        pv=pv,
        submeters=[series(s.entity_id) for s in config.submeters],
        estimated=[s.estimated for s in config.submeters],
        grid_export=series(config.grid_export_entity),
        battery=series(config.battery_entity, config.battery_invert),
    )
    points = [(s.ts, s.residual) for s in samples if s.residual is not None]

    held: list[float] = []  # samples set aside: their solar production was in doubt (#86)
    every_step, echoes, window_s, read, read_from = _detect(samples, config, held)
    # the low steps of the adaptive threshold (#88) are kept apart: what
    # follows sees the steps of the fixed thresholds, as without the option
    steps = [s for s in every_step if not s.low]
    known = resolve_known(histories, config, steps, points, end)
    sessions = find_cycling_sessions(steps)
    pairs = pair_steps(steps)
    in_period = [p for p in points if start <= p[0] < end]
    base_est = base_load(_minute_means(in_period))
    pairs = validate_activations(pairs, points, base_est)
    sessions, pairs = group_bursts(sessions, pairs)  # a thermostat's bursts: one cycling use (#87)
    candidates = find_cycles(points, base_est, sessions + pairs, known, start, end, config.cycles)
    templates = {}
    for tid, phases in (config.cycle_templates or {}).items():
        try:
            templates[tid] = [Phase.from_dict(p) for p in phases]
        except (KeyError, TypeError, ValueError):
            continue
    every = attribute(candidates, sessions + pairs, {k: v for k, v in templates.items() if v}, config.cycles)
    acts = [a for a in every if a.end > start and a.start < end]
    signatures = cluster(acts, local_hour=local_hour)
    adaptive = {}
    if config.adaptive_threshold:  # only with the option: the report is unchanged without it
        small = small_uses(every_step, read, read_from, config, base_est, window_s)
        adaptive = {"small_uses": [_small_use(a) for a in small if a.end > start and a.start < end]}

    curve, totals = _curve(samples, config, known, acts, start, end)
    houses = [s.house for s in samples if start <= s.ts < end and s.house is not None]
    max_house = max(houses) if houses else None
    threshold = peak_threshold(config, max_house)
    detect = config.peak_relative * max_house if max_house is not None else None
    peaks = _peaks(samples, config, known, acts, start, end, detect)
    for p in peaks:
        p["notable"] = threshold is not None and p["house_w"] >= threshold

    in_period = [s for s in samples if start <= s.ts < end]
    pv_unknown_s = 0.0
    for a, b in pairwise(in_period):
        if a.pv is None:
            pv_unknown_s += min(b.ts - a.ts, 300)
    gaps = [b.ts - a.ts for a, b in pairwise(in_period)]
    solar = {}
    if config.pv_entity:  # only with a solar meter: the report is unchanged without one
        interval = solar_interval(pv or [], start, end, solar_doubt_limit(config.step_threshold_w))
        solar = {
            "solar_interval_s": round(interval, 1) if interval is not None else None,
            "solar_held": sum(1 for t in held if start <= t < end),
        }

    return {
        "version": REPORT_VERSION,
        "start": start,
        "end": end,
        "generated_at": time.time(),
        "energy_kwh": totals,
        "max_house_w": round(max_house) if max_house is not None else None,
        "peak_threshold_w": round(threshold) if threshold is not None else None,
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
                **({"mode": RUN, "runs": len(k.intervals)} if k.mode == RUN else {}),
            }
            for k in known
        ],
        "activations": [_activation(a) for a in acts],
        **adaptive,
        "cycles": [c.to_dict() for c in candidates],
        "quality": {
            "samples": len(in_period),
            # without the steps the sub-meters' reports left (#85)
            "steps": sum(1 for s in steps if start <= s.ts < end)
            - sum(1 for t, _ in (echoes.explained if echoes else ()) if start <= t < end),
            "submeters": submeter_quality(config, echoes, series, window_s, start, end),
            "pv_unknown_s": round(pv_unknown_s),
            "max_gap_s": round(max(gaps)) if gaps else None,
            **solar,
            **(
                {"low_steps": sum(1 for s in every_step if s.low and start <= s.ts < end)}
                if config.adaptive_threshold
                else {}
            ),
            "meters": meter_quality(samples, pv if config.pv_entity else None, start, end, config.noise),
            "compute_s": round(time.perf_counter() - t0, 2),
        },
    }


def _small_use(a: Activation) -> dict:
    """A small use (#88): one use under the fixed thresholds, with the size
    of its two steps (a compressor starts higher than it stops)."""
    on, off = a.steps[0], a.steps[-1]
    return {
        "start": a.start,
        "end": a.end,
        "power_w": round(a.power_w),
        "on_w": round(on.delta),
        "off_w": round(-off.delta),
    }


def _activation(a) -> dict:
    out = {
        "start": a.start,
        "end": a.end,
        "power_w": round(a.power_w),
        "cycling": a.cycling,
        "duty": a.duty,
        "on_s": a.on_s,
        "period_s": a.period_s,
        "signature": a.signature,
    }
    if getattr(a, "blocks", None):
        # a burst series (#87): when it was on, the bursts closer than the
        # session gap as one block
        out["blocks"] = [[round(b0), round(b1)] for b0, b1 in a.blocks]
    if getattr(a, "multiphase", False):
        # a recognised cycle (#26): power_w is its mean power
        out["multiphase"] = True
        out["phases"] = [p.to_dict() for p in a.phases]
    return out


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
            # the part of ``submetered`` that is estimated, not measured
            "estimated": round(_mean([s.estimated for s in group])),
            "known": {},
            "signatures": {},
        }
        for kl in known:
            p = kl.energy_ws(b0, b1) / res
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
        "estimated": 0.0,
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
        totals["estimated"] += row["estimated"] * h
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


def peak_threshold(config: AnalysisConfig, period_max_w: float | None) -> float | None:
    """Threshold of the notable peaks: the fixed one, else ``peak_relative``
    x the highest of the period's maximum and the reference of the previous
    days."""
    if config.peak_threshold_w is not None:
        return config.peak_threshold_w
    ref = max((x for x in (period_max_w, config.peak_reference_w) if x is not None), default=None)
    return None if ref is None else config.peak_relative * ref


def _peaks(samples, config, known, acts, start, end, threshold):
    """Episodes above ``threshold`` (the top ``config.top_peaks``), with what
    was running at the maximum."""
    period = [s for s in samples if start <= s.ts < end and s.house is not None]
    if not period or threshold is None:
        return []
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
        measured = m.submetered - m.estimated
        if measured > 0.5:
            parts.append({"kind": "submetered", "name": "submetered", "power_w": round(measured)})
        if m.estimated > 0.5:
            parts.append({"kind": "estimated", "name": "estimated", "power_w": round(m.estimated)})
        for kl in known:
            p = kl.power_at(m.ts)
            if p > 0:
                parts.append({"kind": "known", "name": kl.name, "power_w": round(p)})
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
