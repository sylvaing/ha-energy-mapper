"""Cold appliances (#89): the regular cycles of low power (fridge, freezer).

A compressor runs a few minutes, pauses, and starts again, day and night,
all year. Its steps are far below the fixed thresholds: only the
noise-adaptive threshold (#88) keeps them, as ``low`` steps, and pairing
each off-step with the on-step closest in size mixes two such appliances
up (a compressor starts higher than it stops, and the off-step of one is
often the size of the on-step of the other). So they have their own model,
learned where the curve is quiet and followed through the whole day.

**Learning** (:func:`learn`), on the low steps of the quiet stretches (no
solar production, no step of the fixed thresholds around): every on-step
is tried with every off-step that follows it within an hour; the pairs of
one appliance are dense in (on-step, off-step, run length), those of two
different appliances are not. Each dense group becomes a chain of runs
(one edge, one run), and is kept when its pauses are regular. Two groups
with the same steps and other run lengths are one appliance when their
union is at least as regular (a compressor with a long run every few
ones), and the runs that fill a hole of the chain are adopted whatever
their length.

**The model** (:class:`Model`) is a two-state machine with explicit
durations: the on-step, the off-step, the mean power of a run measured on
the curve, the usual run lengths, the usual pause.

**Following** (:func:`follow`) is the best path of that machine through
the low steps of the day, quiet or not. A run is a pair of steps of the
model's sizes and of a usual length (*detected*), or one step of the right
size completed by the other edge, looked for in the curve where the run
length says it is due, under the adaptive threshold (*completed*). A
dynamic programme keeps the chain of runs with the most evidence whose
pauses are the model's: a detection that spans two runs is two completed
runs, one or two runs missing between two others are *filled* (and their
edges looked for in the curve). Where more is missing, the appliance is
not followed: its energy there is **estimated** from the duty learned in
the quiet hours, and reported as such.

The learned models persist in the registry (signatures of kind ``cold``,
:mod:`.registry`) and come back to the analysis of the next days
(``AnalysisConfig.cold_models``): on a day whose quiet hours taught
nothing, the appliance is still followed, or estimated.

Pure and standard library only. Nightly only: the live view lists the low
steps and opens no event for them.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from .steps import ROBUST_SIGMA, Signal, Step

KIND = "cold"
BAND = "cold"
GROUP = (KIND, BAND)
DETECTED, COMPLETED, PLACED, FILLED = "detected", "completed", "placed", "filled"
HOWS = (DETECTED, COMPLETED, PLACED, FILLED)


@dataclass(frozen=True)
class ColdConfig:
    min_on_s: float = 120.0  # a run lasts at least this
    max_on_s: float = 3600.0  # ... and at most this
    # two steps are the same edge of the same appliance within this
    size_tol: float = 0.12
    size_tol_w: float = 8.0
    track_tol: float = 1.5  # x that tolerance when following (the noisy hours measure worse)
    off_ratio: tuple[float, float] = (0.55, 1.2)  # off-step / on-step of one run
    duration_tol: float = 0.2  # two runs have the same length within this
    duration_tol_s: float = 45.0
    pause_tol: float = 0.3  # a pause is the usual one within this, when following
    learn_pause_tol: float = 0.15  # ... and within this in the quiet hours, to call it a rhythm
    max_period_s: float = 7200.0  # a compressor that starts less often than this is not followed
    min_cycles: int = 5  # runs of a group, in the quiet stretches of one analysis
    min_regular: float = 0.6  # share of its pauses that are the usual one
    max_holes: int = 3  # runs missing in a pause of a chain; longer pauses are not judged
    max_fill: int = 2  # runs filled between two runs; more is estimated
    quiet_margin_s: float = 300.0  # no step of the fixed thresholds this close to a quiet run
    search_window_s: float = 60.0  # medians of the curve when looking for an edge
    edge_share: tuple[float, float] = (0.5, 1.5)  # an edge found in the curve, x the model's step
    edge_noise: float = 3.0  # ... and at least this many times the noise of the curve around it
    min_evidence: int = 3  # runs the curve shows, for an appliance to be reported
    # a stored model the day did not teach again: this share of the runs its
    # period expects must be detected (an appliance that is gone finds a few
    # steps of its size from other loads, and would be estimated for ever)
    min_seen: float = 0.15
    # a known appliance learned again takes the day's values when its period is the stored one within this
    model_tol: float = 0.25
    # the score of a run, and what a transition costs (:func:`follow`)
    score_detected: float = 2.0
    score_unusual: float = 1.5  # two steps, another run length than the usual ones
    score_found: float = 1.2  # one step, the other edge found in the curve
    score_placed: float = 0.6  # one step, the other edge placed by the mean run length
    cost_fill: float = 0.5
    cost_break: float = 1.5

    def near(self, w: float, ref: float, factor: float = 1.0) -> bool:
        return abs(w - ref) <= factor * max(self.size_tol_w, self.size_tol * ref)

    def same_length(self, s: float, ref: float) -> bool:
        return abs(s - ref) <= max(self.duration_tol_s, self.duration_tol * ref)


COLD = ColdConfig()


@dataclass
class Model:
    """One cold appliance: a two-state machine with its usual durations."""

    on_w: float
    off_w: float
    power_w: float  # mean power of a run, measured on the curve
    on_s: list[float]  # the usual run lengths, the most frequent first
    run_s: float  # the mean run
    pause_s: float  # the usual pause between two runs
    id: str | None = None  # the registry's, for a model that was given
    cycles: int = 0  # quiet runs it was learned from in this analysis (0: given)
    ignored: bool = False  # the user ignored it: followed (its steps are its own), not reported

    @property
    def period_s(self) -> float:
        return self.run_s + self.pause_s

    @property
    def duty(self) -> float:
        return self.run_s / self.period_s if self.period_s > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "on_w": round(self.on_w),
            "off_w": round(self.off_w),
            "power_w": round(self.power_w),
            "on_s": round(self.on_s[0]),  # the most frequent run length
            "on_modes_s": [round(s) for s in self.on_s],
            "run_s": round(self.run_s),
            "pause_s": round(self.pause_s),
            "period_s": round(self.period_s),
            "duty": round(self.duty, 3),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> Model | None:
        """A stored model (:meth:`to_dict`, a registry signature); None
        when it is not one (a signature expected before it is seen)."""
        try:
            on_s = [float(s) for s in d.get("on_modes_s") or [d["on_s"]]]
            run_s = float(d.get("run_s") or on_s[0])
            model = cls(
                on_w=float(d["on_w"]),
                off_w=float(d["off_w"]),
                power_w=float(d["power_w"]),
                on_s=on_s,
                run_s=run_s,
                pause_s=float(d["pause_s"]),
                id=d.get("id"),
                ignored=bool(d.get("ignored")),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        ok = min(model.on_w, model.off_w, model.power_w, model.run_s, model.pause_s, *model.on_s) > 0
        return model if ok else None


def alike(a: Mapping, b: Mapping, cfg: ColdConfig = COLD) -> float | None:
    """How far two models (as dicts: ``on_w``, ``off_w``) are from each
    other, None when they are not the same appliance: both steps within
    the size tolerance. Two appliances with the same steps are one for the
    curve, whatever their periods: their edges cannot be told apart."""
    try:
        on_a, on_b = float(a["on_w"]), float(b["on_w"])
        off_a, off_b = float(a["off_w"]), float(b["off_w"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(on_a, on_b, off_a, off_b) <= 0:
        return None
    if not (cfg.near(on_a, on_b) and cfg.near(off_a, off_b)):
        return None
    return abs(on_a - on_b) / on_b + abs(off_a - off_b) / off_b


class Quiet:
    """Where the models are learned: no step of the fixed thresholds within
    ``margin_s`` of the run, and no solar production at its two edges
    (``calm(ts)``: the panels produce nothing; None without a solar meter)."""

    def __init__(self, loud: Sequence[float], calm: Callable[[float], bool] | None, margin_s: float):
        self.loud = sorted(loud)
        self.calm = calm
        self.margin_s = margin_s

    def __call__(self, start: float, end: float) -> bool:
        i = bisect.bisect_left(self.loud, start - self.margin_s)
        if i < len(self.loud) and self.loud[i] <= end + self.margin_s:
            return False
        return self.calm is None or (self.calm(start) and self.calm(end))


# ---------------------------------------------------------------------------
# the curve
# ---------------------------------------------------------------------------
def _mean(sig: Signal, start: float, end: float) -> float | None:
    """The time-weighted mean of the curve within ``[start, end)``."""
    i = bisect.bisect_left(sig.ts, start)
    j = bisect.bisect_left(sig.ts, end)
    if j - i < 2:
        return None
    total = sum(sig.v[k] * (sig.ts[k + 1] - sig.ts[k]) for k in range(i, j - 1))
    span = sig.ts[j - 1] - sig.ts[i]
    return total / span if span > 0 else None


def run_power(sig: Signal, start: float, end: float, window_s: float) -> float | None:
    """The mean power of one run: the curve during it, above the level just
    before and just after."""
    margin = min(15.0, (end - start) / 4)
    inside = _mean(sig, start + margin, end - margin)
    before = sig.median(start - window_s, start - 1e-3)
    after = sig.median(end + margin, end + margin + window_s)
    if inside is None or before is None or after is None:
        return None
    power = inside - (before + after) / 2
    # something else switched during the run: its level says nothing
    return power if abs(after - before) <= 0.25 * abs(power) else None


def find_edge(
    sig: Signal, start: float, end: float, sign: float, size: float, cfg: ColdConfig
) -> tuple[float, float] | None:
    """The reading within ``[start, end]`` where the curve moves the most in
    the direction ``sign``, the medians of ``search_window_s`` after and
    before it compared: ``(ts, size)`` when the move is the model's step
    (``edge_share``), else None. This is how an edge the detector did not
    keep is found: where the model says one is due, a lower bar is enough."""
    i = bisect.bisect_left(sig.ts, start)
    j = bisect.bisect_right(sig.ts, end)
    best: tuple[float, float] | None = None
    moves = []
    for k in range(i, j):
        t = sig.ts[k]
        before = sig.median(t - cfg.search_window_s, t - 1e-3)
        after = sig.median(t, t + cfg.search_window_s)
        if before is None or after is None:
            continue
        d = sign * (after - before)
        moves.append(abs(d))
        if best is None or d > best[1]:
            best = (t, d)
    if best is None:
        return None
    lo, hi = cfg.edge_share
    # the curve moves that much there anyway: not an edge
    noise = ROBUST_SIGMA * statistics.median(moves)
    return best if max(lo * size, cfg.edge_noise * noise) <= best[1] <= hi * size else None


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------
@dataclass
class _Pair:
    up: int
    down: int
    on_w: float
    off_w: float
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def _edges(steps: Sequence[Step]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """``(ups, downs)``: the low steps nothing explained, as ``(ts, size)``."""
    free = sorted((s for s in steps if s.low and not s.explained_by), key=lambda s: s.ts)
    return (
        [(s.ts, s.delta) for s in free if s.delta > 0],
        [(s.ts, -s.delta) for s in free if s.delta < 0],
    )


def _pairs(ups, downs, quiet: Callable[[float, float], bool], cfg: ColdConfig) -> list[_Pair]:
    """Every quiet pair of an on-step and a later off-step that can be one run."""
    down_ts = [t for t, _ in downs]
    up_ts = [t for t, _ in ups]
    out = []
    lo, hi = cfg.off_ratio
    for i, (t, w) in enumerate(ups):
        a = bisect.bisect_left(down_ts, t + cfg.min_on_s)
        b = bisect.bisect_right(down_ts, t + cfg.max_on_s)
        first = bisect.bisect_right(down_ts, t)
        for j in range(a, b):
            td, wd = downs[j]
            if not (lo * w <= wd <= hi * w and quiet(t, td)):
                continue
            # one run has no other edge of its own inside: a periodic
            # appliance would else pair each start with a later stop too
            if any(cfg.near(downs[k][1], wd) for k in range(first, j)):
                continue
            if any(cfg.near(ups[k][1], w) for k in range(i + 1, bisect.bisect_left(up_ts, td))):
                continue
            out.append(_Pair(i, j, w, wd, t, td))
    return out


def _distance(p: _Pair, on_w: float, off_w: float, length: float, cfg: ColdConfig) -> float:
    """At most 1 when ``p`` is a run of these steps and this length."""
    return max(
        abs(p.on_w - on_w) / max(cfg.size_tol_w, cfg.size_tol * on_w),
        abs(p.off_w - off_w) / max(cfg.size_tol_w, cfg.size_tol * off_w),
        abs(p.length - length) / max(cfg.duration_tol_s, cfg.duration_tol * length),
    )


def _one_each(pairs: Sequence[_Pair], key) -> list[_Pair]:
    """The pairs in the order of ``key``, each edge in one pair only and no
    two runs overlapping; sorted by start."""
    ups: set[int] = set()
    downs: set[int] = set()
    kept: list[_Pair] = []
    for p in sorted(pairs, key=key):
        if p.up in ups or p.down in downs:
            continue
        if any(p.start < q.end and q.start < p.end for q in kept):
            continue
        ups.add(p.up)
        downs.add(p.down)
        kept.append(p)
    kept.sort(key=lambda p: p.start)
    return kept


def _dense(pairs: list[_Pair], cfg: ColdConfig) -> list[list[_Pair]]:
    """The dense groups of ``pairs`` in (on-step, off-step, length), the
    largest first; each one a chain of runs (one edge, one run)."""
    out: list[list[_Pair]] = []
    alive = list(pairs)
    while len(alive) >= cfg.min_cycles:
        best: list[_Pair] = []
        for p in alive:
            near = [q for q in alive if _distance(q, p.on_w, p.off_w, p.length, cfg) <= 1.0]
            if min(len({q.up for q in near}), len({q.down for q in near})) > len(best):
                best = near
        if len(best) < cfg.min_cycles:
            break
        on_w = statistics.median(p.on_w for p in best)
        off_w = statistics.median(p.off_w for p in best)
        length = statistics.median(p.length for p in best)
        members = [p for p in alive if _distance(p, on_w, off_w, length, cfg) <= 1.0]
        centre = (on_w, off_w, length)
        chain = _one_each(members, lambda p, c=centre: _distance(p, *c, cfg))
        if len(chain) < cfg.min_cycles:
            # not a group after all: its densest pairs are set aside
            gone = {id(p) for p in best}
            alive = [p for p in alive if id(p) not in gone]
            continue
        out.append(chain)
        ups = {p.up for p in chain}
        downs = {p.down for p in chain}
        alive = [p for p in alive if p.up not in ups and p.down not in downs]
    return out


def _regularity(chain: Sequence[_Pair], cfg: ColdConfig) -> tuple[float, float]:
    """``(pause, share)``: the usual pause between two runs of the chain
    and the share of its pauses that are that one, or that one with up to
    ``max_holes`` runs missing in between (0 under ``min_cycles - 1``
    usual pauses)."""
    pauses = [b.start - a.end for a, b in pairwise(chain)]
    if not pauses:
        return 0.0, 0.0
    # the usual pause is the shortest common one: the longer ones are holes
    usual = statistics.median(pauses)
    for _ in range(8):
        shorter = statistics.median(g for g in pauses if g <= usual * (1 + cfg.learn_pause_tol))
        if shorter >= usual:
            break
        usual = shorter
    slack = cfg.learn_pause_tol * usual
    near = [g for g in pauses if abs(g - usual) <= slack]
    if len(near) < cfg.min_cycles - 1:
        return usual, 0.0
    usual = statistics.median(near)
    period = usual + statistics.median(p.length for p in chain)
    judged = fitting = 0
    for g in pauses:
        n = round((g - usual) / period)
        if n > cfg.max_holes:
            continue  # the chain was not followed there: says nothing
        judged += 1
        fitting += abs(g - usual - n * period) <= slack
    return usual, fitting / judged if judged else 0.0


def _centre(chain: Sequence[_Pair]) -> tuple[float, float]:
    return statistics.median(p.on_w for p in chain), statistics.median(p.off_w for p in chain)


def _merge(chains: list[list[_Pair]], cfg: ColdConfig) -> list[list[_Pair]]:
    """Two groups of the same steps are one appliance: the curve cannot
    tell their edges apart. Their union when it is a chain at least as
    regular as each of them (other run lengths of the same compressor),
    else the larger one alone (``chains``: the largest first)."""
    out: list[list[_Pair]] = []
    for chain in chains:
        on_w, off_w = _centre(chain)
        for k, other in enumerate(out):
            o_on, o_off = _centre(other)
            if not (cfg.near(on_w, o_on) and cfg.near(off_w, o_off)):
                continue
            union = sorted(other + chain, key=lambda p: p.start)
            share = _regularity(union, cfg)[1]
            apart = all(a.end <= b.start for a, b in pairwise(union))
            if apart and share >= max(_regularity(other, cfg)[1], _regularity(chain, cfg)[1]) - 1e-9:
                out[k] = union
            break
        else:
            out.append(chain)
    return out


def _adopt(
    chain: list[_Pair], pairs: Sequence[_Pair], taken: tuple[set, set], cfg: ColdConfig
) -> list[_Pair]:
    """The runs of the same steps that fill a hole of the chain, whatever
    their length: the pauses before and after them are the usual one."""
    pause, _ = _regularity(chain, cfg)
    if pause <= 0:
        return chain
    on_w, off_w = _centre(chain)
    ups, downs = taken
    chain = list(chain)
    spare = [
        p
        for p in pairs
        if p.up not in ups and p.down not in downs and cfg.near(p.on_w, on_w) and cfg.near(p.off_w, off_w)
    ]
    starts = [p.start for p in chain]
    for p in sorted(spare, key=lambda p: p.start):
        if p.up in ups or p.down in downs:
            continue
        i = bisect.bisect_left(starts, p.start)
        if i == 0 or i == len(chain):
            continue
        before, after = chain[i - 1], chain[i]
        usual = cfg.learn_pause_tol * pause
        if abs(p.start - before.end - pause) <= usual and abs(after.start - p.end - pause) <= usual:
            chain.insert(i, p)
            starts.insert(i, p.start)
            ups.add(p.up)
            downs.add(p.down)
    return chain


def _modes(lengths: Sequence[float], cfg: ColdConfig) -> list[float]:
    """The usual run lengths, the most frequent first (at least two runs
    and a tenth of them; the most frequent one always)."""
    groups: list[list[float]] = []
    for s in sorted(lengths):
        if groups and cfg.same_length(s, statistics.median(groups[-1])):
            groups[-1].append(s)
        else:
            groups.append([s])
    groups.sort(key=lambda g: -len(g))
    floor = max(2, len(lengths) / 10)
    return [statistics.median(g) for k, g in enumerate(groups) if k == 0 or len(g) >= floor]


def _model(chain: Sequence[_Pair], sig: Signal, edges: Sequence[float], cfg: ColdConfig) -> Model | None:
    pause, share = _regularity(chain, cfg)
    if len(chain) < cfg.min_cycles or share < cfg.min_regular or pause <= 0:
        return None
    on_w, off_w = _centre(chain)
    lengths = [p.length for p in chain]
    if pause + statistics.fmean(lengths) > cfg.max_period_s:
        return None
    win = cfg.search_window_s
    # measured on the runs during which nothing else switched
    clean = [
        p
        for p in chain
        if bisect.bisect_right(edges, p.end + 2 * win) - bisect.bisect_left(edges, p.start - win) == 2
    ]
    powers = [w for p in clean if (w := run_power(sig, p.start, p.end, win)) is not None]
    power = statistics.median(powers) if powers else 0.0
    if power <= 0:
        power = (on_w + off_w) / 2
    return Model(
        on_w=on_w,
        off_w=off_w,
        power_w=power,
        on_s=_modes(lengths, cfg),
        run_s=statistics.fmean(lengths),
        pause_s=pause,
        cycles=len(chain),
    )


def learn(
    steps: Sequence[Step],
    points: Sequence[tuple[float, float]],
    quiet: Callable[[float, float], bool],
    cfg: ColdConfig = COLD,
) -> list[Model]:
    """The cold appliances the quiet stretches show: the regular groups of
    the low steps of ``steps``, the one with the most runs first."""
    ups, downs = _edges(steps)
    pairs = _pairs(ups, downs, quiet, cfg)
    chains = _merge(_dense(pairs, cfg), cfg)
    taken = ({p.up for c in chains for p in c}, {p.down for c in chains for p in c})
    sig = Signal(points)
    edges = sorted(t for t, _ in [*ups, *downs])
    models = []
    for chain in chains:
        model = _model(_adopt(chain, pairs, taken, cfg), sig, edges, cfg)
        if model is not None:
            models.append(model)
    models.sort(key=lambda m: -m.cycles)
    return models


# ---------------------------------------------------------------------------
# following
# ---------------------------------------------------------------------------
@dataclass
class Track:
    """One cold appliance through one analysis."""

    model: Model
    # the runs: ``(start, end, how)``, how = DETECTED | COMPLETED | PLACED | FILLED
    cycles: list[tuple[float, float, str]] = field(default_factory=list)
    # where it was not followed: ``(start, end, mean power)`` from the duty
    estimated: list[tuple[float, float, float]] = field(default_factory=list)
    signature: str | None = None
    _starts: list[float] | None = field(default=None, repr=False, compare=False)

    def evidence(self, start: float, end: float, hows: tuple[str, ...] = (DETECTED, COMPLETED)) -> int:
        """The runs within ``[start, end)`` the curve shows: two steps, or
        one step and the other edge found."""
        return sum(1 for a, b, how in self.cycles if how in hows and b > start and a < end)

    def power_at(self, ts: float) -> float:
        if self._starts is None:
            self._starts = [c[0] for c in self.cycles]
        i = bisect.bisect_right(self._starts, ts) - 1
        if i >= 0 and ts < self.cycles[i][1]:
            return self.model.power_w
        return next((w for a, b, w in self.estimated if a <= ts < b), 0.0)

    def overlap_energy_wh(self, start: float, end: float, estimated: bool | None = None) -> float:
        """Energy used within ``[start, end)``: its runs and, where it was
        not followed, the estimate (``estimated``: only that part when
        true, only the runs when false)."""
        total = 0.0
        if estimated is not True:
            on_s = sum(max(0.0, min(end, b) - max(start, a)) for a, b, _ in self.cycles)
            total += self.model.power_w * on_s
        if estimated is not False:
            total += sum(w * max(0.0, min(end, b) - max(start, a)) for a, b, w in self.estimated)
        return total / 3600.0


@dataclass
class _Run:
    start: float
    end: float
    score: float
    how: str
    up: int | None = None
    down: int | None = None


def _candidates(model: Model, ups, downs, taken, sig: Signal, cfg: ColdConfig) -> list[_Run]:
    """Every run the steps allow: two steps of the model's sizes a usual
    length apart, or one step with the other edge found in the curve (or
    placed by the mean run length)."""
    used_ups, used_downs = taken
    mine_up = [
        i for i, (_, w) in enumerate(ups) if i not in used_ups and cfg.near(w, model.on_w, cfg.track_tol)
    ]
    mine_down = [
        j for j, (_, w) in enumerate(downs) if j not in used_downs and cfg.near(w, model.off_w, cfg.track_tol)
    ]
    out: list[_Run] = []
    up_ts = [ups[i][0] for i in mine_up]
    down_ts = [downs[j][0] for j in mine_down]
    for i in mine_up:
        t = ups[i][0]
        for j in mine_down:
            td = downs[j][0]
            length = td - t
            if any(cfg.same_length(length, m) for m in model.on_s):
                out.append(_Run(t, td, cfg.score_detected, DETECTED, i, j))
            elif cfg.min_on_s <= length < model.period_s:
                # another length (a door left open, a long run): its two
                # steps, when no other edge of the appliance is in between
                between_ups = bisect.bisect_left(up_ts, td) - bisect.bisect_right(up_ts, t)
                between_downs = bisect.bisect_left(down_ts, td) - bisect.bisect_right(down_ts, t)
                if not between_ups and not between_downs:
                    out.append(_Run(t, td, cfg.score_unusual, DETECTED, i, j))
    for i in mine_up:
        t = ups[i][0]
        found = None
        for m in model.on_s:
            tol = max(cfg.duration_tol_s, cfg.duration_tol * m)
            edge = find_edge(sig, t + m - tol, t + m + tol, -1.0, model.off_w, cfg)
            if edge is not None and (found is None or edge[1] > found[1]):
                found = edge
        if found is not None:
            out.append(_Run(t, found[0], cfg.score_found, COMPLETED, up=i))
        else:
            out.append(_Run(t, t + model.run_s, cfg.score_placed, PLACED, up=i))
    for j in mine_down:
        t = downs[j][0]
        found = None
        for m in model.on_s:
            tol = max(cfg.duration_tol_s, cfg.duration_tol * m)
            edge = find_edge(sig, t - m - tol, t - m + tol, 1.0, model.on_w, cfg)
            if edge is not None and (found is None or edge[1] > found[1]):
                found = edge
        if found is not None:
            out.append(_Run(found[0], t, cfg.score_found, COMPLETED, down=j))
        else:
            out.append(_Run(t - model.run_s, t, cfg.score_placed, PLACED, down=j))
    out.sort(key=lambda r: (r.start, r.end))
    return out


def _missing(model: Model, gap: float) -> int:
    """The runs the model expects within a pause of ``gap`` seconds."""
    return max(0, round((gap - model.pause_s) / model.period_s))


def _transition(model: Model, gap: float, cfg: ColdConfig) -> float | None:
    """What it costs to go from one run to the next one ``gap`` seconds
    later; None when the machine cannot (no pause that short)."""
    pause = model.pause_s
    if gap < pause * (1 - 2 * cfg.pause_tol):
        return None
    n = _missing(model, gap)
    if n == 0:
        off = abs(gap - pause) / (cfg.pause_tol * pause)
        return 0.25 * min(off, 2.0) ** 2 if off <= 2.0 else cfg.cost_break
    return cfg.cost_fill * n if n <= cfg.max_fill else cfg.cost_break


def _best_path(model: Model, runs: Sequence[_Run], cfg: ColdConfig) -> list[_Run]:
    """The chain of runs with the most evidence the machine can produce."""
    best = [0.0] * len(runs)
    prev: list[int | None] = [None] * len(runs)
    for j, r in enumerate(runs):
        best[j] = r.score
        for i in range(j):
            q = runs[i]
            if q.end > r.start or best[i] <= 0:
                continue
            if (q.up is not None and q.up == r.up) or (q.down is not None and q.down == r.down):
                continue
            cost = _transition(model, r.start - q.end, cfg)
            if cost is not None and best[i] - cost + r.score > best[j]:
                best[j] = best[i] - cost + r.score
                prev[j] = i
    if not runs:
        return []
    j: int | None = max(range(len(runs)), key=lambda k: best[k])
    path = []
    while j is not None:
        path.append(runs[j])
        j = prev[j]
    path.reverse()
    return path


def _fill(
    model: Model, a: float, b: float, n: int, sig: Signal, cfg: ColdConfig
) -> list[tuple[float, float, str]]:
    """``n`` runs between ``a`` and ``b``, evenly; each one moved to its
    edges when the curve shows both where it is due."""
    length = model.run_s  # the mean run: what an unseen run uses on average
    pause = (b - a - n * length) / (n + 1)
    out = []
    at = a
    for _ in range(n):
        start = at + pause
        end = start + length
        slack = max(pause, 0.0) / 2
        up = find_edge(sig, start - slack, start + slack, 1.0, model.on_w, cfg)
        if up is not None:
            for m in model.on_s:
                tol = max(cfg.duration_tol_s, cfg.duration_tol * m)
                down = find_edge(sig, up[0] + m - tol, up[0] + m + tol, -1.0, model.off_w, cfg)
                if down is not None and down[0] < b:
                    start, end = up[0], down[0]
                    break
        out.append((start, end, FILLED))
        at = end
    return out


def follow(
    model: Model,
    steps: Sequence[Step],
    points: Sequence[tuple[float, float]],
    start: float,
    end: float,
    cfg: ColdConfig = COLD,
    taken: tuple[set, set] | None = None,
) -> Track:
    """``model`` through the low steps of ``steps``: its runs, and where it
    could not be followed within ``[start, end)``, the estimate. ``taken``:
    the indices of the on- and off-steps (in time order, among the low
    steps nothing explained) other appliances used; this one's are added."""
    ups, downs = _edges(steps)
    taken = taken if taken is not None else (set(), set())
    sig = Signal(points)
    path = _best_path(model, _candidates(model, ups, downs, taken, sig, cfg), cfg)
    track = Track(model)
    mean_w = model.power_w * model.duty

    def estimate(a: float, b: float, energy_ws: float) -> None:
        a, b = max(a, start), min(b, end)
        if b > a and energy_ws > 0:
            track.estimated.append((a, b, energy_ws / (b - a)))

    last: _Run | None = None
    for r in path:
        if r.up is not None:
            taken[0].add(r.up)
        if r.down is not None:
            taken[1].add(r.down)
        if last is not None:
            gap = r.start - last.end
            n = _missing(model, gap)
            if 0 < n <= cfg.max_fill:
                track.cycles += _fill(model, last.end, r.start, n, sig, cfg)
            elif n > cfg.max_fill:
                a, b = max(last.end, start), min(r.start, end)
                if b > a:  # the runs the model expects in the part of the gap that counts
                    estimate(
                        a,
                        b,
                        model.power_w * model.run_s * (gap - model.pause_s) / model.period_s * (b - a) / gap,
                    )
        track.cycles.append((r.start, r.end, r.how))
        last = r
    # before the first run and after the last one
    first_at = path[0].start if path else end
    last_at = path[-1].end if path else start
    if not path:
        estimate(start, end, mean_w * (end - start))
    else:
        if first_at - start >= model.period_s:
            estimate(start, first_at, mean_w * (first_at - start))
        if end - last_at >= model.period_s:
            estimate(last_at, end, mean_w * (end - last_at))
    return track


def detect(
    steps: Sequence[Step],
    points: Sequence[tuple[float, float]],
    quiet: Callable[[float, float], bool],
    start: float,
    end: float,
    known: Sequence[Mapping] = (),
    cfg: ColdConfig = COLD,
) -> list[Track]:
    """The cold appliances of one analysis, the most energy first.

    ``steps``: every step (the low ones nothing explained are read);
    ``points``: the curve the detector read; ``quiet(start, end)``: a run
    there can teach (:class:`Quiet`); ``known``: the models of the registry
    (dicts with an ``id``; ``ignored``: the user's decision). A model the
    quiet stretches teach again takes the id of the known one it is
    (:func:`alike`) and today's values, unless its period is not the
    stored one (``model_tol``: a night that showed only its long runs); a
    known one they do not show is followed with its stored values. Each
    is reported when the curve shows at least ``min_evidence`` of its runs,
    and a stored one when ``min_seen`` of the runs its period expects are
    detected: an appliance that is gone is not estimated."""
    stored = [m for m in (Model.from_dict(k) for k in known) if m is not None]
    models = []
    for m in learn(steps, points, quiet, cfg):
        near = [(d, k) for k in stored if (d := alike(m.to_dict(), k.to_dict(), cfg)) is not None]
        if near:
            match = min(near, key=lambda x: x[0])[1]
            if abs(m.period_s - match.period_s) > cfg.model_tol * match.period_s:
                continue  # another rhythm than the one learned over the days: the stored model stays
            m.id, m.ignored = match.id, match.ignored
            stored.remove(match)
        models.append(m)
    taken: tuple[set, set] = (set(), set())
    tracks = []
    for m in [*models, *stored]:
        track = follow(m, steps, points, start, end, cfg, taken)
        if m.ignored or track.evidence(start, end) < cfg.min_evidence:
            continue
        if (
            not m.cycles
            and track.evidence(start, end, (DETECTED,)) < cfg.min_seen * (end - start) / m.period_s
        ):
            continue
        tracks.append(track)
    tracks.sort(key=lambda t: -t.overlap_energy_wh(start, end))
    used: dict[str, int] = {}
    for t in tracks:
        sid = daily_id(t.model.power_w)
        n = used.get(sid, 0)
        used[sid] = n + 1
        t.signature = sid if not n else f"{sid}-{n + 1}"
    return tracks


def daily_id(power_w: float) -> str:
    """``cold-80w``: the power rounded to 10 W."""
    return f"cold-{int(round(power_w / 10) * 10)}w"


def track_dict(track: Track, start: float, end: float) -> dict:
    """One cold appliance in the daily report (``cold``)."""
    m = track.model
    cycles = [c for c in track.cycles if c[1] > start and c[0] < end]
    estimated_kwh = track.overlap_energy_wh(start, end, estimated=True) / 1000
    out = {
        "id": track.signature,
        **m.to_dict(),
        # the runs of the quiet stretches this day's model comes from (0: the stored model)
        "learned_cycles": m.cycles,
        "cycles": [{"start": round(a, 1), "end": round(b, 1), "how": how} for a, b, how in cycles],
        "counts": {how: sum(1 for c in cycles if c[2] == how) for how in HOWS},
        # where it was not followed: an estimate from the duty, not a detection
        "estimated": [
            {"start": round(a, 1), "end": round(b, 1), "power_w": round(w, 1)} for a, b, w in track.estimated
        ],
        "energy_kwh": round(track.overlap_energy_wh(start, end) / 1000, 3),
        "estimated_kwh": round(estimated_kwh, 3),
    }
    if m.id:
        out["model"] = m.id
    return out
