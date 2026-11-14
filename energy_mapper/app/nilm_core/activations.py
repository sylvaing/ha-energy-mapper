"""Turn unexplained steps into activations (one use of one appliance).

Two kinds:

* **cycling sessions**: a load of similar size switching on and off every few
  tens of seconds (induction hob at medium power, oven thermostat). Pairing
  these steps one by one would create dozens of tiny "appliances", so they
  are grouped first into one session with a duty cycle.
* **simple activations**: an on-step paired with the off-step of the same
  size that closes it.
"""

from __future__ import annotations

import bisect
import statistics
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

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    @property
    def mean_power_w(self) -> float:
        return self.power_w * self.duty

    @property
    def energy_kwh(self) -> float:
        return self.mean_power_w * self.duration_s / 3.6e6

    def power_at(self, ts: float) -> float:
        """Instant power. For a session, the exact on/off pattern is known."""
        if not self.start <= ts < self.end:
            return 0.0
        if not self.cycling:
            return self.power_w
        state = 0.0
        for s in self.steps:
            if s.ts > ts:
                break
            state = self.power_w if s.delta > 0 else 0.0
        return state

    def overlap_energy_wh(self, start: float, end: float) -> float:
        a, b = max(start, self.start), min(end, self.end)
        if b <= a:
            return 0.0
        return self.mean_power_w * (b - a) / 3600.0


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
    ons, periods = [], []
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
