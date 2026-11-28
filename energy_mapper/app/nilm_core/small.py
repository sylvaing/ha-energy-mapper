"""Long small uses (#110): about 100 W for hours (television, set-top box,
desktop computer).

Such an appliance makes two low steps of the noise-adaptive threshold (#88)
hours apart, and nothing in between. Pairing each off-step with the open
on-step closest in size is not enough at that power: the steps of a cold
appliance are the same size and come every few minutes, a big load or the
solar production hides one of the two steps by day, and two unrelated steps
hours apart are easily the same size. So a long small use is not accepted
on its two steps: it is accepted on the **level** of the curve.

**The level** (:class:`Level`) is the curve the detector read, minute by
minute (the median of each minute), minus what is already explained: the
uses of the fixed thresholds made of two steps, the switched loads, and
the runs of the cold appliances the curve showed (detected or completed,
:mod:`.cold`). Some minutes are *unknown*: around the steps of those uses
and loads (their edges are not known to the second), and during what
cannot be subtracted (a cycling session, a multi-phase cycle). The
**floor** of a stretch is a low quantile of its known minutes: what
the house draws there once the short-lived loads on top (a compressor
nothing follows, a lamp) are left out.

**A pair** of low steps (:func:`pair_long`) between ``min_s`` and ``max_s``
apart, of the same size, is a long small use when

* the floor of its first half hour is above the floor of the half hour
  before it by ``raised`` of the step, and the floor of its last half hour
  above the floor of the half hour after it as much: the level goes up at
  the on-step and stays up, comes down at the off-step and stays down (an
  on-step paired with the off-step of something else fails here: the level
  after that off-step is the level before it);
* in between, the level never stays under the higher of the two outer
  floors plus that much for ``hold`` minutes in a row: followed from
  either step, the raised level reaches the other one (two unrelated steps
  hours apart fail here: the level goes back down between them). Another
  load that starts during the use only raises the level; one that stops
  during it, by as much as the step, makes the use end there: the level
  cannot tell which of the two went off;
* enough of its minutes are known.

**One step hidden** (:func:`complete`). A low step no pair took is followed
on the level: from an on-step forwards (from an off-step backwards) while
the level stays raised above the floor on the other side of the step. Where
it has been back down for ``hold`` minutes, the use has ended, and the end
is the last minute it was seen raised: *where the raised level stops*. It
is kept when everything below holds, else nothing is reported (missing a
use is preferred to inventing one):

* it lasted at least ``min_s`` and at most ``max_s``;
* the stop is known within ``hidden_gap_s``: the last raised minute and the
  first minute back down are no further apart (in between, the minutes are
  unknown: the big load that hides the step);
* past the stop, the floor is back within ``back`` of the step of the floor
  before the use: the house returned to where it was, by about the step;
* enough of its minutes are known.

Each end of a use says how it was found, as the runs of the cold appliances
do: ``detected`` (a step) or ``level``; the use is ``detected`` (two steps)
or ``completed`` (one step and the level).

**Signatures.** A long small use is first only listed (``small_uses`` of
the report, ``long``). Those of the stored days that come back at the same
power on several days become a signature of kind ``small`` in the registry
(:mod:`.registry`), which gives it back to the daily analysis
(``AnalysisConfig.small_models``): from then on the uses of that power are
activations of that signature (``small`` in ``activations``), with their
energy in the curve, and can be named and made an appliance like the
others. A one-off stays in the list.

Pure and standard library only. Nightly only, behind the adaptive
threshold.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .activations import Activation
from .steps import Step

KIND = "small"
BAND = "small"
GROUP = (KIND, BAND)
DETECTED, COMPLETED, LEVEL = "detected", "completed", "level"


@dataclass(frozen=True)
class SmallConfig:
    min_s: float = 1800.0  # a long small use lasts at least this
    max_s: float = 8 * 3600.0  # ... and at most this
    # its two steps are the same size within this
    pair_tol: float = 0.25
    pair_tol_w: float = 20.0
    bucket_s: float = 60.0  # the level: one median per bucket
    ref_s: float = 1800.0  # the stretch a floor is measured on, outside and inside a use
    guard_s: float = 60.0  # left out on each side of a step: the detector's windows
    floor_q: float = 0.2  # the floor of a stretch: this quantile of its known minutes
    raised: float = 0.6  # the floor is raised by this share of the step, at least
    back: float = 0.4  # one step hidden: past the stop, the floor is back within this share
    # the raised level has stopped once it is back down for this many known minutes in a row
    hold: int = 3
    hidden_gap_s: float = 900.0  # ... the stop being known within this
    min_ref: int = 5  # known minutes a floor needs
    min_cover: float = 0.5  # share of the minutes of a use that must be known
    mask_margin_s: float = 60.0  # unknown around a step of the fixed thresholds or of a switched load
    # the steps of these runs of the cold appliances are theirs: not paired here
    cold_steps: tuple[str, ...] = (DETECTED, COMPLETED)
    # two long small uses are the same one: this share of each in common, powers within
    same_overlap: float = 0.5
    same_power: float = 0.3
    # a use is a known signature's (``small_models``): its power within this of the signature's
    model_tol: float = 0.12

    def same_size(self, on_w: float, off_w: float) -> bool:
        return abs(on_w - off_w) <= max(self.pair_tol_w, self.pair_tol * on_w)


SMALL = SmallConfig()


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


class Level:
    """The level of the curve, one value per bucket: the median of the
    readings of the bucket, minus ``minus(ts)`` (what is already explained
    and can be subtracted); None in the buckets without a reading and in
    those ``masks`` (``(start, end)`` spans) touch."""

    def __init__(
        self,
        points: Sequence[tuple[float, float]],
        masks: Iterable[tuple[float, float]] = (),
        minus=None,
        bucket_s: float = 60.0,
    ):
        self.bucket_s = bucket_s
        self.t0 = points[0][0] if points else 0.0
        n = int((points[-1][0] - self.t0) // bucket_s) + 1 if points else 0
        groups: list[list[float]] = [[] for _ in range(n)]
        for t, v in points:
            groups[int((t - self.t0) // bucket_s)].append(v - (minus(t) if minus else 0.0))
        self.values: list[float | None] = [statistics.median(g) if g else None for g in groups]
        for a, b in masks:
            for k in range(max(self.index(a), 0), min(self.index(b), n - 1) + 1):
                self.values[k] = None

    def __len__(self) -> int:
        return len(self.values)

    def index(self, ts: float) -> int:
        return int((ts - self.t0) // self.bucket_s)

    def start(self, k: int) -> float:
        return self.t0 + k * self.bucket_s

    def known(self, start: float, end: float) -> list[float]:
        """The known values of the buckets wholly within ``[start, end]``."""
        i = max(-int(-(start - self.t0) // self.bucket_s), 0)  # ceil
        j = min(int((end - self.t0) // self.bucket_s), len(self.values))
        return [v for v in self.values[i:j] if v is not None]

    def buckets(self, start: float, end: float) -> int:
        """How many buckets lie wholly within ``[start, end]``."""
        i = max(-int(-(start - self.t0) // self.bucket_s), 0)
        j = min(int((end - self.t0) // self.bucket_s), len(self.values))
        return max(j - i, 0)

    def floor(self, start: float, end: float, q: float, min_known: int) -> float | None:
        values = self.known(start, end)
        return _quantile(values, q) if len(values) >= min_known else None


def _steady(a: Activation) -> bool:
    """A use of the fixed thresholds that is one on-step and one off-step:
    its power can be taken out of the level."""
    return not a.cycling and not getattr(a, "multiphase", False)


def masks_of(acts: Iterable[Activation], known: Iterable, margin_s: float) -> list[tuple[float, float]]:
    """When the level is unknown: around the two steps of a use of the
    fixed thresholds and around the transitions of a switched load (their
    power in between is subtracted, :func:`explained_power`); during a
    cycling session, each burst of a burst series and a multi-phase cycle,
    whose power at a given minute is not known well enough; while a load
    measured by its runs is on."""
    out: list[tuple[float, float]] = []

    def edges(a: float, b: float) -> None:
        out.extend([(a - margin_s, a + margin_s), (b - margin_s, b + margin_s)])

    for a in acts:
        if _steady(a):
            edges(a.start, a.end)
        elif a.cycling and a.blocks:
            out += [(s - margin_s, e + margin_s) for s, e in zip(*a.on_periods(), strict=True)]
        else:
            out.append((a.start - margin_s, a.end + margin_s))
    for k in known:
        if getattr(k, "profile", None):
            out += [(s - margin_s, e + margin_s) for s, e in k.intervals]
        elif getattr(k, "power_w", 0) > 0:
            for s, e in k.intervals:
                edges(s, e)
    return out


def explained_power(
    acts: Iterable[Activation], known: Iterable, tracks: Iterable
) -> list[tuple[float, float, float]]:
    """``(start, end, power)`` of what is taken out of the level.

    * The uses of the fixed thresholds made of two steps, each at the size
      of its on-step: the level is then continuous where the use starts,
      and where it stops it moves by what else changed during the use (an
      on-step of 2.1 kW closed by an off-step of 2 kW leaves the level
      100 W higher: something of 100 W started behind it).
    * The switched loads at their measured power.
    * The runs of the cold appliances the curve showed (detected or
      completed). A run that was placed or filled is where the model
      expects it, not where the curve has it: subtracting it would dig a
      hole in the level.
    """
    out = []
    for a in acts:
        if _steady(a):
            on = a.steps[0].delta if a.steps and a.steps[0].delta > 0 else a.power_w
            out.append((a.start, a.end, on))
    for k in known:
        if not getattr(k, "profile", None) and getattr(k, "power_w", 0) > 0:
            out += [(s, e, k.power_w) for s, e in k.intervals]
    for t in tracks:
        out += [(a, b, t.model.power_w) for a, b, how in t.cycles if how in (DETECTED, COMPLETED)]
    out.sort()
    return out


class _Runs:
    """The summed power of ``(start, end, power)`` runs at a time."""

    def __init__(self, runs: Sequence[tuple[float, float, float]]):
        self.runs = sorted(runs)
        self.starts = [r[0] for r in self.runs]
        self.longest = max((b - a for a, b, _ in self.runs), default=0.0)

    def __call__(self, ts: float) -> float:
        i = bisect.bisect_right(self.starts, ts)
        j = bisect.bisect_left(self.starts, ts - self.longest)
        return sum(w for a, b, w in self.runs[j:i] if a <= ts < b)


def level_of(
    points: Sequence[tuple[float, float]],
    acts: Iterable[Activation] = (),
    known: Iterable = (),
    tracks: Iterable = (),
    cfg: SmallConfig = SMALL,
) -> Level:
    """The level the long small uses are judged on (see the module)."""
    acts = list(acts)
    known = list(known)
    runs = _Runs(explained_power(acts, known, tracks))
    return Level(points, masks_of(acts, known, cfg.mask_margin_s), runs if runs.runs else None, cfg.bucket_s)


# ---------------------------------------------------------------------------
# two steps
# ---------------------------------------------------------------------------
def _stays_up(level: Level, start: float, end: float, bar: float, hold: int) -> bool:
    """Whether the level never stays under ``bar`` for ``hold`` known
    buckets in a row within ``[start, end]``."""
    down = 0
    for v in level.values[max(level.index(start) + 1, 0) : max(level.index(end), 0)]:
        if v is None:
            continue
        down = down + 1 if v < bar else 0
        if down >= hold:
            return False
    return True


def pair_ok(level: Level, start: float, end: float, power_w: float, cfg: SmallConfig = SMALL) -> bool:
    """Whether the level supports a use of ``power_w`` from ``start`` to
    ``end`` (see the module: raised at both ends, and never back down in
    between)."""
    g = cfg.guard_s
    inside = level.known(start + g, end - g)
    total = level.buckets(start + g, end - g)
    if total <= 0 or len(inside) < max(cfg.min_ref, cfg.min_cover * total):
        return False
    before = level.floor(start - g - cfg.ref_s, start - g, cfg.floor_q, cfg.min_ref)
    after = level.floor(end + g, end + g + cfg.ref_s, cfg.floor_q, cfg.min_ref)
    if before is None or after is None:
        return False
    span = min(cfg.ref_s, (end - start) / 2)
    head = level.floor(start + g, start + g + span, cfg.floor_q, cfg.min_ref)
    tail = level.floor(end - g - span, end - g, cfg.floor_q, cfg.min_ref)
    if head is None or tail is None:
        return False
    need = cfg.raised * power_w
    return (
        head - before >= need
        and tail - after >= need
        # followed from each of its steps, the raised level reaches the other one
        and _stays_up(level, start + g, end - g, max(before, after) + need, cfg.hold)
    )


def _use(start: float, end: float, power_w: float, steps: list[Step], on: str, off: str) -> Activation:
    return Activation(
        start=start, end=end, power_w=round(power_w, 1), steps=steps, small=True, ends=(on, off)
    )


def _same(a: Activation, b: Activation, cfg: SmallConfig) -> bool:
    """The same raised level seen twice: each covers ``same_overlap`` of
    the other, at about the same power (a shorter use during a longer one
    is another use)."""
    shared = min(a.end, b.end) - max(a.start, b.start)
    return shared >= cfg.same_overlap * max(a.duration_s, b.duration_s) and abs(
        a.power_w - b.power_w
    ) <= cfg.same_power * max(a.power_w, b.power_w)


def pair_long(steps: Sequence[Step], level: Level, cfg: SmallConfig = SMALL) -> list[Activation]:
    """The long small uses two of ``steps`` make (free low steps, in time
    order): every pair of an on-step and a later off-step of the same size,
    ``min_s`` to ``max_s`` apart, that the level supports. The shortest
    first: where two share a step, or are the same raised level seen twice
    (the steps of a compressor nothing follows, just before the on-step
    and just after the off-step, are a pair too), the shorter one is the
    one that says the least beyond what the level shows. Their steps are
    ``used``."""
    ups = [s for s in steps if s.delta > 0]
    downs = [s for s in steps if s.delta < 0]
    down_ts = [s.ts for s in downs]
    found: list[tuple[float, float, Step, Step]] = []
    for u in ups:
        a = bisect.bisect_left(down_ts, u.ts + cfg.min_s)
        b = bisect.bisect_right(down_ts, u.ts + cfg.max_s)
        for d in downs[a:b]:
            if not cfg.same_size(u.delta, -d.delta):
                continue
            power = (u.delta - d.delta) / 2
            if pair_ok(level, u.ts, d.ts, power, cfg):
                found.append((d.ts - u.ts, abs(u.delta + d.delta) / power, u, d))
    out: list[Activation] = []
    for _, _, u, d in sorted(found, key=lambda f: (f[0], f[1])):
        if u.used or d.used:
            continue
        use = _use(u.ts, d.ts, (u.delta - d.delta) / 2, [u, d], DETECTED, DETECTED)
        if any(_same(use, other, cfg) for other in out):
            continue
        u.used = d.used = True
        out.append(use)
    out.sort(key=lambda a: a.start)
    return out


# ---------------------------------------------------------------------------
# one step
# ---------------------------------------------------------------------------
def find_stop(
    level: Level, ts: float, power_w: float, forward: bool, cfg: SmallConfig = SMALL
) -> float | None:
    """Where the level raised by a step of ``power_w`` at ``ts`` stops:
    walking forwards from an on-step (backwards from an off-step), the last
    minute it is seen raised before it stays back down. None when the use
    cannot be kept (see the module)."""
    g = cfg.guard_s
    if forward:
        near = level.floor(ts - g - cfg.ref_s, ts - g, cfg.floor_q, cfg.min_ref)
    else:
        near = level.floor(ts + g, ts + g + cfg.ref_s, cfg.floor_q, cfg.min_ref)
    if near is None:
        return None
    bar = near + cfg.raised * power_w
    step = 1 if forward else -1
    k = level.index(ts + g) + 1 if forward else level.index(ts - g) - 1
    last = level.index(ts + (cfg.max_s if forward else -cfg.max_s))
    raised_at: int | None = None  # the last bucket seen raised
    down: list[int] = []  # the buckets back down since
    known = 0
    while 0 <= k < len(level) and (k <= last if forward else k >= last):
        v = level.values[k]
        if v is not None:
            if v >= bar:
                raised_at, down = k, []
                known += 1
            else:
                down.append(k)
                if len(down) >= cfg.hold:
                    break
        k += step
    else:
        return None  # still raised at the end of the curve, or for too long: not a use that ended
    if raised_at is None:
        return None
    # the edge of the last raised bucket that faces the stop
    stop = level.start(raised_at + 1) if forward else level.start(raised_at)
    first_down = level.start(down[0]) if forward else level.start(down[0] + 1)
    if abs(first_down - stop) > cfg.hidden_gap_s:
        return None
    duration = abs(stop - ts)
    if not cfg.min_s <= duration <= cfg.max_s:
        return None
    total = abs(raised_at - level.index(ts)) or 1
    if known < max(cfg.min_ref, cfg.min_cover * total):
        return None
    if forward:
        far = level.floor(first_down, first_down + cfg.ref_s, cfg.floor_q, cfg.min_ref)
    else:
        far = level.floor(first_down - cfg.ref_s, first_down, cfg.floor_q, cfg.min_ref)
    if far is None or abs(far - near) > cfg.back * power_w:
        return None
    return stop


def complete(
    steps: Sequence[Step], level: Level, others: Sequence[Activation] = (), cfg: SmallConfig = SMALL
) -> list[Activation]:
    """The long small uses one of ``steps`` (free low steps) and the level
    make, the longest first; not those that are one of ``others`` (the
    pairs) or of the ones already kept. Their step is ``used``."""
    found = []
    for s in steps:
        if s.used:
            continue
        size = abs(s.delta)
        stop = find_stop(level, s.ts, size, s.delta > 0, cfg)
        if stop is None:
            continue
        if s.delta > 0:
            found.append(_use(s.ts, stop, size, [s], DETECTED, LEVEL))
        else:
            found.append(_use(stop, s.ts, size, [s], LEVEL, DETECTED))
    out: list[Activation] = []
    for a in sorted(found, key=lambda a: -a.duration_s):
        if any(_same(a, b, cfg) for b in [*others, *out]):
            continue
        a.steps[0].used = True
        out.append(a)
    out.sort(key=lambda a: a.start)
    return out


def long_uses(
    steps: Sequence[Step],
    points: Sequence[tuple[float, float]],
    acts: Iterable[Activation] = (),
    known: Iterable = (),
    tracks: Iterable = (),
    cfg: SmallConfig = SMALL,
) -> tuple[list[Activation], Level]:
    """``(uses, level)``: the long small uses among ``steps`` (the free low
    steps, in time order), by start, and the level they were judged on."""
    level = level_of(points, acts, known, tracks, cfg)
    pairs = pair_long(steps, level, cfg)
    single = complete(steps, level, pairs, cfg)
    return sorted([*pairs, *single], key=lambda a: a.start), level


def how(a: Activation) -> str:
    """``detected`` (two steps) or ``completed`` (one step and the level)."""
    return DETECTED if a.ends == (DETECTED, DETECTED) else COMPLETED


# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------
def daily_id(power_w: float) -> str:
    """``small-90w``: the power rounded to 10 W."""
    return f"small-{int(round(power_w / 10) * 10)}w"


def model_of(power_w: float, models: Sequence[Mapping], cfg: SmallConfig = SMALL) -> Mapping | None:
    """The known signature (``small_models``) a use of ``power_w`` is of:
    the nearest within ``model_tol`` of its own power, or None."""
    best = None
    for m in models:
        try:
            ref = float(m["power_w"])
        except (KeyError, TypeError, ValueError):
            continue
        near = ref > 0 and abs(power_w - ref) <= cfg.model_tol * ref
        if near and (best is None or abs(power_w - ref) < abs(power_w - float(best["power_w"]))):
            best = m
    return best
