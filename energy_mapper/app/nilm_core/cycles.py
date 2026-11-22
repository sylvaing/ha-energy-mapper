"""Multi-phase appliances (#26): dishwasher, washing machine, tumble dryer.

Their cycle is a sequence of phases of very different powers: water heating
(~2 kW, 10-30 min), wash or circulation (a motor or a pump, 50-500 W, often
in bursts), spin, drying. The step engine sees the heating phase as one
steady activation and misses the low-power phases (below the step
threshold). This module adds a light, deterministic layer on top of it:

1. **Candidate cycles** (:func:`find_cycles`): around each heating-like
   activation (steady, at least ``heat_min_w``, a few minutes to two hours),
   the minutes where the load stays above the base by at least ``low_w``
   (a local threshold far below the step threshold) are grouped, pauses
   shorter than ``max_gap_s`` included. A group of ``min_cycle_s`` to
   ``max_cycle_s`` with at least ``min_low_s`` of activity outside its heating
   phases is a candidate. Its own power, minute by minute, is the load minus
   the base, the switched loads and the activations that are not part of it
   (those overlapping its edges, or long ones such as a water heater).
2. **Phases** (:func:`segment`): the per-minute power is classified (heat,
   motor, idle), smoothed, split into runs, and runs shorter than
   ``min_phase_s`` are merged into their closest neighbour. Idle runs are
   not phases (their time is in the offsets).
3. **Templates** (:func:`learn`, used by the registry): candidates of all the
   stored days are clustered on a distance between phase sequences (dynamic
   time warping on the phases' power and duration, plus the cycle's energy
   and duration). A recurring cluster is a ``multiphase`` signature whose
   template is its medoid, with each phase's power and duration replaced by
   the weighted medians of the members' phases aligned to it.
4. **Attribution** (:func:`attribute`, used by the daily analysis): a
   candidate close enough to a template becomes one activation of that
   signature, with its exact per-minute power; the activations inside it
   (its heating phase, motor bursts) are absorbed so their energy is not
   counted twice.

Pure Python, standard library only; a candidate costs a few hundred
operations, a template distance about ``len(a) * len(b)``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .activations import Activation

HEAT, MOTOR, IDLE = "heat", "motor", "idle"
_RANK = {IDLE: 0, MOTOR: 1, HEAT: 2}
_LN2 = math.log(2.0)
_SKIP = 3.0  # the highest phase cost
_TIMING = 2.0  # cost of a phase moved from the start to the end of the cycle


@dataclass
class CycleConfig:
    resolution_s: float = 60.0
    low_w: float = 50.0  # a minute is active from this power above the base
    heat_w: float = 800.0  # a phase is "heat" from this mean power
    heat_min_w: float = 1000.0  # an anchor: a steady activation of at least this power...
    heat_min_s: float = 300.0  # ... lasting at least this (not a kettle)...
    heat_max_s: float = 2 * 3600.0  # ... and at most this
    long_s: float = 2.5 * 3600.0  # longer activations are never part of a cycle
    max_gap_s: float = 600.0  # a pause this long ends a cycle
    max_before_s: float = 1200.0  # a cycle starts at most this long before its first heating
    max_after_s: float = 6000.0  # and ends at most this long after its last one
    min_cycle_s: float = 1800.0
    max_cycle_s: float = 4 * 3600.0
    min_low_s: float = 600.0  # activity outside the heating phases
    min_phase_s: float = 300.0
    match_distance: float = 0.6  # template distance of a recognised cycle


@dataclass
class Phase:
    kind: str  # HEAT | MOTOR
    offset_s: float  # from the start of the cycle
    duration_s: float
    power_w: float  # mean power during the phase

    @property
    def energy_kwh(self) -> float:
        return self.power_w * self.duration_s / 3.6e6

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "offset_s": round(self.offset_s),
            "duration_s": round(self.duration_s),
            "power_w": round(self.power_w),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> Phase:
        return cls(
            str(d.get("kind") or MOTOR),
            float(d.get("offset_s") or 0.0),
            float(d["duration_s"]),
            float(d["power_w"]),
        )


@dataclass
class Cycle:
    start: float
    end: float
    resolution_s: float
    profile: list[float]  # own power (W) per bucket from ``start``
    phases: list[Phase]
    members: list[Activation] = field(default_factory=list, repr=False)
    template: str | None = None
    distance: float | None = None

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    @property
    def energy_kwh(self) -> float:
        return sum(self.profile) * self.resolution_s / 3.6e6

    @property
    def power_w(self) -> float:
        """Its highest phase power (the heating phase)."""
        return max((p.power_w for p in self.phases), default=0.0)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "power_w": round(self.power_w),
            "energy_kwh": round(self.energy_kwh, 3),
            "phases": [p.to_dict() for p in self.phases],
            "members": len(self.members),
            "template": self.template,
            "distance": None if self.distance is None else round(self.distance, 3),
        }


@dataclass
class CycleActivation(Activation):
    """A recognised cycle, as one activation of its ``multiphase`` signature:
    ``power_w`` is its mean power, ``profile`` its power minute by minute."""

    profile: list[float] = field(default_factory=list, repr=False)
    resolution_s: float = 60.0
    phases: list[Phase] = field(default_factory=list)
    multiphase: bool = True

    def _bucket(self, ts: float) -> int:
        return int((ts - self.start) // self.resolution_s)

    def power_at(self, ts: float) -> float:
        if not self.start <= ts < self.end:
            return 0.0
        i = self._bucket(ts)
        return self.profile[i] if 0 <= i < len(self.profile) else 0.0

    def overlap_energy_wh(self, start: float, end: float) -> float:
        a, b = max(start, self.start), min(end, self.end)
        if b <= a:
            return 0.0
        wh = 0.0
        res = self.resolution_s
        for i in range(max(self._bucket(a), 0), min(self._bucket(b - 1e-6), len(self.profile) - 1) + 1):
            b0 = self.start + i * res
            wh += self.profile[i] * (min(b, b0 + res) - max(a, b0)) / 3600.0
        return wh


# ---------------------------------------------------------------------------
# 1. candidate cycles
# ---------------------------------------------------------------------------
def _bucket_means(points: Sequence[tuple[float, float]], t0: float, n: int, res: float) -> list[float | None]:
    sums = [0.0] * n
    counts = [0] * n
    for t, v in points:
        i = int((t - t0) // res)
        if 0 <= i < n:
            sums[i] += v
            counts[i] += 1
    out: list[float | None] = []
    last = None
    for s, c in zip(sums, counts, strict=True):
        if c:
            last = s / c
        out.append(last)  # HA records on change: hold the last value
    return out


def _is_anchor(a: Activation, cfg: CycleConfig) -> bool:
    return (
        not a.cycling
        and not getattr(a, "multiphase", False)
        and a.power_w >= cfg.heat_min_w
        and cfg.heat_min_s <= a.duration_s <= cfg.heat_max_s
    )


def _may_belong(a: Activation, cfg: CycleConfig) -> bool:
    """Whether an activation inside a cycle's window can be one of its
    phases: not a long load, not a cycling session (a hob), not a short
    burst of heating power (a kettle)."""
    if a.cycling or getattr(a, "multiphase", False) or a.duration_s > cfg.long_s:
        return False
    return not (a.power_w >= cfg.heat_w and a.duration_s < cfg.min_phase_s)


def find_cycles(
    points: Sequence[tuple[float, float]],
    base_w: float,
    activations: Sequence[Activation],
    known: Iterable = (),
    start: float | None = None,
    end: float | None = None,
    config: CycleConfig | None = None,
) -> list[Cycle]:
    """Candidate cycles starting in ``[start, end)`` (see the module docstring).

    ``points`` is the residual load, ``activations`` the day's activations
    (sessions and pairs), ``known`` the switched loads (``energy_ws(start, end)``
    and ``claims``)."""
    cfg = config or CycleConfig()
    anchors = sorted((a for a in activations if _is_anchor(a, cfg)), key=lambda a: a.start)
    if not points or not anchors:
        return []
    res = cfg.resolution_s
    t0 = math.floor(points[0][0] / res) * res
    n = int((points[-1][0] - t0) // res) + 1
    load = _bucket_means(points, t0, n, res)
    known = list(known)
    long_acts = [a for a in activations if a.duration_s > cfg.long_s]

    def claimed(i: int, acts: Iterable[Activation]) -> float:
        b0 = t0 + i * res
        # a run-mode switched load (#79) is transparent: its cycle stays visible
        w = sum(k.energy_ws(b0, b0 + res) / res for k in known if getattr(k, "claims", True))
        return w + sum(a.overlap_energy_wh(b0, b0 + res) * 3600.0 / res for a in acts)

    # activity: what is left above the base once the switched loads and the
    # long activations are taken out (the cycle's own activations stay in)
    free = [None if v is None else v - base_w - claimed(i, long_acts) for i, v in enumerate(load)]
    active = [f is not None and f >= cfg.low_w for f in free]
    gap = int(cfg.max_gap_s // res)
    segments: list[tuple[int, int]] = []  # [i0, i1) bucket ranges
    i = 0
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i + 1
        last = i
        while j < n and j - last <= gap:
            if active[j]:
                last = j
            j += 1
        segments.append((i, last + 1))
        i = last + 1

    out: list[Cycle] = []
    for i0, i1 in segments:
        seg0, seg1 = t0 + i0 * res, t0 + i1 * res
        if seg1 - seg0 < cfg.min_cycle_s:
            continue
        members = [
            a for a in activations if a.start >= seg0 - res and a.end <= seg1 + res and _may_belong(a, cfg)
        ]
        heat = [a for a in members if _is_anchor(a, cfg)]
        if not heat:
            continue
        # bounded around the heating phases: a low load running next to the
        # cycle (a TV, a fridge) must not stretch it for hours
        lo = int((min(a.start for a in heat) - cfg.max_before_s - t0) // res)
        hi = int(math.ceil((max(a.end for a in heat) + cfg.max_after_s - t0) / res))
        i0, i1 = max(i0, lo), min(i1, hi)
        seg0, seg1 = t0 + i0 * res, t0 + i1 * res
        if not cfg.min_cycle_s <= seg1 - seg0 <= cfg.max_cycle_s:
            continue
        if start is not None and not start <= seg0 < (end if end is not None else math.inf):
            continue  # the day of its start reports it
        members = [a for a in members if a.start >= seg0 - res and a.end <= seg1 + res]
        heat = [a for a in members if _is_anchor(a, cfg)]
        ids = {id(a) for a in members}
        others = [a for a in activations if id(a) not in ids]
        profile = []
        low_s = 0.0
        for k in range(i0, i1):
            v = load[k]
            p = 0.0 if v is None else max(v - base_w - claimed(k, others), 0.0)
            profile.append(p)
            b0 = t0 + k * res
            if p >= cfg.low_w and not any(a.start < b0 + res and a.end > b0 for a in heat):
                low_s += res
        if low_s < cfg.min_low_s:
            continue
        phases = segment(profile, res, cfg)
        if not any(p.kind == HEAT for p in phases):
            continue
        out.append(
            Cycle(
                start=seg0,
                end=seg1,
                resolution_s=res,
                profile=[round(p, 1) for p in profile],
                phases=phases,
                members=members,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 2. phases
# ---------------------------------------------------------------------------
def segment(profile: Sequence[float], res: float = 60.0, config: CycleConfig | None = None) -> list[Phase]:
    """Phases of a per-bucket power profile (idle runs left out)."""
    cfg = config or CycleConfig()
    if not profile:
        return []

    def rank(p: float) -> int:
        return 2 if p >= cfg.heat_w else 1 if p >= cfg.low_w else 0

    raw = [rank(p) for p in profile]
    # median of three: a single-bucket blip does not make a phase
    ranks = list(raw)
    for i in range(1, len(raw) - 1):
        ranks[i] = sorted(raw[i - 1 : i + 2])[1]
    runs: list[list[int]] = []  # [rank, i0, i1)
    for i, r in enumerate(ranks):
        if runs and runs[-1][0] == r:
            runs[-1][2] = i + 1
        else:
            runs.append([r, i, i + 1])

    def mean(run) -> float:
        return sum(profile[run[1] : run[2]]) / (run[2] - run[1])

    min_len = max(int(round(cfg.min_phase_s / res)), 1)
    while len(runs) > 1:
        short = [(r[2] - r[1], k) for k, r in enumerate(runs) if r[2] - r[1] < min_len]
        if not short:
            break
        _, k = min(short)
        m = mean(runs[k])
        neighbours = [j for j in (k - 1, k + 1) if 0 <= j < len(runs)]
        j = min(neighbours, key=lambda j: (abs(mean(runs[j]) - m), j))
        a, b = sorted((j, k))
        keep = runs[j][0]
        runs[a : b + 1] = [[keep, runs[a][1], runs[b][2]]]
        # neighbours of the same rank join
        merged: list[list[int]] = []
        for r in runs:
            if merged and merged[-1][0] == r[0]:
                merged[-1][2] = r[2]
            else:
                merged.append(r)
        runs = merged

    out = []
    for r, i0, i1 in runs:
        if r == 0:
            continue
        p = sum(profile[i0:i1]) / (i1 - i0)
        out.append(Phase(HEAT if p >= cfg.heat_w else MOTOR, i0 * res, (i1 - i0) * res, p))
    return out


# ---------------------------------------------------------------------------
# 3. distance between phase sequences
# ---------------------------------------------------------------------------
def _lnr(a: float, b: float) -> float:
    return abs(math.log(max(a, 1e-3) / max(b, 1e-3))) / _LN2


def phase_cost(a: Phase, b: Phase) -> float:
    """0 for identical phases; 1 for a power ratio of 2 (or a duration ratio
    of 4); at most 3."""
    return min(_lnr(a.power_w, b.power_w) + 0.5 * _lnr(a.duration_s, b.duration_s), _SKIP)


def _dtw(a: Sequence[Phase], b: Sequence[Phase]) -> tuple[float, list[tuple[int, int]]]:
    """Dynamic time warping where each phase weighs its share of its cycle's
    energy, counted once: a pair costs ``(wa + wb) / 2 x phase_cost`` when
    both phases are new, ``wa / 2 x ...`` when only ``a``'s phase is (it is
    aligned to a ``b`` phase already used), and so on. A short low-power
    phase (a fridge starting next to the cycle, a heating split in two)
    costs little. Leading and trailing phases of either side can be
    skipped at the highest cost (3) times their weight. Returns the total cost and the
    alignment path (skipped phases are not in it)."""
    n, m = len(a), len(b)
    ea = sum(p.energy_kwh for p in a) or 1.0
    eb = sum(p.energy_kwh for p in b) or 1.0
    wa = [0.5 * p.energy_kwh / ea for p in a]
    wb = [0.5 * p.energy_kwh / eb for p in b]
    ca, cb = _centers(a), _centers(b)

    def cost(i: int, j: int) -> float:
        # what the phases are, and when in the cycle (the second heating of
        # a dishwasher is not the only one of a washing machine)
        return min(phase_cost(a[i], b[j]) + _TIMING * abs(ca[i] - cb[j]), _SKIP)

    inf = math.inf
    d = [[inf] * (m + 1) for _ in range(n + 1)]
    d[0][0] = 0.0
    for i in range(1, n + 1):
        d[i][0] = d[i - 1][0] + _SKIP * wa[i - 1]
    for j in range(1, m + 1):
        d[0][j] = d[0][j - 1] + _SKIP * wb[j - 1]
    for i in range(1, n + 1):
        row, prev = d[i], d[i - 1]
        for j in range(1, m + 1):
            c = cost(i - 1, j - 1)
            row[j] = min(
                prev[j - 1] + (wa[i - 1] + wb[j - 1]) * c,
                prev[j] + wa[i - 1] * c,
                row[j - 1] + wb[j - 1] * c,
            )
    # open end: the last phases of one side may be skipped
    ends = [(d[n][m], n, m)]
    tail = 0.0
    for i in range(n - 1, 0, -1):
        tail += _SKIP * wa[i]
        ends.append((d[i][m] + tail, i, m))
    tail = 0.0
    for j in range(m - 1, 0, -1):
        tail += _SKIP * wb[j]
        ends.append((d[n][j] + tail, n, j))
    total, i, j = min(ends)
    path = []
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        c = cost(i - 1, j - 1)
        options = (
            (d[i - 1][j - 1] + (wa[i - 1] + wb[j - 1]) * c, 0, i - 1, j - 1),
            (d[i - 1][j] + wa[i - 1] * c, 1, i - 1, j),
            (d[i][j - 1] + wb[j - 1] * c, 2, i, j - 1),
        )
        _, _, i, j = min(options)
    path.reverse()
    return total, path


def _centers(phases: Sequence[Phase]) -> list[float]:
    """Middle of each phase, as a fraction of the cycle's span."""
    if not phases:
        return []
    t0 = min(p.offset_s for p in phases)
    span = max(p.offset_s + p.duration_s for p in phases) - t0 or 1.0
    return [(p.offset_s + p.duration_s / 2 - t0) / span for p in phases]


def _total(phases: Sequence[Phase]) -> tuple[float, float]:
    """Energy (kWh) and span (s) of a phase sequence."""
    energy = sum(p.energy_kwh for p in phases)
    span = max((p.offset_s + p.duration_s for p in phases), default=0.0) - min(
        (p.offset_s for p in phases), default=0.0
    )
    return energy, span


def distance(a: Sequence[Phase], b: Sequence[Phase]) -> float:
    """Distance between two cycles' phase sequences (0 = identical): the
    energy-weighted DTW cost, plus the energy and span ratios."""
    if not a or not b:
        return math.inf
    cost, _ = _dtw(a, b)
    ea, sa = _total(a)
    eb, sb = _total(b)
    return cost + 0.5 * _lnr(ea, eb) + 0.25 * _lnr(max(sa, 1.0), max(sb, 1.0))


# ---------------------------------------------------------------------------
# 4. templates (the registry) and attribution (the daily analysis)
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    """A stored candidate cycle, weighted by its age (registry input)."""

    day: str
    start: float
    end: float
    energy_kwh: float
    phases: list[Phase]
    weight: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def power(self) -> float:
        return max((p.power_w for p in self.phases), default=0.0)


def _wmedian(pairs: Iterable[tuple[float, float]]) -> float | None:
    items = sorted((v, w) for v, w in pairs if w > 0)
    if not items:
        return None
    half = sum(w for _, w in items) / 2.0
    acc = 0.0
    for v, w in items:
        acc += w
        if acc >= half:
            return v
    return items[-1][0]


def _medoid(members: list[Sample], cap: int = 40) -> Sample:
    """The member closest to the others (among the ``cap`` heaviest)."""
    pool = sorted(members, key=lambda s: (-s.weight, s.start))[:cap]
    if len(pool) <= 2:
        return pool[0]
    best, best_cost = pool[0], math.inf
    for s in pool:
        c = sum(o.weight * distance(s.phases, o.phases) for o in pool if o is not s)
        if c < best_cost - 1e-12:
            best, best_cost = s, c
    return best


def cluster(samples: Sequence[Sample], max_distance: float, iterations: int = 3) -> list[list[Sample]]:
    """Deterministic leader clustering with medoids: in start order, each
    sample joins the nearest medoid within ``max_distance`` or starts a new
    cluster; the medoids are recomputed and the samples reassigned."""
    ordered = sorted(samples, key=lambda s: (s.start, s.end))
    medoids: list[Sample] = []
    clusters: list[list[Sample]] = []
    for _ in range(max(iterations, 1)):
        groups: list[list[Sample]] = [[] for _ in medoids]
        reps = list(medoids)
        for s in ordered:
            best, best_d = None, math.inf
            for k, r in enumerate(reps):
                d = distance(s.phases, r.phases)
                if d <= max_distance and d < best_d:
                    best, best_d = k, d
            if best is None:
                reps.append(s)
                groups.append([s])
            else:
                groups[best].append(s)
        groups = [g for g in groups if g]
        new = [_medoid(g) for g in groups]
        stable = [id(x) for x in new] == [id(x) for x in medoids]
        medoids, clusters = new, groups
        if stable:
            break
    return clusters


def template_of(members: list[Sample], medoid: Sample | None = None) -> list[Phase]:
    """The medoid's phases, each one's power, duration and offset replaced by
    the weighted medians over the members' phases aligned to it."""
    medoid = medoid or _medoid(members)
    n = len(medoid.phases)
    powers: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    durations: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    offsets: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    for s in members:
        _, path = _dtw(s.phases, medoid.phases)
        dur = [0.0] * n
        off: list[float | None] = [None] * n
        pw: list[list[float]] = [[] for _ in range(n)]
        for i, j in path:
            p = s.phases[i]
            dur[j] += p.duration_s
            pw[j].append(p.power_w)
            off[j] = p.offset_s if off[j] is None else min(off[j], p.offset_s)
        for j in range(n):
            if pw[j]:
                powers[j].append((statistics.mean(pw[j]), s.weight))
                durations[j].append((dur[j], s.weight))
                offsets[j].append((off[j] or 0.0, s.weight))
    out = []
    for j, ph in enumerate(medoid.phases):
        p = _wmedian(powers[j]) or ph.power_w
        out.append(
            Phase(
                ph.kind,
                _wmedian(offsets[j]) or ph.offset_s,
                _wmedian(durations[j]) or ph.duration_s,
                p,
            )
        )
    return out


def match(phases: Sequence[Phase], templates: Mapping[str, Sequence[Phase]], max_distance: float):
    """``(template id, distance)`` of the nearest template within
    ``max_distance``, or ``(None, None)``."""
    best, best_d = None, math.inf
    for tid in sorted(templates):
        d = distance(phases, templates[tid])
        if d <= max_distance and d < best_d:
            best, best_d = tid, d
    return (best, best_d) if best is not None else (None, None)


def trimmed(cycle: Cycle, template: Sequence[Phase]) -> Cycle:
    """The cycle without its leading and trailing phases the template does
    not align (a fridge or a TV running next to it)."""
    _, path = _dtw(cycle.phases, template)
    if not path:
        return cycle
    first, last = path[0][0], path[-1][0]
    if first == 0 and last == len(cycle.phases) - 1:
        return cycle
    res = cycle.resolution_s
    i0 = int(cycle.phases[first].offset_s // res)
    p = cycle.phases[last]
    i1 = int(math.ceil((p.offset_s + p.duration_s) / res))
    start, end = cycle.start + i0 * res, cycle.start + i1 * res
    shift = i0 * res
    return Cycle(
        start=start,
        end=end,
        resolution_s=res,
        profile=cycle.profile[i0:i1],
        phases=[
            Phase(x.kind, x.offset_s - shift, x.duration_s, x.power_w) for x in cycle.phases[first : last + 1]
        ],
        members=[a for a in cycle.members if a.start < end and a.end > start],
    )


def attribute(
    cycles: Sequence[Cycle],
    activations: list[Activation],
    templates: Mapping[str, Sequence[Phase]],
    config: CycleConfig | None = None,
) -> list[Activation]:
    """Match the candidates to the templates; a recognised cycle (trimmed to
    the phases its template aligns) replaces its member activations by one
    :class:`CycleActivation` (signature = the template id), and the
    candidate gets ``template`` and ``distance``. Returns the new list of
    activations."""
    cfg = config or CycleConfig()
    if not templates:
        return list(activations)
    absorbed: set[int] = set()
    made: list[Activation] = []
    for c in cycles:
        if any(id(a) in absorbed for a in c.members):
            continue
        tid, d = match(c.phases, templates, cfg.match_distance)
        if tid is None:
            continue
        c.template, c.distance = tid, d
        t = trimmed(c, templates[tid])
        absorbed.update(id(a) for a in t.members)
        mean = sum(t.profile) / len(t.profile) if t.profile else 0.0
        made.append(
            CycleActivation(
                start=t.start,
                end=t.end,
                power_w=round(mean, 1),
                signature=tid,
                profile=list(t.profile),
                resolution_s=t.resolution_s,
                phases=list(t.phases),
            )
        )
    out = [a for a in activations if id(a) not in absorbed] + made
    out.sort(key=lambda a: a.start)
    return out
