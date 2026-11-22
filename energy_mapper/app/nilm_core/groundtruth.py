"""Ground truth from a measured device, and the evaluation of the recognition.

A power sensor that measures one appliance (a smart plug on a dishwasher,
an oven's own meter) tells exactly when it ran. This module turns such a
series into **runs** (one use each), learns from them the **signature** the
engine should find in the whole-house curve, and **evaluates** the curve-based
detections of a signature against the runs (precision, recall, energy).

Used by the add-on to learn an appliance from a temporary plug (#14); the
nightly self-test (#15) reuses :func:`runs_of`, :func:`detections` and
:func:`evaluate` the same way (hide a measured device, find it again).

A **connected appliance** without a useful power sensor (#79) is a ground
truth too: its operation state (``run`` / ``stop``) bounds each run exactly,
and the house curve measures it (:func:`state_runs`: what the residual load
draws above its baseline around the run, tolerant to a gradual start). An
optional phase state names the phases with their power, an optional
programme state tells which programme ran, and an optional energy counter,
often coarse (0.1 kWh, updated every 15 minutes), checks and scales the
measured energy over all the runs (:func:`learn`).

1. **Runs** (:func:`runs_of`): the sensor's readings are piecewise constant
   (Home Assistant records a state when it changes; a state holds until the
   next one, ``unavailable`` / ``unknown`` mean no data). The device runs
   from ``on_w``; pauses shorter than ``max_gap_s`` stay inside one run (a
   dishwasher's pauses, an oven's thermostat). Runs shorter than
   ``min_run_s`` or never reaching ``min_use_w`` are standby noise. The
   periods the sensor reported are returned too (``covered``): a detection
   outside them cannot be checked.
2. **Kind of a run**, the engine's kinds: ``multiphase`` when it has
   heating (from ``CycleConfig.heat_w``, at least ``heat_min_s``), low-power
   activity (``low_w`` to ``heat_w``, at least ``min_low_s``: a pump, a
   motor) and the duration of a cycle; its phases come from
   :func:`.cycles.segment` on its per-minute power. ``cycling`` when its
   high level is crossed at least ``cycling_steps`` times with a duty of at
   most ``cycling_max_duty`` (a thermostat). ``steady`` otherwise.
3. **Learned signature** (:func:`learn`): the most frequent kind; its power
   (median high level), duration band, on-time, period and duty; for a
   multi-phase appliance the template of its most frequent programme
   (clustered and merged like the registry's, :mod:`.cycles`).
4. **Evaluation** (:func:`evaluate`): a detection and a run match when they
   overlap by at least ``min_overlap`` of the shorter one. Precision: the
   share of the detections (inside the covered periods) that match a run;
   recall: the share of the runs matched by a detection.

Pure Python, standard library only, no clock, no I/O.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from . import cycles as mp
from .known import UNAVAILABLE, RunMeasureConfig, measure_run
from .signatures import duration_band

STEADY, CYCLING, MULTIPHASE = "steady", "cycling", "multiphase"
_KIND_ORDER = (MULTIPHASE, CYCLING, STEADY)  # ties of the majority vote


@dataclass
class GroundTruthConfig:
    on_w: float = 20.0  # the device runs from this power
    min_use_w: float = 50.0  # a run must reach this power (else standby noise)
    max_gap_s: float = 600.0  # a pause this long ends a run
    min_run_s: float = 60.0
    resolution_s: float = 60.0  # per-bucket power of a multi-phase run
    cycling_steps: int = 6  # crossings of the high level of a cycling run
    cycling_max_duty: float = 0.85
    min_overlap: float = 0.5  # of the shorter of a detection and a run
    cycles: mp.CycleConfig = field(default_factory=mp.CycleConfig)
    # connected appliances (#79): the baseline around a run
    runs: RunMeasureConfig = field(default_factory=RunMeasureConfig)
    counter_lag_s: float = 1200.0  # a coarse counter reports the end of a run this late
    # the counter scales the measured energy from this total over the runs
    # (a 0.1 kWh resolution is then within 10 %)
    min_counter_kwh: float = 1.0


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
@dataclass
class Run:
    """One use of the measured appliance."""

    start: float
    end: float
    energy_kwh: float
    power_w: float  # median power while on (the high level)
    max_w: float
    kind: str  # STEADY | CYCLING | MULTIPHASE
    on_s: float | None = None  # cycling: median on-time
    period_s: float | None = None  # cycling: median period
    duty: float | None = None  # share of the run at the high level
    phases: list[mp.Phase] = field(default_factory=list)  # multiphase only
    # connected appliance (#79): measured from the curve over its state's run
    source: str | None = None  # "state", or None for a power sensor
    baseline_w: float | None = None
    program: str | None = None  # the programme state during the run
    counter_kwh: float | None = None  # the energy counter's delta over the run
    # the phase state's phases: [{state, offset_s, duration_s, power_w}]
    named_phases: list[dict] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        out = {
            "start": round(self.start),
            "end": round(self.end),
            "energy_kwh": round(self.energy_kwh, 4),
            "power_w": round(self.power_w),
            "max_w": round(self.max_w),
            "kind": self.kind,
            "duty": None if self.duty is None else round(self.duty, 3),
        }
        if self.kind == CYCLING:
            out["on_s"] = None if self.on_s is None else round(self.on_s)
            out["period_s"] = None if self.period_s is None else round(self.period_s)
        if self.phases:
            out["phases"] = [p.to_dict() for p in self.phases]
        if self.source:
            out["source"] = self.source
            out["baseline_w"] = None if self.baseline_w is None else round(self.baseline_w)
            if self.program is not None:
                out["program"] = self.program
            if self.counter_kwh is not None:
                out["counter_kwh"] = round(self.counter_kwh, 3)
            if self.named_phases:
                out["named_phases"] = [dict(p) for p in self.named_phases]
        return out

    @classmethod
    def from_dict(cls, d: Mapping) -> Run:
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            energy_kwh=float(d.get("energy_kwh") or 0.0),
            power_w=float(d.get("power_w") or 0.0),
            max_w=float(d.get("max_w") or d.get("power_w") or 0.0),
            kind=str(d.get("kind") or STEADY),
            on_s=_num(d.get("on_s")),
            period_s=_num(d.get("period_s")),
            duty=_num(d.get("duty")),
            phases=[mp.Phase.from_dict(p) for p in d.get("phases") or []],
            source=d.get("source"),
            baseline_w=_num(d.get("baseline_w")),
            program=None if d.get("program") is None else str(d["program"]),
            counter_kwh=_num(d.get("counter_kwh")),
            named_phases=[dict(p) for p in d.get("named_phases") or [] if isinstance(p, Mapping)],
        )


def _num(value) -> float | None:
    return None if value is None else float(value)


Segment = tuple[float, float, float]  # [t0, t1) at a constant power


def segments(points: Sequence[tuple[float, float | None]], start: float, end: float) -> list[Segment]:
    """The piecewise-constant series clipped to ``[start, end)``: each
    reading holds until the next one; ``None`` (unavailable) is no data."""
    out: list[Segment] = []
    pts = sorted(points, key=lambda p: p[0])
    for i, (t, v) in enumerate(pts):
        t1 = pts[i + 1][0] if i + 1 < len(pts) else end
        a, b = max(t, start), min(t1, end)
        if v is None or b <= a:
            continue
        out.append((a, b, max(float(v), 0.0)))
    return out


def covered(segs: Sequence[Segment], gap_s: float = 0.0) -> list[tuple[float, float]]:
    """The periods with data, joined across gaps of at most ``gap_s``."""
    out: list[list[float]] = []
    for a, b, _ in segs:
        if out and a - out[-1][1] <= gap_s:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _wquantile(items: Iterable[tuple[float, float]], q: float) -> float | None:
    """Weighted quantile of (value, weight) pairs."""
    pairs = sorted((v, w) for v, w in items if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * total:
            return v
    return pairs[-1][0]


def _profile(segs: Sequence[Segment], t0: float, t1: float, res: float) -> list[float]:
    """Mean power per bucket of ``res`` seconds from ``t0`` (no data: 0)."""
    n = max(int((t1 - t0 + res - 1e-9) // res), 1)
    energy = [0.0] * n
    for a, b, v in segs:
        if v <= 0:
            continue
        k0 = max(int((a - t0) // res), 0)
        k1 = min(int((b - t0) // res), n - 1)
        for k in range(k0, k1 + 1):
            b0 = t0 + k * res
            lo, hi = max(a, b0), min(b, b0 + res)
            if hi > lo:
                energy[k] += v * (hi - lo)
    return [e / res for e in energy]


def _classify(segs: Sequence[Segment], start: float, end: float, cfg: GroundTruthConfig) -> Run:
    duration = end - start
    energy = sum(v * (b - a) for a, b, v in segs) / 3.6e6
    active = [(v, b - a) for a, b, v in segs if v >= cfg.on_w]
    p90 = _wquantile(active, 0.9) or 0.0
    high = max(cfg.on_w, p90 / 2.0)
    level = _wquantile(((v, w) for v, w in active if v >= high), 0.5) or p90
    peak = max((v for v, _ in active), default=0.0)
    on_s = sum(b - a for a, b, v in segs if v >= high)
    # on runs at the high level (for a cycling run: its on-times and periods)
    ons: list[list[float]] = []
    for a, b, v in segs:
        if v >= high:
            if ons and a - ons[-1][1] <= 1e-6:
                ons[-1][1] = b
            else:
                ons.append([a, b])
    crossings = max(2 * len(ons) - 1, 0)
    duty = on_s / duration if duration > 0 else None
    cc = cfg.cycles
    heat_s = sum(b - a for a, b, v in segs if v >= cc.heat_w)
    low_s = sum(b - a for a, b, v in segs if cc.low_w <= v < cc.heat_w)
    run = Run(start, end, energy, level, peak, STEADY, duty=duty)
    if heat_s >= cc.heat_min_s and low_s >= cc.min_low_s and cc.min_cycle_s <= duration <= cc.max_cycle_s:
        run.kind = MULTIPHASE
        res = cfg.resolution_s
        run.phases = mp.segment(_profile(segs, start, end, res), res, cc)
        if not run.phases:
            run.kind = STEADY
    elif crossings >= cfg.cycling_steps and duty is not None and duty <= cfg.cycling_max_duty:
        run.kind = CYCLING
        run.on_s = statistics.median(b - a for a, b in ons)
        starts = [a for a, _ in ons]
        periods = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        run.period_s = statistics.median(periods) if periods else None
    return run


def runs_of(
    points: Sequence[tuple[float, float | None]],
    start: float,
    end: float,
    config: GroundTruthConfig | None = None,
) -> tuple[list[Run], list[tuple[float, float]]]:
    """The runs of a measured device within ``[start, end)`` and the periods
    its sensor reported. ``points`` are ``(timestamp, watts or None)``,
    sorted or not, from before ``start`` (the state at ``start``)."""
    cfg = config or GroundTruthConfig()
    segs = segments(points, start, end)
    spans: list[list[float]] = []
    for a, b, v in segs:
        if v < cfg.on_w:
            continue
        if spans and a - spans[-1][1] < cfg.max_gap_s:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    runs = []
    for a, b in spans:
        if b - a < cfg.min_run_s:
            continue
        inside = [(max(x, a), min(y, b), v) for x, y, v in segs if y > a and x < b]
        if max((v for _, _, v in inside), default=0.0) < cfg.min_use_w:
            continue
        runs.append(_classify(inside, a, b, cfg))
    return runs, covered(segs)


# ---------------------------------------------------------------------------
# runs of a connected appliance (#79)
# ---------------------------------------------------------------------------
StateSegment = tuple[float, float, str]  # [t0, t1) in one state


def state_segments(
    states: Sequence[tuple[float, str | None]], start: float, end: float
) -> list[StateSegment]:
    """The known states clipped to ``[start, end)``: each state holds until
    the next one; ``unavailable`` / ``unknown`` are no data."""
    out: list[StateSegment] = []
    pts = sorted(states, key=lambda p: p[0])
    for i, (t, v) in enumerate(pts):
        t1 = pts[i + 1][0] if i + 1 < len(pts) else end
        a, b = max(t, start), min(t1, end)
        if v is None or str(v) in UNAVAILABLE or b <= a:
            continue
        out.append((a, b, str(v)))
    return out


def _value_at(points: Sequence[tuple[float, float]], ts: float) -> float | None:
    """The last reading at or before ``ts``."""
    best = None
    for t, v in points:
        if t > ts:
            break
        best = v
    return best


def counter_delta(points: Sequence[tuple[float, float]], a: float, b: float) -> float | None:
    """What a cumulative counter (sorted ``(t, kWh)``) counted between ``a``
    and ``b``; None without a reading before ``a`` or after a reset."""
    v0, v1 = _value_at(points, a), _value_at(points, b)
    if v0 is None or v1 is None or v1 < v0:
        return None
    return v1 - v0


def _named_phases(segs: Sequence[Segment], phases: Sequence[StateSegment], a: float, b: float) -> list[dict]:
    """The phase state's phases within the run, with their mean power."""
    out: list[dict] = []
    for x, y, state in phases:
        x, y = max(x, a), min(y, b)
        if y <= x:
            continue
        if out and out[-1]["state"] == state and abs(out[-1]["_end"] - x) < 1e-6:
            out[-1]["_end"] = y
        else:
            out.append({"state": state, "_start": x, "_end": y})
    for p in out:
        x, y = p.pop("_start"), p.pop("_end")
        energy = sum(w * max(0.0, min(t1, y) - max(t0, x)) for t0, t1, w in segs)
        p.update(offset_s=round(x - a), duration_s=round(y - x), power_w=round(energy / (y - x)))
    return out


def _program(programs: Sequence[StateSegment], a: float, b: float) -> str | None:
    """The programme state holding most of the run."""
    held: dict[str, float] = {}
    for x, y, state in programs:
        o = min(y, b) - max(x, a)
        if o > 0:
            held[state] = held.get(state, 0.0) + o
    return max(held, key=lambda k: (held[k], k)) if held else None


def state_runs(
    states: Sequence[tuple[float, str | None]],
    on_states: Sequence[str],
    curve: Sequence[tuple[float, float]],
    start: float,
    end: float,
    config: GroundTruthConfig | None = None,
    phases: Sequence[tuple[float, str | None]] | None = None,
    programs: Sequence[tuple[float, str | None]] | None = None,
    counter: Sequence[tuple[float, float]] | None = None,
) -> tuple[list[Run], list[tuple[float, float]]]:
    """The runs of a connected appliance within ``[start, end)`` and the
    periods its operation state reported.

    A run is a period in one of ``on_states`` (pauses in another state
    shorter than ``max_gap_s`` stay inside it), of at least ``min_run_s``.
    Its power is what the residual load (``curve``: sorted ``(t, W)``, the
    other known loads taken out) draws above its baseline around the run
    (:func:`.known.measure_run`): no step is needed, a gradual start or
    phases of very different powers are measured as they are. Its kind and
    phases are those of a measured run (:func:`_classify`); ``phases`` (the
    phase state) names its phases with their power, ``programs`` (the
    programme state) gives its programme, ``counter`` (sorted ``(t, kWh)``
    of a cumulative energy counter) its counted energy, read
    ``counter_lag_s`` after its end (a coarse counter reports late)."""
    cfg = config or GroundTruthConfig()
    on = set(on_states)
    segs = state_segments(states, start, end)
    spans: list[list[float]] = []
    for a, b, v in segs:
        if v not in on:
            continue
        if spans and a - spans[-1][1] < cfg.max_gap_s:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    spans = [s for s in spans if s[1] - s[0] >= cfg.min_run_s]
    curve = sorted(curve)
    times = [t for t, _ in curve]
    phase_segs = state_segments(phases, start, end) if phases else []
    program_segs = state_segments(programs, start, end) if programs else []
    counter = sorted(counter) if counter else []
    runs = []
    for i, (a, b) in enumerate(spans):
        base, excess = measure_run(curve, a, b, cfg.runs, times)
        run = _classify(excess, a, b, cfg)
        run.source = "state"
        run.baseline_w = base
        run.program = _program(program_segs, a, b)
        if phase_segs:
            run.named_phases = _named_phases(excess, phase_segs, a, b)
        if counter:
            limit = spans[i + 1][0] if i + 1 < len(spans) else end
            run.counter_kwh = counter_delta(counter, a, min(b + cfg.counter_lag_s, max(limit, b)))
        runs.append(run)
    return runs, covered(segs)


# ---------------------------------------------------------------------------
# learned signature
# ---------------------------------------------------------------------------
@dataclass
class Learned:
    """The signature learned from the runs, in the registry's terms."""

    kind: str  # registry kind: steady | cycling | multiphase
    band: str  # short | medium | long | session | cycle
    power_w: float  # steady/cycling: the level; multiphase: the heating power
    duration_s: float
    uses: int  # runs of this kind (and band, programme) it was learned from
    runs: int  # all the runs
    on_s: float | None = None
    period_s: float | None = None
    duty: float | None = None
    phases: list[mp.Phase] = field(default_factory=list)
    cycle_kwh: float | None = None
    energy_kwh: float = 0.0  # median energy of one use
    # the engine can see it: a step of at least the step threshold, or a
    # multi-phase cycle with heating
    recognisable: bool = True
    # connected appliance (#79)
    source: str | None = None  # "state": learned from an operation state
    measured_kwh: float | None = None  # median energy of one use from the curve
    counter_ratio: float | None = None  # counted / measured energy over the runs
    counter_kwh: float | None = None  # what the counter counted over those runs
    # the phase state: [{state, power_w, duration_s, uses}], in cycle order
    named_phases: list[dict] = field(default_factory=list)
    program: str | None = None  # the programme it was learned from
    # every programme seen: [{program, uses, kind, power_w, duration_s,
    # energy_kwh, phases?}], most used first
    programs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        extra: dict = {}
        if self.source:
            extra = {
                "source": self.source,
                "measured_kwh": None if self.measured_kwh is None else round(self.measured_kwh, 3),
                "counter_ratio": None if self.counter_ratio is None else round(self.counter_ratio, 3),
                "counter_kwh": None if self.counter_kwh is None else round(self.counter_kwh, 3),
                "named_phases": [dict(p) for p in self.named_phases],
                "program": self.program,
                "programs": [dict(p) for p in self.programs],
            }
        return {
            **extra,
            "kind": self.kind,
            "band": self.band,
            "power_w": round(self.power_w),
            "duration_s": round(self.duration_s),
            "uses": self.uses,
            "runs": self.runs,
            "on_s": None if self.on_s is None else round(self.on_s),
            "period_s": None if self.period_s is None else round(self.period_s),
            "duty": None if self.duty is None else round(self.duty, 3),
            "phases": [p.to_dict() for p in self.phases],
            "cycle_kwh": None if self.cycle_kwh is None else round(self.cycle_kwh, 3),
            "energy_kwh": round(self.energy_kwh, 3),
            "recognisable": self.recognisable,
        }


def learn(
    runs: Sequence[Run], step_threshold_w: float = 300.0, config: GroundTruthConfig | None = None
) -> Learned | None:
    """The signature of the most frequent kind of run (None without runs).
    A steady appliance whose runs fall in several duration bands keeps the
    most frequent band; a multi-phase one its most frequent programme."""
    cfg = config or GroundTruthConfig()
    if not runs:
        return None
    counts = {k: sum(1 for r in runs if r.kind == k) for k in _KIND_ORDER}
    kind = max(_KIND_ORDER, key=lambda k: (counts[k], -_KIND_ORDER.index(k)))
    mine = [r for r in runs if r.kind == kind]
    programs = _programs(runs, cfg)
    program = None
    if kind == MULTIPHASE and programs:
        # one template per programme: the most used programme of this kind
        # is the signature, the others are listed (``programs``)
        program = next((p["program"] for p in programs if p["kind"] == MULTIPHASE), None)
        if program is not None:
            mine = [r for r in mine if r.program == program]
    if kind == MULTIPHASE:
        best, template = _template(mine, cfg)
        out = Learned(
            kind=MULTIPHASE,
            band="cycle",
            power_w=max((p.power_w for p in template), default=0.0),
            duration_s=statistics.median(s.duration for s in best),
            uses=len(best),
            runs=len(runs),
            phases=template,
            cycle_kwh=statistics.median(s.energy_kwh for s in best),
            energy_kwh=statistics.median(s.energy_kwh for s in best),
            recognisable=any(p.kind == mp.HEAT for p in template),
        )
        best_runs = {(s.start, s.end) for s in best}
        return _connected(
            out, runs, [r for r in mine if (r.start, r.end) in best_runs], programs, program, cfg
        )
    if kind == CYCLING:
        band = "session"
    else:
        bands: dict[str, int] = {}
        for r in mine:
            b = duration_band(r.duration_s)
            bands[b] = bands.get(b, 0) + 1
        band = max(("short", "medium", "long"), key=lambda b: (bands.get(b, 0), b == "medium"))
        mine = [r for r in mine if duration_band(r.duration_s) == band]
    power = statistics.median(r.power_w for r in mine)

    def med(values):
        values = [v for v in values if v is not None]
        return statistics.median(values) if values else None

    out = Learned(
        kind=kind,
        band=band,
        power_w=power,
        duration_s=statistics.median(r.duration_s for r in mine),
        uses=len(mine),
        runs=len(runs),
        on_s=med(r.on_s for r in mine) if kind == CYCLING else None,
        period_s=med(r.period_s for r in mine) if kind == CYCLING else None,
        duty=med(r.duty for r in mine) if kind == CYCLING else None,
        energy_kwh=statistics.median(r.energy_kwh for r in mine),
        recognisable=power >= step_threshold_w,
    )
    return _connected(out, runs, mine, programs, None, cfg)


def _template(runs: Sequence[Run], cfg: GroundTruthConfig) -> tuple[list[mp.Sample], list[mp.Phase]]:
    """The largest cluster of multi-phase runs and its template."""
    samples = [mp.Sample("", r.start, r.end, r.energy_kwh, r.phases, 1.0) for r in runs]
    clusters = mp.cluster(samples, cfg.cycles.match_distance)
    best = max(clusters, key=lambda m: (len(m), -m[0].start))
    return best, mp.template_of(best)


def _programs(runs: Sequence[Run], cfg: GroundTruthConfig) -> list[dict]:
    """Per programme of a connected appliance's runs (#79): its uses, its
    most frequent kind, power, duration and energy, and the template of a
    multi-phase one. Most used first; empty without a programme state."""
    groups: dict[str, list[Run]] = {}
    for r in runs:
        if r.program is not None:
            groups.setdefault(r.program, []).append(r)
    out = []
    for program, items in groups.items():
        counts = {k: sum(1 for r in items if r.kind == k) for k in _KIND_ORDER}
        kind = max(_KIND_ORDER, key=lambda k: (counts[k], -_KIND_ORDER.index(k)))
        mine = [r for r in items if r.kind == kind]
        item = {
            "program": program,
            "uses": len(items),
            "kind": kind,
            "power_w": round(statistics.median(r.power_w for r in mine)),
            "duration_s": round(statistics.median(r.duration_s for r in mine)),
            "energy_kwh": round(statistics.median(r.energy_kwh for r in mine), 3),
        }
        if kind == MULTIPHASE:
            _, template = _template(mine, cfg)
            item["power_w"] = round(max((p.power_w for p in template), default=0.0))
            item["phases"] = [p.to_dict() for p in template]
        out.append(item)
    out.sort(key=lambda p: (-p["uses"], p["program"]))
    return out


def _phase_summary(runs: Sequence[Run]) -> list[dict]:
    """The phase state's phases over the runs: per state, the median power
    and duration, in the order they usually come."""
    per: dict[str, dict] = {}
    for r in runs:
        for p in r.named_phases:
            try:
                state = str(p["state"])
                item = per.setdefault(state, {"power": [], "duration": [], "offset": []})
                item["power"].append(float(p["power_w"]))
                item["duration"].append(float(p["duration_s"]))
                item["offset"].append(float(p["offset_s"]))
            except (KeyError, TypeError, ValueError):
                continue
    out = [
        {
            "state": state,
            "power_w": round(statistics.median(v["power"])),
            "duration_s": round(statistics.median(v["duration"])),
            "uses": len(v["power"]),
            "_at": statistics.median(v["offset"]),
        }
        for state, v in per.items()
    ]
    out.sort(key=lambda p: (p["_at"], p["state"]))
    for p in out:
        p.pop("_at")
    return out


def _connected(
    out: Learned,
    runs: Sequence[Run],
    mine: Sequence[Run],
    programs: list[dict],
    program: str | None,
    cfg: GroundTruthConfig,
) -> Learned:
    """A signature learned from a connected appliance's runs (#79): its
    phases by the phase state, its programmes, and its energy checked
    against the energy counter: once the runs with a counted energy reach
    ``min_counter_kwh`` counted, the energy of one use is the measured one
    scaled by counted / measured over those runs (a coarse counter is only
    right over many runs; the curve has the appliance's neighbours in it)."""
    if not any(r.source == "state" for r in runs):
        return out
    out.source = "state"
    out.measured_kwh = out.energy_kwh
    out.named_phases = _phase_summary(mine)
    out.programs = programs
    out.program = program
    counted = [r for r in runs if r.counter_kwh is not None]
    total = sum(r.counter_kwh or 0.0 for r in counted)
    measured = sum(r.energy_kwh for r in counted)
    if counted:
        out.counter_kwh = total
    if counted and total >= cfg.min_counter_kwh and measured > 0:
        out.counter_ratio = total / measured
        out.energy_kwh = out.energy_kwh * out.counter_ratio
        if out.cycle_kwh is not None:
            out.cycle_kwh = out.cycle_kwh * out.counter_ratio
    return out


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
@dataclass
class Interval:
    start: float
    end: float
    energy_kwh: float = 0.0


def _overlap(a: Interval, b: Interval) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def _matches(a: Interval, b: Interval, min_overlap: float) -> bool:
    shorter = min(a.end - a.start, b.end - b.start)
    o = _overlap(a, b)
    return o > 0 and o >= min_overlap * shorter


def overlap_s(a: Interval, b: Interval) -> float:
    """Seconds shared by two intervals."""
    return _overlap(a, b)


def matches(a: Interval, b: Interval, min_overlap: float = 0.5) -> bool:
    """The rule of :func:`evaluate`: they overlap by at least ``min_overlap``
    of the shorter one (also used for the labelled examples, #17)."""
    return _matches(a, b, min_overlap)


def _inside(d: Interval, periods: Sequence[tuple[float, float]]) -> bool:
    """At least half of the detection is in the covered periods."""
    length = d.end - d.start
    if length <= 0:
        return any(a <= d.start < b for a, b in periods)
    o = sum(max(0.0, min(d.end, b) - max(d.start, a)) for a, b in periods)
    return o >= 0.5 * length


def evaluate(
    truth: Sequence[Interval],
    detected: Sequence[Interval],
    covered_periods: Sequence[tuple[float, float]] | None = None,
    min_overlap: float = 0.5,
) -> dict:
    """Detections of the curve against the true uses. Detections outside
    ``covered_periods`` (when given) are left out: nothing can tell whether
    they are right. Several detections may match one use (an appliance seen
    as two activations): they all count as correct, and the use as found
    once."""
    dets = [d for d in detected if covered_periods is None or _inside(d, covered_periods)]
    found = sum(1 for t in truth if any(_matches(t, d, min_overlap) for d in dets))
    correct = sum(1 for d in dets if any(_matches(t, d, min_overlap) for t in truth))
    precision = correct / len(dets) if dets else None
    recall = found / len(truth) if truth else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else (0.0 if precision is not None and recall is not None else None)
    )
    truth_kwh = sum(t.energy_kwh for t in truth)
    detected_kwh = sum(d.energy_kwh for d in dets)
    return {
        "uses": len(truth),
        "found": found,
        "missed": len(truth) - found,
        "detections": len(dets),
        "correct": correct,
        "false": len(dets) - correct,
        "precision": None if precision is None else round(precision, 3),
        "recall": None if recall is None else round(recall, 3),
        "f1": None if f1 is None else round(f1, 3),
        "truth_kwh": round(truth_kwh, 3),
        "detected_kwh": round(detected_kwh, 3),
        "energy_ratio": round(detected_kwh / truth_kwh, 3) if truth_kwh > 0 else None,
    }


def merge_evaluations(parts: Iterable[Mapping]) -> dict:
    """The evaluation of several periods (days) from their own."""
    keys = ("uses", "found", "missed", "detections", "correct", "false")
    total = {k: 0 for k in keys}
    truth_kwh = detected_kwh = 0.0
    for p in parts:
        for k in keys:
            total[k] += int(p.get(k) or 0)
        truth_kwh += float(p.get("truth_kwh") or 0.0)
        detected_kwh += float(p.get("detected_kwh") or 0.0)
    precision = total["correct"] / total["detections"] if total["detections"] else None
    recall = total["found"] / total["uses"] if total["uses"] else None
    f1 = None
    if precision is not None and recall is not None:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    return {
        **total,
        "precision": None if precision is None else round(precision, 3),
        "recall": None if recall is None else round(recall, 3),
        "f1": None if f1 is None else round(f1, 3),
        "truth_kwh": round(truth_kwh, 3),
        "detected_kwh": round(detected_kwh, 3),
        "energy_ratio": round(detected_kwh / truth_kwh, 3) if truth_kwh > 0 else None,
    }


def activation_energy_kwh(a: Mapping) -> float:
    """Energy of a stored activation (a session counts its duty; a
    recognised cycle's ``power_w`` is already its mean power)."""
    duration = float(a["end"]) - float(a["start"])
    duty = 1.0 if a.get("multiphase") else float(a.get("duty") or 1.0)
    return float(a.get("power_w") or 0.0) * duty * duration / 3.6e6


def detections(
    summary: Mapping,
    signature_id: str,
    match: Callable[[Mapping], str | None],
    template: Sequence[mp.Phase] | None = None,
    max_distance: float = 0.6,
) -> list[Interval]:
    """What the analysis of one stored day recognised as ``signature_id``:
    its activations whose registry id (``match``, e.g.
    :meth:`.registry.Matcher.match`) is that signature and, with a
    ``template``, the candidate cycles within ``max_distance`` of it (those
    the analysis would recognise once the template is validated)."""
    out: list[Interval] = []
    for a in summary.get("activations") or []:
        try:
            if match(a) == signature_id:
                out.append(Interval(float(a["start"]), float(a["end"]), activation_energy_kwh(a)))
        except (KeyError, TypeError, ValueError):
            continue
    if template:
        for c in summary.get("cycles") or []:
            try:
                phases = [mp.Phase.from_dict(p) for p in c.get("phases") or []]
                d = Interval(float(c["start"]), float(c["end"]), float(c.get("energy_kwh") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            recognised = c.get("template") == signature_id or (
                phases and mp.distance(phases, template) <= max_distance
            )
            if recognised and not any(_overlap(d, x) > 0 for x in out):
                out.append(d)
    out.sort(key=lambda x: x.start)
    return out


def truth_intervals(runs: Iterable[Mapping | Run]) -> list[Interval]:
    """The runs as intervals (stored runs or :class:`Run`)."""
    out = []
    for r in runs:
        if isinstance(r, Run):
            out.append(Interval(r.start, r.end, r.energy_kwh))
        else:
            out.append(Interval(float(r["start"]), float(r["end"]), float(r.get("energy_kwh") or 0.0)))
    return out
