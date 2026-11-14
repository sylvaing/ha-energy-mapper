"""Known switched loads: appliances driven by an HA entity without a power sensor.

Example: an electric water heater on a relay (``switch.water_heater``). The
entity state tells *when* it may run; the steps in the house load tell *if*
it actually drew power (a hot tank's thermostat may be open) and *how much*.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .steps import Step

UNAVAILABLE = {"unavailable", "unknown", ""}


@dataclass
class SwitchedLoadConfig:
    entity_id: str
    name: str
    on_states: tuple[str, ...] = ("on",)
    # power used when no step could be measured (0 = learn only)
    power_w: float = 0.0


@dataclass
class KnownLoad:
    name: str
    entity_id: str
    power_w: float  # measured (median of matched steps) or configured
    intervals: list[tuple[float, float]] = field(default_factory=list)
    transitions: int = 0  # entity on/off changes seen
    matched: int = 0  # changes that had a matching step in the load

    def active_at(self, ts: float) -> bool:
        return any(a <= ts < b for a, b in self.intervals)

    def overlap(self, start: float, end: float) -> float:
        return sum(max(0.0, min(b, end) - max(a, start)) for a, b in self.intervals)


def state_intervals(
    states: Iterable[tuple[float, str]], on_states: Sequence[str], until: float
) -> list[tuple[float, float]]:
    """Periods during which the entity was in one of ``on_states``.

    Unavailable states are skipped: they do not end an on-period (relays
    keep their state when the network drops).
    """
    out: list[tuple[float, float]] = []
    cur: float | None = None
    for ts, s in sorted(states, key=lambda x: x[0]):
        if s in UNAVAILABLE:
            continue
        on = s in on_states
        if on and cur is None:
            cur = ts
        elif not on and cur is not None:
            out.append((cur, ts))
            cur = None
    if cur is not None:
        out.append((cur, until))
    return out


def _near(steps: list[Step], step_ts: list[float], t: float, window: float) -> list[Step]:
    i = bisect.bisect_left(step_ts, t - window)
    j = bisect.bisect_right(step_ts, t + window)
    return steps[i:j]


def resolve_switched_load(
    cfg: SwitchedLoadConfig,
    states: Iterable[tuple[float, str]],
    steps: list[Step],
    until: float,
    threshold: float,
    window_s: float = 30.0,
) -> KnownLoad:
    """Learn the load's power from the steps at its transitions and keep the
    on-periods during which it actually drew power. Matched steps are marked
    as explained so they are not analysed again."""
    periods = state_intervals(states, cfg.on_states, until)
    step_ts = [s.ts for s in steps]

    # 1. candidate powers: the largest same-sign step near each transition
    ups: list[tuple[Step, float]] = []
    downs: list[tuple[Step, float]] = []
    for a, b in periods:
        near_a = [s for s in _near(steps, step_ts, a, window_s) if s.delta > 0]
        if near_a:
            best = max(near_a, key=lambda s: s.delta)
            ups.append((best, a))
        if b < until:
            near_b = [s for s in _near(steps, step_ts, b, window_s) if s.delta < 0]
            if near_b:
                best = min(near_b, key=lambda s: s.delta)
                downs.append((best, b))
    sizes = [s.delta for s, _ in ups] + [-s.delta for s, _ in downs]
    power = statistics.median(sizes) if sizes else cfg.power_w

    load = KnownLoad(cfg.name, cfg.entity_id, round(power, 1))
    load.transitions = len(periods) + sum(1 for _, b in periods if b < until)
    if power <= 0:
        return load

    # 2. keep steps whose size is consistent with the learned power
    tol = max(0.2 * power, threshold * 0.5)

    def consistent(step: Step) -> bool:
        return abs(abs(step.delta) - power) <= tol

    good_up = {id(s) for s, _ in ups if consistent(s)}
    good_down = {id(s) for s, _ in downs if consistent(s)}
    for s, _ in ups + downs:
        if id(s) in good_up or id(s) in good_down:
            s.explained_by = cfg.name
            load.matched += 1

    # 3. an on-period counts if it started or ended with a consistent step
    up_at = {a for s, a in ups if id(s) in good_up}
    down_at = {b for s, b in downs if id(s) in good_down}
    for a, b in periods:
        if a in up_at or b in down_at:
            load.intervals.append((a, b))
    return load
