"""Step detection: an appliance switching on or off is a step in the load."""

from __future__ import annotations

import bisect
import statistics
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise


@dataclass
class Step:
    ts: float
    delta: float  # W, positive = something switched on
    before: float
    after: float
    explained_by: str | None = None  # name of a known load or of a sub-meter (#85), if any
    used: bool = False  # consumed by an activation
    # under the fixed threshold: kept only by the noise-adaptive threshold (#88)
    low: bool = False


WINDOW_S = 20.0  # the detector's windows on a fast meter (see adapted_windows)
MERGE_S = 15.0


def meter_interval(ts: Sequence[float]) -> float | None:
    """The meter's reporting interval: the median gap between readings
    (None under 3 readings)."""
    if len(ts) > 2:
        return statistics.median(b - a for a, b in pairwise(ts))
    return None


def adapted_windows(interval_s: float | None, window_s: float, merge_s: float) -> tuple[float, float]:
    """The detector's windows for a meter reporting every ``interval_s``
    (1 s to 60 s meters): at least 3 readings per side."""
    if interval_s is None:
        return window_s, merge_s
    return max(window_s, 3.1 * interval_s), max(merge_s, 1.1 * interval_s)


# the night threshold (#77): a step smaller than the step threshold is kept
# when the PV production is known and below PV_QUIET_W in the PV_QUIET_S
# before and after it (medians), where meter drift cannot fake it
PV_QUIET_W = 20.0
PV_QUIET_S = 60.0


def night_threshold(
    step_threshold_w: float, night_step_threshold_w: float | None, has_pv: bool
) -> float | None:
    """The threshold of the small steps kept while PV produces nothing: None
    when it does not apply (option unset, no PV meter, or not lower than the
    step threshold)."""
    if not has_pv or not night_step_threshold_w or night_step_threshold_w >= step_threshold_w:
        return None
    return float(night_step_threshold_w)


def pv_quiet(
    ts: Sequence[float],
    values: Sequence[float],
    t: float,
    quiet_w: float = PV_QUIET_W,
    window_s: float = PV_QUIET_S,
) -> bool:
    """The PV production is known and below ``quiet_w`` around ``t``: the
    medians of the readings in ``[t - window_s, t)`` and ``[t, t + window_s)``
    (``ts`` sorted). False when either window is empty."""
    i = bisect.bisect_left(ts, t - window_s)
    j = bisect.bisect_left(ts, t)
    k = bisect.bisect_left(ts, t + window_s)
    if i >= j or j >= k:
        return False
    return max(statistics.median(values[i:j]), statistics.median(values[j:k])) < quiet_w


class PvQuiet:
    """:func:`pv_quiet` over a PV series: ``PvQuiet(points)(t)``."""

    def __init__(self, points: Sequence[tuple[float, float]]):
        self.ts = [p[0] for p in points]
        self.v = [p[1] for p in points]

    def __call__(self, t: float) -> bool:
        return pv_quiet(self.ts, self.v, t)


# the solar doubt (#86): the house load is grid + solar, two meters that do
# not report together. When a cloud passes, the grid reading moves at once
# and the solar reading comes later: a step appears, then cancels itself.
# So the production of a reading is in doubt by what its next different
# value shows, when that value comes within PV_LATE_S: the production may
# already have been there. Nothing is in doubt while it is below PV_QUIET_W.
PV_LATE_S = 60.0
# a reading is set aside when its doubt reaches this share of the step threshold
SOLAR_DOUBT = 0.5
HOLD_READINGS = 3  # the level held through them: the median of the last trusted readings


def solar_doubt_limit(step_threshold_w: float) -> float:
    """The solar doubt from which a reading is set aside."""
    return SOLAR_DOUBT * step_threshold_w


class SolarHold:
    """Sets aside the readings whose solar production is in doubt (#86).

    Each reading of the load comes with the solar production it was built
    with (:meth:`push`). Its **doubt** is how far the next different
    production is from its own, when one comes within ``late_s`` (a late
    or skipped solar reading: the production may already have moved, and
    the load with it); none while the production is below ``quiet_w``.

    A reading whose doubt reaches ``limit_w`` is not trusted: the detector
    gets the level before the doubt instead (the median of the last
    ``HOLD_READINGS`` trusted readings), for as long as the doubt lasts. A
    cloud that the solar meter reports late then shows no step, and an
    appliance that switched meanwhile shows one step, once the readings
    are trusted again.

    The readings come out in order, each once its doubt is known: at once
    while the solar panels produce nothing, else when the production
    changes or ``late_s`` later (:meth:`flush` at the end). The nightly
    analysis and the live view run the same instance logic, so they give
    the detector the same points. ``held``: the times of the readings set
    aside, when a list is given.
    """

    def __init__(
        self,
        limit_w: float,
        late_s: float = PV_LATE_S,
        quiet_w: float = PV_QUIET_W,
        held: list[float] | None = None,
    ):
        self.limit_w, self.late_s, self.quiet_w = limit_w, late_s, quiet_w
        self.held = held
        self.count = 0  # readings set aside
        self._pending: deque[tuple[float, float, float]] = deque()  # doubt not known yet
        self._trusted: deque[float] = deque(maxlen=HOLD_READINGS)
        self._level: float | None = None  # held through the doubt in progress

    @property
    def pending(self) -> int:
        """Readings waiting for their doubt to be known."""
        return len(self._pending)

    def push(self, ts: float, value: float, pv: float) -> list[tuple[float, float]]:
        """One reading of the load and its solar production; returns the
        points whose doubt is now known, in order."""
        out: list[tuple[float, float]] = []
        # the pending readings all have the same production (a different
        # one would have settled them): this reading settles all or none
        while self._pending:
            t0, v0, p0 = self._pending[0]
            if ts > t0 + self.late_s:
                doubt = 0.0
            elif pv != p0:
                doubt = abs(pv - p0)
            else:
                break
            self._pending.popleft()
            out.append(self._settle(t0, v0, doubt))
        if pv <= self.quiet_w:
            out.append(self._settle(ts, value, 0.0))
        else:
            self._pending.append((ts, value, pv))
        return out

    def flush(self) -> list[tuple[float, float]]:
        """End of the readings: nothing came after the pending ones."""
        out = [self._settle(t, v, 0.0) for t, v, _ in self._pending]
        self._pending.clear()
        return out

    def _settle(self, ts: float, value: float, doubt: float) -> tuple[float, float]:
        if doubt < self.limit_w:
            self._level = None
            self._trusted.append(value)
            return ts, value
        if self._level is None and self._trusted:
            self._level = statistics.median(self._trusted)
        self.count += 1
        if self.held is not None:
            self.held.append(ts)
        # nothing trusted yet (the very first readings): as it is
        return ts, value if self._level is None else self._level


def hold_doubtful(
    points: Sequence[tuple[float, float]],
    pv: Sequence[float],
    limit_w: float,
    held: list[float] | None = None,
) -> list[tuple[float, float]]:
    """``points`` for the detector, the readings whose solar production
    ``pv`` (one value per point) is in doubt set aside: :class:`SolarHold`
    over a whole series."""
    hold = SolarHold(limit_w, held=held)
    out: list[tuple[float, float]] = []
    for (ts, value), p in zip(points, pv, strict=True):
        out += hold.push(ts, value, p)
    return out + hold.flush()


# the noise-adaptive threshold (#88): the fixed thresholds were chosen for the
# worst hour of the curve (a cloudy midday) and apply at 3 a.m. too. With the
# option on, a change smaller than them is kept as a **low** step when it
# stands out of the noise measured around it: at least
# ``max(floor_w, k x noise)``, the noise being the robust spread of the
# detector's own statistic (the difference of the medians after and before a
# reading) over the readings of the last ``window_s``.
@dataclass(frozen=True)
class NoiseConfig:
    """The noise-adaptive threshold (#88)."""

    window_s: float = 1800.0  # the noise is measured over the readings of this long before
    k: float = 4.0  # a low step is at least this many times the noise
    floor_w: float = 40.0  # ... and at least this, however quiet the curve
    # under this many readings in the window the noise is not known: no low step
    min_readings: int = 30

    def threshold(self, noise_w: float | None) -> float | None:
        """The smallest low step for a measured noise (None: not known)."""
        return None if noise_w is None else max(self.floor_w, self.k * noise_w)


NOISE = NoiseConfig()
ROBUST_SIGMA = 1.4826  # x the median absolute value: the standard deviation of a normal noise


class NoiseTracker:
    """The noise of the curve around the last reading: ``ROBUST_SIGMA`` x the
    median of ``|d|`` over the readings of the last ``window_s``, ``d`` being
    the detector's statistic at each reading (:meth:`push`, in time order).
    An appliance that switches moves ``d`` at a few readings only: the
    median does not see it. One that cycles for half an hour, or a changing
    sky, raises it, and the threshold with it. Causal: the nightly analysis
    and the live view give it the same readings in the same order. It
    follows a change of the noise with a delay (half the window for a
    sudden one)."""

    def __init__(self, config: NoiseConfig = NOISE):
        self.config = config
        self._recent: deque[tuple[float, float]] = deque()
        self._sorted: list[float] = []

    def push(self, ts: float, d: float) -> None:
        a = abs(d)
        self._recent.append((ts, a))
        bisect.insort(self._sorted, a)
        while self._recent[0][0] <= ts - self.config.window_s:
            _, old = self._recent.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, old)]

    @property
    def noise_w(self) -> float | None:
        """None under ``min_readings`` readings in the window."""
        n = len(self._sorted)
        if n < self.config.min_readings:
            return None
        return ROBUST_SIGMA * self._sorted[(n - 1) // 2]

    @property
    def threshold_w(self) -> float | None:
        """The smallest low step now (None: the noise is not known)."""
        return self.config.threshold(self.noise_w)


class StepMerger:
    """Merges the candidates of the adaptive detector, given in time order
    (:meth:`add`), and gives the steps in time order.

    The candidates of the fixed thresholds are merged exactly as
    :func:`detect_steps` merges them without the option, among themselves
    only: the steps that are not low are those found with the option off.
    The low candidates are merged among themselves, per direction (the
    largest of those closer than ``merge_s`` is kept), and one within
    ``merge_s`` of a candidate of the fixed thresholds of the same direction
    is dropped: it is a part of that step.
    """

    def __init__(self) -> None:
        self._last: Step | None = None  # of the fixed thresholds, may still be merged
        self._near: dict[bool, float] = {}  # per direction: the last candidate of the fixed thresholds
        self._low: dict[bool, Step] = {}  # per direction: the low step that may still be merged
        self._ready: list[Step] = []  # final, not given yet (a step before them may still come)

    def add(self, c: Step, merge_s: float) -> None:
        up = c.delta > 0
        if c.low:
            near = self._near.get(up)
            if near is not None and c.ts - near < merge_s:
                return
            last = self._low.get(up)
            if last is not None and c.ts - last.ts < merge_s:
                if abs(c.delta) > abs(last.delta):
                    self._low[up] = c
                return
            if last is not None:
                self._ready.append(last)
            self._low[up] = c
            return
        self._near[up] = c.ts
        low = self._low.get(up)
        if low is not None and c.ts - low.ts < merge_s:
            del self._low[up]
        last = self._last
        if last is not None and c.ts - last.ts < merge_s and (last.delta > 0) == up:
            if abs(c.delta) > abs(last.delta):
                self._last = c
            return
        if last is not None:
            self._ready.append(last)
        self._last = c

    def pop(self, horizon: float | None, merge_s: float) -> list[Step]:
        """The steps that are final and in order once every candidate before
        ``horizon`` has been added (None: all of them, the end)."""
        if self._last is not None and (horizon is None or horizon >= self._last.ts + merge_s):
            self._ready.append(self._last)
            self._last = None
        for up in list(self._low):
            if horizon is None or horizon >= self._low[up].ts + merge_s:
                self._ready.append(self._low.pop(up))
        # a pending step is only ever replaced by a later one: nothing final
        # can come before the earliest of them, nor before the horizon
        bound = self.pending
        if horizon is not None and (bound is None or horizon < bound):
            bound = horizon
        self._ready.sort(key=lambda s: s.ts)
        k = len(self._ready) if bound is None else bisect.bisect_left([s.ts for s in self._ready], bound)
        out, self._ready = self._ready[:k], self._ready[k:]
        return out

    @property
    def pending(self) -> float | None:
        """The time of the earliest step that may still change."""
        times = [s.ts for s in self._low.values()]
        if self._last is not None:
            times.append(self._last.ts)
        return min(times) if times else None

    @property
    def waiting(self) -> float | None:
        """The time of the earliest step not given yet."""
        times = [s.ts for s in self._ready]
        if self.pending is not None:
            times.append(self.pending)
        return min(times) if times else None


def low_candidate(d: float, jump: float, fixed_w: float, threshold_w: float | None) -> bool:
    """A change ``d`` the fixed thresholds did not keep is a low candidate:
    under the fixed threshold that applies (``fixed_w``), at least the
    adaptive one, the reading jumping by 60 % of it as for any candidate."""
    if threshold_w is None:
        return False
    return threshold_w <= abs(d) < fixed_w and jump >= 0.6 * threshold_w


class Signal:
    """Fast windowed medians over an irregular series."""

    def __init__(self, points: Sequence[tuple[float, float]]):
        self.ts = [p[0] for p in points]
        self.v = [p[1] for p in points]

    def median(self, start: float, end: float) -> float | None:
        i = bisect.bisect_left(self.ts, start)
        j = bisect.bisect_right(self.ts, end)
        if i >= j:
            return None
        return statistics.median(self.v[i:j])


def detect_steps(
    points: Sequence[tuple[float, float]],
    threshold: float = 300.0,
    window_s: float = WINDOW_S,
    merge_s: float = MERGE_S,
    low_threshold: float | None = None,
    low_ok: Callable[[float], bool] | None = None,
    noise: NoiseConfig | None = None,
) -> list[Step]:
    """Find level changes larger than ``threshold``.

    For each reading that jumps from the previous one, the median of the
    readings in the ``window_s`` before and after is compared (the windows
    grow with the meter's median reporting interval). Medians make
    the detector robust to single-sample spikes (motor inrush, meter noise).
    Neighbouring candidates of the same sign are merged, keeping the largest.

    ``low_threshold`` and ``low_ok`` (the night threshold, #77): a change
    between ``low_threshold`` and ``threshold`` is a candidate too when
    ``low_ok(ts)`` says so (:class:`PvQuiet`: PV produces nothing). Small
    candidates are judged before merging, so where ``low_ok`` is false the
    steps are exactly those found without them.

    ``noise`` (the noise-adaptive threshold, #88): the steps above are found
    exactly as without it, and the smaller changes that stand out of the
    measured noise are added as ``low`` steps (:class:`NoiseTracker`,
    :func:`low_candidate`, :class:`StepMerger`).
    """
    sig = Signal(points)
    window_s, merge_s = adapted_windows(meter_interval(sig.ts), window_s, merge_s)
    low = low_threshold if low_ok is not None and low_threshold and low_threshold < threshold else None
    jump = (low if low is not None else threshold) * 0.6
    if noise is not None:
        return _detect_adaptive(sig, threshold, window_s, merge_s, low, low_ok, jump, noise)
    cand: list[Step] = []
    for i in range(1, len(sig.ts)):
        if abs(sig.v[i] - sig.v[i - 1]) < jump:
            continue
        t = sig.ts[i]
        before = sig.median(t - window_s, t - 1e-3)
        after = sig.median(t, t + window_s)
        if before is None or after is None:
            continue
        d = after - before
        if abs(d) >= threshold or (low is not None and abs(d) >= low and low_ok(t)):
            cand.append(Step(t, d, before, after))
    out: list[Step] = []
    for c in cand:
        if out and c.ts - out[-1].ts < merge_s and (c.delta > 0) == (out[-1].delta > 0):
            if abs(c.delta) > abs(out[-1].delta):
                out[-1] = c
        else:
            out.append(c)
    return out


def _detect_adaptive(
    sig: Signal,
    threshold: float,
    window_s: float,
    merge_s: float,
    low: float | None,
    low_ok: Callable[[float], bool] | None,
    jump: float,
    noise: NoiseConfig,
) -> list[Step]:
    """:func:`detect_steps` with the noise-adaptive threshold: every reading
    is measured (the noise is that of all of them), in time order."""
    tracker = NoiseTracker(noise)
    merger = StepMerger()
    for i in range(len(sig.ts)):
        t = sig.ts[i]
        before = sig.median(t - window_s, t - 1e-3)
        after = sig.median(t, t + window_s)
        if before is None or after is None:
            continue
        d = after - before
        tracker.push(t, d)
        moved = abs(sig.v[i] - sig.v[i - 1]) if i else 0.0
        # the night threshold (#77) applies to this change
        night = low is not None and low <= abs(d) < threshold and low_ok(t)
        if moved >= jump and (abs(d) >= threshold or night):
            merger.add(Step(t, d, before, after), merge_s)
        elif low_candidate(d, moved, low if night else threshold, tracker.threshold_w):
            merger.add(Step(t, d, before, after, low=True), merge_s)
    return merger.pop(None, merge_s)


# ---------------------------------------------------------------------------
# live: the same steps, one reading at a time
# ---------------------------------------------------------------------------
PROVISIONAL = "provisional"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"


@dataclass
class LiveStep:
    """A step seen by :class:`StepStream`. The same object is updated in
    place (and returned again) when it changes state."""

    id: int
    ts: float
    delta: float
    before: float
    after: float
    state: str = PROVISIONAL
    # provisional only: between 60 % and 100 % of the threshold (the night
    # threshold when it applies), so too small to be confirmed as it stands
    # (the guided mode says "too small")
    small: bool = False
    pv_moved: bool = False  # set by the live detector: PV jumped at the same time
    # set by the live detector (#85): the sub-meter whose misaligned reading
    # made this step; such a step ends cancelled
    explained_by: str | None = None
    # confirmed only: under the fixed threshold, kept by the noise-adaptive
    # threshold (#88). Not a line of the feed: such steps are kept apart
    low: bool = False

    def to_step(self) -> Step:
        return Step(self.ts, self.delta, self.before, self.after, self.explained_by, low=self.low)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "delta_w": round(self.delta),
            "before_w": round(self.before),
            "after_w": round(self.after),
            "state": self.state,
            "small": self.small,
            "pv_moved": self.pv_moved,
            "explained_by": self.explained_by,
            "low": self.low,
        }


class StepStream:
    """:func:`detect_steps`, one reading at a time.

    Fed with the same points and the same meter interval, the confirmed
    steps are exactly those of :func:`detect_steps`: a jump is judged once
    the window after it is complete (its median is then the same), and a
    step is confirmed once no later candidate can be merged into it. That
    is ``window_s + merge_s`` after the step (~35 s on a 10 s meter).

    Before that, a **provisional** step is given after
    ``provisional_readings`` readings (or ``provisional_s`` seconds), from
    the median of what is known after the jump: shown greyed, then
    confirmed (with the final values) or cancelled (a spike). Neighbouring
    jumps of the same sign share one provisional step, as they would be
    merged. A step confirmed with no provisional one is new.

    ``interval_s``: the meter's reporting interval that sizes the windows
    (:func:`adapted_windows`); None estimates it from the last readings
    (the night uses the median of the whole day). Points must come in time
    order: an older point is dropped (and counted in ``dropped``).

    ``low_threshold``, ``low_ok`` and ``low_wait_s``: the night threshold of
    :func:`detect_steps`. ``low_ok(ts)`` needs what comes ``low_wait_s``
    after the step (the PV after it), so with them every jump is judged
    once ``max(window_s, low_wait_s)`` has passed, keeping the order (and
    the merges) of the night: confirmation comes later.

    ``noise``: the noise-adaptive threshold of :func:`detect_steps` (#88).
    Every reading is then measured once its window is complete, in time
    order, with the night's own :class:`NoiseTracker` and
    :class:`StepMerger`: the same steps, the ``low`` ones among them, at the
    same delay as without it. A low step never takes a provisional step:
    the feed shows what it shows with the option off.
    """

    def __init__(
        self,
        threshold: float = 300.0,
        window_s: float = WINDOW_S,
        merge_s: float = MERGE_S,
        interval_s: float | None = None,
        provisional_readings: int = 3,
        provisional_s: float = 10.0,
        estimate_readings: int = 101,
        low_threshold: float | None = None,
        low_ok: Callable[[float], bool] | None = None,
        low_wait_s: float = PV_QUIET_S,
        noise: NoiseConfig | None = None,
    ):
        self.threshold = threshold
        self.tracker = NoiseTracker(noise) if noise is not None else None
        self._merger = StepMerger()
        self._n = 0  # with the adaptive threshold: the next reading to measure
        self.low_threshold = (
            low_threshold if low_ok is not None and low_threshold and low_threshold < threshold else None
        )
        self.low_ok = low_ok
        self.low_wait_s = low_wait_s
        self._base = (window_s, merge_s)
        self.interval_s = interval_s
        self._fixed = interval_s is not None
        self.window_s, self.merge_s = adapted_windows(interval_s, window_s, merge_s)
        self.provisional_readings = provisional_readings
        self.provisional_s = provisional_s
        self._gaps: deque[float] = deque(maxlen=estimate_readings - 1)
        self._ts: list[float] = []
        self._v: list[float] = []
        self._jumps: deque[float] = deque()  # judged once their window is complete
        self._early: deque[float] = deque()  # waiting for their provisional step
        self._last: Step | None = None  # the last candidate, may still be merged
        self._open: list[LiveStep] = []  # provisional steps
        self._next_id = 1
        self.readings = 0
        self.dropped = 0
        self.confirmed = 0
        self.cancelled = 0

    @property
    def latest(self) -> float | None:
        return self._ts[-1] if self._ts else None

    def push(self, ts: float, value: float) -> list[LiveStep]:
        """Add one reading; returns the steps that appeared or changed."""
        if self._ts and ts < self._ts[-1]:
            self.dropped += 1
            return []
        self.readings += 1
        if self._ts:
            self._gaps.append(ts - self._ts[-1])
            if not self._fixed and len(self._gaps) >= 2:
                self.interval_s = statistics.median(self._gaps)
                self.window_s, self.merge_s = adapted_windows(self.interval_s, *self._base)
            if abs(value - self._v[-1]) >= self.smallest * 0.6:
                if self.tracker is None:  # else every reading is measured, in order
                    self._jumps.append(ts)
                self._early.append(ts)
        self._ts.append(ts)
        self._v.append(value)
        out: list[LiveStep] = []
        self._provisional(out)
        self._judge(out, final=False)
        self._expire(out, final=False)
        self._prune()
        return out

    def flush(self) -> list[LiveStep]:
        """End of the stream: judge the last jumps with what is known (as
        :func:`detect_steps` does at the end of its points)."""
        out: list[LiveStep] = []
        self._early.clear()
        self._judge(out, final=True)
        self._expire(out, final=True)
        return out

    @property
    def smallest(self) -> float:
        """The smallest step that can be kept: the night threshold when set."""
        return self.low_threshold if self.low_threshold is not None else self.threshold

    @property
    def settled(self) -> float | None:
        """Every step before this time has been given: a step confirmed
        later is at this time or after it (the oldest jump not judged yet,
        the candidate that may still be merged, else the last reading).
        None before the first reading."""
        times = [self._ts[-1]] if self._ts else []
        if self._jumps:
            times.append(self._jumps[0])
        if self._last is not None:
            times.append(self._last.ts)
        if self.tracker is not None:
            if self._n < len(self._ts):
                times.append(self._ts[self._n])
            if self._merger.waiting is not None:
                times.append(self._merger.waiting)
        return min(times) if times else None

    # -- internals ---------------------------------------------------------
    def _delay(self) -> float:
        """How long after a jump it is judged."""
        if self.low_threshold is None:
            return self.window_s
        return max(self.window_s, self.low_wait_s)

    def _low(self, t: float) -> bool:
        """The night threshold applies to a step at ``t``."""
        return self.low_threshold is not None and self.low_ok(t)

    def _accept(self, t: float, d: float) -> bool:
        """The rule of :func:`detect_steps`."""
        if abs(d) >= self.threshold:
            return True
        return self.low_threshold is not None and abs(d) >= self.low_threshold and self.low_ok(t)

    def _median(self, start: float, end: float) -> float | None:
        i = bisect.bisect_left(self._ts, start)
        j = bisect.bisect_right(self._ts, end)
        if i >= j:
            return None
        return statistics.median(self._v[i:j])

    def _provisional(self, out: list[LiveStep]) -> None:
        latest = self._ts[-1]
        while self._early:
            t = self._early[0]
            n = len(self._ts) - bisect.bisect_left(self._ts, t)
            if n < self.provisional_readings and latest - t < self.provisional_s:
                break  # the later jumps have even fewer readings
            self._early.popleft()
            before = self._median(t - self.window_s, t - 1e-3)
            after = self._median(t, min(latest, t + self.window_s))
            if before is None or after is None:
                continue
            d = after - before
            limit = self.low_threshold if self._low(t) else self.threshold
            if abs(d) < limit * 0.6:
                continue
            small = abs(d) < limit
            near = [p for p in self._open if (p.delta > 0) == (d > 0) and abs(p.ts - t) < self.merge_s]
            if near:
                p = near[-1]
                if abs(d) > abs(p.delta):
                    p.ts, p.delta, p.before, p.after, p.small = t, d, before, after, small
                    self._emit(out, p)
                continue
            p = LiveStep(self._new_id(), t, d, before, after, PROVISIONAL, small)
            self._open.append(p)
            out.append(p)

    def _judge(self, out: list[LiveStep], final: bool) -> None:
        if self.tracker is not None:
            self._judge_adaptive(out, final)
            return
        latest = self._ts[-1] if self._ts else None
        delay = self._delay()
        while self._jumps and (final or self._jumps[0] + delay < latest):
            t = self._jumps.popleft()
            before = self._median(t - self.window_s, t - 1e-3)
            after = self._median(t, t + self.window_s)
            if before is None or after is None:
                continue
            d = after - before
            if not self._accept(t, d):
                continue
            c = Step(t, d, before, after)
            last = self._last
            if last and c.ts - last.ts < self.merge_s and (c.delta > 0) == (last.delta > 0):
                if abs(c.delta) > abs(last.delta):
                    self._last = c
            else:
                if last:
                    self._confirm(out, last)
                self._last = c
        last = self._last
        if last and (
            final
            or (
                latest >= last.ts + self.merge_s
                and (not self._jumps or self._jumps[0] >= last.ts + self.merge_s)
            )
        ):
            self._confirm(out, last)
            self._last = None

    def _judge_adaptive(self, out: list[LiveStep], final: bool) -> None:
        """:func:`_detect_adaptive`, for the readings whose window is complete."""
        ts, v = self._ts, self._v
        if not ts:
            return
        latest = ts[-1]
        delay = self._delay()
        jump = self.smallest * 0.6
        low = self.low_threshold
        while self._n < len(ts) and (final or ts[self._n] + delay < latest):
            i = self._n
            self._n += 1
            t = ts[i]
            before = self._median(t - self.window_s, t - 1e-3)
            after = self._median(t, t + self.window_s)
            if before is None or after is None:
                continue
            d = after - before
            self.tracker.push(t, d)
            moved = abs(v[i] - v[i - 1]) if i else 0.0
            night = low is not None and low <= abs(d) < self.threshold and self.low_ok(t)
            if moved >= jump and (abs(d) >= self.threshold or night):
                self._merger.add(Step(t, d, before, after), self.merge_s)
            elif low_candidate(d, moved, low if night else self.threshold, self.tracker.threshold_w):
                self._merger.add(Step(t, d, before, after, low=True), self.merge_s)
        # every candidate before the next reading to measure is in (a later
        # reading is not before the last one)
        horizon = None if final else (ts[self._n] if self._n < len(ts) else latest)
        for step in self._merger.pop(horizon, self.merge_s):
            self._confirm(out, step)

    def _confirm(self, out: list[LiveStep], step: Step) -> None:
        self.confirmed += 1
        up = step.delta > 0
        near = [p for p in self._open if (p.delta > 0) == up and abs(p.ts - step.ts) < 2 * self.merge_s]
        if step.low:
            near = []  # its provisional step, if any, ends cancelled as with the option off
        if near:
            p = min(near, key=lambda p: abs(p.ts - step.ts))
            self._open.remove(p)
        else:
            p = LiveStep(self._new_id(), step.ts, step.delta, step.before, step.after)
        p.ts, p.delta, p.before, p.after = step.ts, step.delta, step.before, step.after
        p.state, p.small, p.low = CONFIRMED, False, step.low
        self._emit(out, p)

    def _expire(self, out: list[LiveStep], final: bool) -> None:
        """Cancel the provisional steps no confirmed step can claim any more."""
        if not self._open:
            return
        horizon = self.settled
        keep = []
        for p in self._open:
            if final or p.ts + 2 * self.merge_s <= horizon:
                p.state = CANCELLED
                self.cancelled += 1
                self._emit(out, p)
            else:
                keep.append(p)
        self._open = keep

    def _emit(self, out: list[LiveStep], p: LiveStep) -> None:
        if not any(x is p for x in out):
            out.append(p)

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _prune(self) -> None:
        if len(self._ts) < 1024:
            return
        oldest = self._ts[-1]
        for q in (self._jumps, self._early):
            if q:
                oldest = min(oldest, q[0])
        if self.tracker is not None and self._n < len(self._ts):
            oldest = min(oldest, self._ts[self._n])
        k = bisect.bisect_left(self._ts, oldest - self.window_s - 1.0)
        if self.tracker is not None:
            k = min(k, self._n - 1)  # the reading before the next one: its jump
        if k > 512:
            del self._ts[:k]
            del self._v[:k]
            self._n -= k
