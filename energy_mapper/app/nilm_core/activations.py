"""Turn unexplained steps into activations (one use of one appliance).

Three kinds:

* **cycling sessions**: a load of similar size switching on and off every few
  tens of seconds (induction hob at medium power, oven thermostat). Pairing
  these steps one by one would create dozens of tiny "appliances", so they
  are grouped first into one session with a duty cycle.
* **simple activations**: an on-step paired with the off-step of the same
  size that closes it.
* **burst series** (#87): a heater regulating on its thermostat switches on
  for some tens of seconds every few minutes, for hours. Its steps are too
  far apart for a session, so each burst is first a simple activation (or a
  short session); the regular ones of the same power are then grouped into
  one cycling use (:func:`group_bursts`).
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from .steps import Step


@dataclass
class Activation:
    start: float
    end: float
    power_w: float
    cycling: bool = False
    duty: float = 1.0  # fraction of the time the load is on (cycling only)
    cycles: int = 0
    on_s: float | None = None  # median on-time of one cycle
    period_s: float | None = None  # median cycle period
    signature: str | None = None
    steps: list[Step] = field(default_factory=list, repr=False)
    # a burst series (#87): its blocks ``(start, end)``, the bursts closer
    # than the session gap counted as one
    blocks: list[tuple[float, float]] | None = None
    # a long small use (#110, :mod:`.small`): under the fixed thresholds,
    # accepted on the level of the curve; ``ends``: how its start and its
    # end were found (``detected``: a step, ``level``: where the level stops)
    small: bool = False
    ends: tuple[str, str] | None = None
    _on: tuple[list[float], list[float]] | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    @property
    def mean_power_w(self) -> float:
        return self.power_w * self.duty

    @property
    def energy_kwh(self) -> float:
        return self.mean_power_w * self.duration_s / 3.6e6

    def on_periods(self) -> tuple[list[float], list[float]]:
        """The starts and the ends of the on-periods of a cycling use: each
        on-step followed by an off-step."""
        if self._on is None:
            pairs = [(a.ts, b.ts) for a, b in pairwise(self.steps) if a.delta > 0 and b.delta < 0]
            self._on = ([p[0] for p in pairs], [p[1] for p in pairs])
        return self._on

    def power_at(self, ts: float) -> float:
        """Instant power. For a cycling use, the exact on/off pattern is known."""
        if not self.start <= ts < self.end:
            return 0.0
        if not self.cycling:
            return self.power_w
        starts, ends = self.on_periods()
        i = bisect.bisect_right(starts, ts) - 1
        return self.power_w if i >= 0 and ts < ends[i] else 0.0

    def overlap_energy_wh(self, start: float, end: float) -> float:
        """Energy used within ``[start, end)``. A cycling use counts its own
        on-periods: a heater on for 40 s every 4 minutes uses nothing in
        the minutes between two bursts."""
        a, b = max(start, self.start), min(end, self.end)
        if b <= a:
            return 0.0
        if not self.cycling or not self.steps:
            return self.mean_power_w * (b - a) / 3600.0
        starts, ends = self.on_periods()
        on_s = 0.0
        i = max(bisect.bisect_right(starts, a) - 1, 0)
        while i < len(starts) and starts[i] < b:
            on_s += max(0.0, min(b, ends[i]) - max(a, starts[i]))
            i += 1
        return self.power_w * on_s / 3600.0


def find_cycling_sessions(
    steps: list[Step],
    min_w: float = 600.0,
    max_gap_s: float = 120.0,
    min_steps: int = 6,
    size_tol: float = 0.2,
) -> list[Activation]:
    """Group alternating up/down steps of similar size into sessions.

    When another appliance switches at the same moment as a cycle, the meter
    sees one combined step (e.g. +1.4 kW hob + 2.8 kW oven = +4.2 kW). Such a
    step is split: the cycle's share joins the session, and the remainder is
    added to ``steps`` as a new step, so the other appliance can be paired.
    """
    free = [s for s in steps if not s.explained_by and not s.used and abs(s.delta) >= min_w]
    sessions: list[Activation] = []
    residuals: list[Step] = []
    i = 0
    while i < len(free):
        seed = free[i]
        ref = abs(seed.delta)
        group = [seed]
        combined: list[tuple[Step, Step, Step]] = []  # (original, cycle part, rest)
        j = i + 1
        while j < len(free):
            s = free[j]
            if s.ts - group[-1].ts > max_gap_s:
                break
            alternates = (s.delta > 0) != (group[-1].delta > 0)
            size = abs(s.delta)
            if alternates and abs(size - ref) <= size_tol * ref:
                group.append(s)
                ref = statistics.median(abs(x.delta) for x in group)
            elif (
                alternates
                and size > ref * (1 + size_tol)
                and size - ref >= min_w * 0.5
                and not _clean_match_soon(free, j, ref, size_tol, s.delta > 0)
            ):
                sign = 1.0 if s.delta > 0 else -1.0
                part = Step(s.ts, sign * ref, s.before, s.before + sign * ref)
                rest = Step(s.ts, s.delta - sign * ref, s.before + sign * ref, s.after)
                group.append(part)
                combined.append((s, part, rest))
            j += 1
        if len(group) >= min_steps:
            sessions.append(_session(group))
            for s in group:
                s.used = True
            for original, _part, rest in combined:
                original.used = True
                residuals.append(rest)
            i = j
        else:
            i += 1
    if residuals:
        steps.extend(residuals)
        steps.sort(key=lambda s: s.ts)
    return sessions


def _clean_match_soon(
    free: list[Step], j: int, ref: float, tol: float, up: bool, within_s: float = 30.0
) -> bool:
    """True if a step of the cycle's own size and direction follows shortly:
    then the big step belongs to another appliance and must not be split."""
    t = free[j].ts
    for s in free[j + 1 :]:
        if s.ts - t > within_s:
            break
        if (s.delta > 0) == up and abs(abs(s.delta) - ref) <= tol * ref:
            return True
    return False


def _session(group: list[Step]) -> Activation:
    power = statistics.median(abs(s.delta) for s in group)
    ons = []
    for a, b in pairwise(group):
        if a.delta > 0 and b.delta < 0:
            ons.append(b.ts - a.ts)
    ups = [s.ts for s in group if s.delta > 0]
    periods = [b - a for a, b in pairwise(ups)]
    start = group[0].ts
    end = group[-1].ts
    on_total = sum(ons)
    duty = min(1.0, on_total / (end - start)) if end > start else 1.0
    return Activation(
        start=start,
        end=end,
        power_w=round(power, 1),
        cycling=True,
        duty=round(duty, 3),
        cycles=len(ups),
        on_s=round(statistics.median(ons), 1) if ons else None,
        period_s=round(statistics.median(periods), 1) if periods else None,
        steps=list(group),
    )


@dataclass(frozen=True)
class BurstConfig:
    """The burst series (#87): what a thermostat's bursts must look like to
    be one cycling use."""

    min_w: float = 600.0  # as the cycling sessions: a heating element
    # a burst is on for less than this; longer, it is a use of its own (and
    # may be a heating phase of a multi-phase cycle, cycles.py)
    max_on_s: float = 300.0
    block_gap_s: float = 120.0  # bursts closer than this (off to on) are one block
    max_period_s: float = 900.0  # from the start of a block to the next one
    min_blocks: int = 4
    size_tol: float = 0.2  # the cycling tolerance, on the median power of the series
    same_size: float = 0.05  # two series as close as this to a burst: the longest takes it
    period_tol: float = 0.25  # a period within this of the median of the last ones
    period_memory: int = 5  # ... that many: a thermostat's period drifts with the room
    # blocks a series of at least ``min_blocks`` may miss in a row (a burst
    # hidden by another appliance switching at the same time)
    max_missed: int = 1

    def is_burst(self, power_w: float, duration_s: float, cycling: bool = False) -> bool:
        """Whether an activation can be one burst of a series: a short use
        or a cycling session, of at least ``min_w``."""
        return power_w >= self.min_w and (cycling or duration_s < self.max_on_s)


BURSTS = BurstConfig()


class _Series:
    """A series being built: its blocks (lists of unit indexes)."""

    def __init__(self, blocks: list[list[int]], units: Sequence[tuple[float, float, float]]):
        self.blocks = blocks
        self.units = units

    def add(self, i: int, new_block: bool) -> None:
        if new_block:
            self.blocks.append([i])
        else:
            self.blocks[-1].append(i)

    @property
    def end(self) -> float:
        return self.units[self.blocks[-1][-1]][1]

    @property
    def last_start(self) -> float:
        return self.units[self.blocks[-1][0]][0]

    @property
    def power(self) -> float:
        return statistics.median(self.units[i][2] for b in self.blocks for i in b)

    def period(self, memory: int) -> float | None:
        starts = [self.units[b[0]][0] for b in self.blocks[-(memory + 1) :]]
        return statistics.median(b - a for a, b in pairwise(starts)) if len(starts) > 1 else None

    def follows(self, start: float, cfg: BurstConfig) -> bool:
        """Whether a block starting at ``start`` keeps the rhythm."""
        period = start - self.last_start
        usual = self.period(cfg.period_memory)
        if usual is None:
            return period <= cfg.max_period_s
        # an established series may have missed a block or so
        most = 1 + (cfg.max_missed if len(self.blocks) >= cfg.min_blocks else 0)
        return any(
            abs(period - n * usual) <= cfg.period_tol * n * usual and period <= n * cfg.max_period_s
            for n in range(1, most + 1)
        )

    def over(self, start: float, cfg: BurstConfig) -> bool:
        """Whether a unit starting at ``start`` comes too late to continue it."""
        most = 1 + (cfg.max_missed if len(self.blocks) >= cfg.min_blocks else 0)
        return start - self.end > cfg.block_gap_s and start - self.last_start > most * cfg.max_period_s


def burst_series(
    units: Sequence[tuple[float, float, float]], config: BurstConfig = BURSTS
) -> list[list[list[int]]]:
    """The series of regular bursts among ``units``, the ``(start, end,
    power_w)`` of the bursts (:meth:`BurstConfig.is_burst`) sorted by start.
    Each series is the list of its blocks, each block the indexes of its
    units.

    Units of the same power (``size_tol`` of the median of the series)
    closer than ``block_gap_s`` form a **block** (a radiator bursting every
    80 s while its relay allows it). A block follows the previous one when
    it starts at most ``max_period_s`` after its start and, from the third
    block, when this period is within ``period_tol`` of the median of the
    last ones. A series has at least ``min_blocks`` blocks: two kettles
    twenty minutes apart, or three uses of a microwave at any intervals,
    stay what they are. Once it has them, a block may come two periods
    after the previous one (``max_missed``: a burst was hidden by another
    appliance switching at the same time).

    One pass in time order, each decision made from the units before (so
    that the live view can follow the same rule): a unit goes to the series
    it continues (the closest in power, the longest of those within
    ``same_size``). One that breaks the rhythm of a series of fewer than
    ``min_blocks`` blocks restarts it from its last block; it leaves a
    longer one as it is, to be continued by a later unit.
    """
    cfg = config
    open_: list[_Series] = []
    done: list[_Series] = []

    def close(s: _Series) -> None:
        if len(s.blocks) >= cfg.min_blocks:
            done.append(s)

    for i, (start, _end, power) in enumerate(units):
        for s in list(open_):
            if s.over(start, cfg):
                open_.remove(s)
                close(s)
        fits: list[tuple[tuple, _Series, bool]] = []
        breaks: list[tuple[tuple, _Series]] = []
        for s in open_:
            ref = s.power
            off = abs(power - ref) / ref
            if start < s.end or off > cfg.size_tol:
                continue  # another load running at the same time, or of another size
            rank = (int(off / cfg.same_size), -len(s.blocks), off)
            if start - s.end <= cfg.block_gap_s:
                fits.append((rank, s, False))
            elif s.follows(start, cfg):
                fits.append((rank, s, True))
            else:
                breaks.append((rank, s))
        if fits:
            _, s, new_block = min(fits, key=lambda f: f[0])
            s.add(i, new_block)
            continue
        if breaks:
            _, s = min(breaks, key=lambda f: f[0])
            if len(s.blocks) < cfg.min_blocks and start - s.last_start <= cfg.max_period_s:
                s.blocks = [s.blocks[-1], [i]]
                continue
        open_.append(_Series([[i]], units))
    for s in open_:
        close(s)
    return sorted((s.blocks for s in done), key=lambda blocks: blocks[0][0])


def _series(members: list[Activation], blocks: list[tuple[float, float]]) -> Activation:
    """One cycling use from the bursts of a series, in time order. Its
    on-periods are those of its bursts, each taken on its own: a session may
    end on an on-step whose off-step was not seen, which must not last
    until the next burst."""
    on: list[tuple[float, float]] = []
    for a in members:
        on += zip(*a.on_periods(), strict=True) if a.cycling else [(a.start, a.end)]
    start, end = members[0].start, members[-1].end
    durations = [b - a for a, b in on]
    periods = [b[0] - a[0] for a, b in pairwise(on)]
    use = Activation(
        start=start,
        end=end,
        # the median over the bursts: a short session counts as one, as in the rule
        power_w=round(statistics.median(a.power_w for a in members), 1),
        cycling=True,
        duty=round(min(1.0, sum(durations) / (end - start)), 3) if end > start else 1.0,
        cycles=len(on),
        on_s=round(statistics.median(durations), 1) if durations else None,
        period_s=round(statistics.median(periods), 1) if periods else None,
        steps=[s for a in members for s in a.steps],
        blocks=blocks,
    )
    use._on = ([p[0] for p in on], [p[1] for p in on])
    return use


def group_bursts(
    sessions: list[Activation], pairs: list[Activation], config: BurstConfig = BURSTS
) -> tuple[list[Activation], list[Activation]]:
    """Group the regular bursts of a thermostat into one cycling use (#87).

    ``sessions`` and ``pairs`` are the activations of
    :func:`find_cycling_sessions` and :func:`pair_steps` (validated). The
    short ones of at least ``min_w`` and the sessions are the bursts; each
    series of :func:`burst_series` becomes one cycling activation with the
    steps of its bursts: its power is their median, its duty the share of
    the time it is on, and its ``blocks`` keep when it was. Returns the
    sessions (those left and the series, by start) and the pairs left.
    """
    bursts = sorted(
        (a for a in sessions + pairs if config.is_burst(a.power_w, a.duration_s, a.cycling)),
        key=lambda a: a.start,
    )
    series = burst_series([(a.start, a.end, a.power_w) for a in bursts], config)
    if not series:
        return sessions, pairs
    grouped: set[int] = set()
    out: list[Activation] = []
    for blocks in series:
        members = [bursts[i] for b in blocks for i in b]
        grouped.update(id(a) for a in members)
        out.append(_series(members, [(bursts[b[0]].start, bursts[b[-1]].end) for b in blocks]))
    out += [a for a in sessions if id(a) not in grouped]
    out.sort(key=lambda a: a.start)
    return out, [a for a in pairs if id(a) not in grouped]


def pair_steps(
    steps: list[Step],
    tol_frac: float = 0.25,
    tol_w: float = 60.0,
    max_on_s: float = 6 * 3600.0,
) -> list[Activation]:
    """Pair each off-step with the open on-step closest in size.

    Closest size wins over most recent: with several loads on at once, the
    off-step of a kettle must not close the water heater that started before
    it. Ties go to the most recent load.
    """
    open_: list[Step] = []
    out: list[Activation] = []
    for s in steps:
        if s.explained_by or s.used:
            continue
        open_ = [o for o in open_ if s.ts - o.ts <= max_on_s]
        if s.delta > 0:
            open_.append(s)
            continue
        size = -s.delta
        best, best_err = None, None
        for o in open_:
            tol = max(tol_w, tol_frac * o.delta)
            err = abs(o.delta - size)
            if err > tol:
                continue
            rel = err / tol
            if best is None or rel < best_err - 1e-9 or (abs(rel - best_err) <= 1e-9 and o.ts > best.ts):
                best, best_err = o, rel
        if best is None:
            continue
        open_.remove(best)
        best.used = s.used = True
        out.append(
            Activation(
                start=best.ts,
                end=s.ts,
                power_w=round((best.delta + size) / 2, 1),
                steps=[best, s],
            )
        )
    out.sort(key=lambda a: a.start)
    return out


def validate_activations(
    acts: list[Activation],
    points: list[tuple[float, float]],
    base_w: float,
    min_share: float = 0.6,
    low_percentile: float = 0.1,
) -> list[Activation]:
    """Drop pairings the load does not support.

    An appliance that really runs from ``start`` to ``end`` keeps the load at
    least ``base + power`` during that time. Steps from clouds (PV and grid
    meters are not sampled together) or from two unrelated appliances can be
    paired by mistake into a phantom load lasting hours: during it, the load
    falls back to the base. A low percentile of the load during the
    activation must stay above ``base + min_share * power``.
    """
    ts = [p[0] for p in points]
    out: list[Activation] = []
    for a in acts:
        margin = min(15.0, a.duration_s / 4)
        i = bisect.bisect_left(ts, a.start + margin)
        j = bisect.bisect_right(ts, a.end - margin)
        window = sorted(p[1] for p in points[i:j])
        if len(window) < 3:
            out.append(a)  # too short to judge
            continue
        low = window[int(low_percentile * (len(window) - 1))]
        if low >= base_w + min_share * a.power_w:
            out.append(a)
        else:
            for s in a.steps:
                s.used = False
    return out
