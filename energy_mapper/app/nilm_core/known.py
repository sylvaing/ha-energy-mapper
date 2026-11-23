"""Known switched loads: appliances driven by an HA entity without a power sensor.

Example: an electric water heater on a relay (``switch.water_heater``). The
entity state tells *when* it may run; the steps in the house load tell *if*
it actually drew power (a hot tank's thermostat may be open) and *how much*.

Two ways to measure it (#79):

- ``step`` (relays and every entity but a ``sensor``): the power is the step
  of the house load at the entity's transitions, as before.
- ``run`` (an operation-state ``sensor``, e.g. an ``enum`` with ``run`` /
  ``stop``): a connected appliance's state bounds its whole cycle, but the
  appliance may start with a gradual ramp (no step) or run phases of very
  different powers (a dishwasher heats for 20 minutes of a 3-hour cycle). Each
  run is measured from the residual load itself: what it draws above the
  baseline around the run (:func:`baseline_w`), minute by minute. A run that
  draws less than ``min_w`` on average did not use power (a delayed start).
  The load is *transparent* to the detection: it marks no step and claims no
  power from the multi-phase candidates, so the curve can still learn the
  appliance's own signature; in the breakdown it takes the room first.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .steps import Step

UNAVAILABLE = {"unavailable", "unknown", ""}
STEP, RUN = "step", "run"
Segment = tuple[float, float, float]  # [t0, t1) at a constant power (W)


@dataclass
class RunMeasureConfig:
    """Measurement of an operation-state load from its runs (``run`` mode)."""

    before_s: float = 1200.0  # baseline window before the start
    after_skip_s: float = 120.0  # a ramp down after the end is not baseline
    after_s: float = 1200.0  # baseline window after the end (and the skip)
    trim: float = 0.2  # the highest share of the baseline readings left out
    min_run_s: float = 60.0
    max_run_s: float = 8 * 3600.0  # longer runs are not measured (too much else runs)
    min_w: float = 50.0  # a run drawing less on average used no power
    max_gap_s: float = 600.0  # a shorter pause (another state) stays inside the run
    # the two sides of the baseline disagree by more than this: the lower one
    # (something ran next to the appliance on the other side)
    split_w: float = 150.0


@dataclass
class SwitchedLoadConfig:
    entity_id: str
    name: str
    on_states: tuple[str, ...] = ("on",)
    # power used when no step could be measured (0 = learn only)
    power_w: float = 0.0
    # also try the opposite states and keep the one the load agrees with
    # (pilot-wire radiators heat when their relay is OFF); step mode only
    auto_polarity: bool = True
    # ``step`` | ``run``; None: ``run`` for a ``sensor`` (an operation state),
    # ``step`` for everything else (relays)
    mode: str | None = None

    @property
    def measure(self) -> str:
        if self.mode in (STEP, RUN):
            return self.mode
        return RUN if self.entity_id.startswith("sensor.") else STEP


@dataclass
class KnownLoad:
    name: str
    entity_id: str
    power_w: float  # measured (median of matched steps) or configured
    intervals: list[tuple[float, float]] = field(default_factory=list)
    transitions: int = 0  # entity on/off changes seen
    matched: int = 0  # changes that had a matching step in the load
    on_states: tuple[str, ...] = ("on",)  # states in which it consumes
    inverted: bool = False  # True when found opposite to the configuration
    mode: str = STEP
    # run mode: what each run drew above its baseline, piecewise constant
    profile: list[Segment] = field(default_factory=list)

    @property
    def claims(self) -> bool:
        """Its power is taken out before the multi-phase candidates (step
        mode); a run-mode load is transparent to the detection."""
        return self.mode != RUN

    def active_at(self, ts: float) -> bool:
        return any(a <= ts < b for a, b in self.intervals)

    def overlap(self, start: float, end: float) -> float:
        return sum(max(0.0, min(b, end) - max(a, start)) for a, b in self.intervals)

    def energy_ws(self, start: float, end: float) -> float:
        """Energy (W.s) attributed to the load within ``[start, end)``."""
        if self.mode != RUN:
            return self.power_w * self.overlap(start, end)
        return sum(w * max(0.0, min(b, end) - max(a, start)) for a, b, w in self.profile)

    def power_at(self, ts: float) -> float:
        """Power attributed to the load at ``ts`` (0 when off)."""
        if self.mode != RUN:
            return self.power_w if self.active_at(ts) else 0.0
        i = bisect.bisect_right([a for a, _, _ in self.profile], ts) - 1
        if i >= 0 and self.profile[i][0] <= ts < self.profile[i][1]:
            return self.profile[i][2]
        return 0.0


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
    points: Sequence[tuple[float, float]] | None = None,
    run_config: RunMeasureConfig | None = None,
) -> KnownLoad:
    """Learn the load's power from the steps at its transitions and keep the
    on-periods during which it actually drew power. Matched steps are marked
    as explained so they are not analysed again.

    With ``auto_polarity``, the opposite states are tried too, and the
    polarity with more transitions confirmed by the load wins.

    An operation-state load (``cfg.measure == "run"``) is measured from the
    residual ``points`` over each run instead (:func:`resolve_state_load`)."""
    states = sorted(states, key=lambda x: x[0])
    if cfg.measure == RUN and points is not None:
        return resolve_state_load(cfg, states, points, until, run_config)
    load, marked = _either_polarity(cfg, states, steps, until, threshold, window_s)
    for s in marked:
        s.explained_by = cfg.name
    return load


def _either_polarity(cfg, states, steps, until, threshold, window_s):
    """:func:`_resolve` with the configured states, or with the opposite
    ones (``auto_polarity``) when more transitions agree with the load."""
    load, marked = _resolve(cfg, cfg.on_states, states, steps, until, threshold, window_s)
    if cfg.auto_polarity:
        seen = {s for _, s in states if s not in UNAVAILABLE}
        opposite = tuple(sorted(seen - set(cfg.on_states)))
        if opposite:
            alt, alt_marked = _resolve(cfg, opposite, states, steps, until, threshold, window_s)
            if alt.matched > load.matched:
                alt.inverted = True
                load, marked = alt, alt_marked
    return load, marked


def explain_low_steps(
    cfg: SwitchedLoadConfig,
    states: Iterable[tuple[float, str]],
    low: list[Step],
    until: float,
    load: KnownLoad | None,
    tol_w: float,
    window_s: float = 30.0,
) -> list[Step]:
    """The low steps of the adaptive threshold (#88) that a switched load's
    transitions make (#99), marked ``explained_by`` it and returned.

    ``low``: the low steps, in time order; ``load``: what
    :func:`resolve_switched_load` made of the steps of the fixed thresholds.
    The rule is the one of those steps (:func:`_resolve`): the largest free
    step of the right sign within ``window_s`` of each transition, kept when
    its size agrees with the load's power within 20 % or ``tol_w``.

    - A load the fixed thresholds measured (a power just above the step
      threshold: some of its steps fall under it) keeps its power and its
      polarity: only the low steps of that size are its own.
    - A load they never saw (a 100 W pump on a relay) has its power taken
      from the low steps themselves, the median of those at its transitions,
      in the polarity more of them agree with.

    The load itself is left as it is: its power, its on-periods and its
    energy are still those of the steps of the fixed thresholds. A
    run-mode load marks no step (it is transparent to the detection)."""
    if cfg.measure == RUN:
        return []
    states = sorted(states, key=lambda x: x[0])
    threshold = 2 * tol_w  # the tolerance of _resolve is half the threshold it is given
    if load is not None and load.matched and load.power_w > 0:
        _, marked = _resolve(cfg, load.on_states, states, low, until, threshold, window_s, load.power_w)
    else:
        _, marked = _either_polarity(cfg, states, low, until, threshold, window_s)
    for s in marked:
        s.explained_by = cfg.name
    return marked


def _resolve(cfg, on_states, states, steps, until, threshold, window_s, power_w=None):
    """``power_w``: the load's power when it is already known (else the
    median of the candidate steps, or the configured power)."""
    periods = state_intervals(states, on_states, until)
    step_ts = [s.ts for s in steps]
    free = lambda s: not s.explained_by  # noqa: E731 - steps of other known loads are taken

    # 1. candidate powers: the largest same-sign step near each transition
    ups: list[tuple[Step, float]] = []
    downs: list[tuple[Step, float]] = []
    for a, b in periods:
        near_a = [s for s in _near(steps, step_ts, a, window_s) if s.delta > 0 and free(s)]
        if near_a:
            ups.append((max(near_a, key=lambda s: s.delta), a))
        if b < until:
            near_b = [s for s in _near(steps, step_ts, b, window_s) if s.delta < 0 and free(s)]
            if near_b:
                downs.append((min(near_b, key=lambda s: s.delta), b))
    sizes = [s.delta for s, _ in ups] + [-s.delta for s, _ in downs]
    power = power_w if power_w is not None else statistics.median(sizes) if sizes else cfg.power_w

    load = KnownLoad(cfg.name, cfg.entity_id, round(power, 1), on_states=tuple(on_states))
    load.transitions = len(periods) + sum(1 for _, b in periods if b < until)
    if power <= 0:
        return load, []

    # 2. keep steps whose size is consistent with the learned power
    tol = max(0.2 * power, threshold * 0.5)
    good_up = {id(s) for s, _ in ups if abs(s.delta - power) <= tol}
    good_down = {id(s) for s, _ in downs if abs(-s.delta - power) <= tol}
    marked = [s for s, _ in ups + downs if id(s) in good_up or id(s) in good_down]
    load.matched = len(marked)

    # 3. an on-period counts if it started or ended with a consistent step
    up_at = {a for s, a in ups if id(s) in good_up}
    down_at = {b for s, b in downs if id(s) in good_down}
    for a, b in periods:
        if a in up_at or b in down_at:
            load.intervals.append((a, b))
    return load, marked


# ---------------------------------------------------------------------------
# run mode: an operation state bounds each run, the curve measures it
# ---------------------------------------------------------------------------
def _window(points: Sequence[tuple[float, float]], times: list[float], a: float, b: float) -> list[float]:
    i = bisect.bisect_left(times, a)
    j = bisect.bisect_left(times, b)
    return [v for _, v in points[i:j]]


def _trimmed_mean(values: Sequence[float], top: float) -> float | None:
    """The mean without the highest ``top`` share of the values (a kettle
    next to the run); a fridge cycling stays in, as it does during the run."""
    if not values:
        return None
    v = sorted(values)
    keep = v[: max(1, len(v) - int(top * len(v)))]
    return sum(keep) / len(keep)


def baseline_w(
    points: Sequence[tuple[float, float]],
    start: float,
    end: float,
    config: RunMeasureConfig | None = None,
    times: list[float] | None = None,
) -> float | None:
    """The load around a run, without it: the mean of the residual load in
    ``before_s`` before ``start`` and in ``after_s`` after ``end`` (once
    ``after_skip_s`` has passed: a ramp down), without its highest
    ``trim`` share (another appliance's short use). When the two sides
    disagree by more than ``split_w``, the lower one (something else ran on
    the other side); one side alone when the other has no reading.
    ``points`` sorted."""
    cfg = config or RunMeasureConfig()
    times = times if times is not None else [t for t, _ in points]
    before = _window(points, times, start - cfg.before_s, start)
    after = _window(points, times, end + cfg.after_skip_s, end + cfg.after_skip_s + cfg.after_s)
    b, a = _trimmed_mean(before, cfg.trim), _trimmed_mean(after, cfg.trim)
    if b is None or a is None:
        return b if a is None else a
    if abs(b - a) > cfg.split_w:
        return min(b, a)
    return _trimmed_mean(before + after, cfg.trim)


def excess(
    points: Sequence[tuple[float, float]],
    start: float,
    end: float,
    base: float,
    times: list[float] | None = None,
) -> list[Segment]:
    """What the load draws above ``base`` within ``[start, end)``, piecewise
    constant (each reading holds until the next one)."""
    times = times if times is not None else [t for t, _ in points]
    i = max(bisect.bisect_right(times, start) - 1, 0)
    out: list[Segment] = []
    for k in range(i, len(points)):
        t, v = points[k]
        if t >= end:
            break
        t1 = points[k + 1][0] if k + 1 < len(points) else end
        a, b = max(t, start), min(t1, end)
        if b > a:
            out.append((a, b, max(v - base, 0.0)))
    return out


def measure_run(
    points: Sequence[tuple[float, float]],
    start: float,
    end: float,
    config: RunMeasureConfig | None = None,
    times: list[float] | None = None,
) -> tuple[float | None, list[Segment]]:
    """``(baseline, excess segments)`` of one run (``(None, [])`` without
    readings around it)."""
    times = times if times is not None else [t for t, _ in points]
    base = baseline_w(points, start, end, config, times)
    if base is None:
        return None, []
    return base, excess(points, start, end, base, times)


def resolve_state_load(
    cfg: SwitchedLoadConfig,
    states: Sequence[tuple[float, str]],
    points: Sequence[tuple[float, float]],
    until: float,
    config: RunMeasureConfig | None = None,
) -> KnownLoad:
    """An operation-state load measured from its runs: each on-period
    (pauses shorter than ``max_gap_s`` inside it) of ``min_run_s`` to
    ``max_run_s`` is kept when it drew at least ``min_w``
    above its baseline on average, with its power minute by minute; the
    load's power is the median of the runs' mean powers. A ramping start or
    a cycle of several phases is measured as it is (no step needed)."""
    rc = config or RunMeasureConfig()
    points = sorted(points)
    times = [t for t, _ in points]
    periods: list[tuple[float, float]] = []
    for a, b in state_intervals(states, cfg.on_states, until):
        if periods and a - periods[-1][1] < rc.max_gap_s:
            periods[-1] = (periods[-1][0], b)  # a pause: still the same run
        else:
            periods.append((a, b))
    load = KnownLoad(cfg.name, cfg.entity_id, 0.0, on_states=tuple(cfg.on_states), mode=RUN)
    load.transitions = len(periods) + sum(1 for _, b in periods if b < until)
    means = []
    for a, b in periods:
        if not rc.min_run_s <= b - a <= rc.max_run_s:
            continue
        _, segs = measure_run(points, a, b, rc, times)
        if not segs:
            continue
        mean = sum(w * (y - x) for x, y, w in segs) / (b - a)
        if mean < rc.min_w:
            continue
        means.append(mean)
        load.intervals.append((a, b))
        load.profile.extend(segs)
        load.matched += 1 + (1 if b < until else 0)
    load.power_w = round(statistics.median(means), 1) if means else cfg.power_w
    return load
