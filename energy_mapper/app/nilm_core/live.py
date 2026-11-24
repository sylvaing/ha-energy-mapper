"""The live view (#17): steps, events and candidates while someone watches.

The pipeline, fed with raw Home Assistant states one at a time::

    states -> SampleBuilder -> StepStream -> LivePairer -> CandidateRanker
              (signal.py)      (steps.py)    events        top 3 per event

* the samples are those of the night (:class:`~.signal.SampleBuilder`, same
  rules as :func:`~.signal.build_samples`);
* the steps are those of the night (:class:`~.steps.StepStream`, same result
  as :func:`~.steps.detect_steps`), provisional first;
* an **event** is one line of the feed: a switch-on, completed by the
  off-step that closes it (the rule of :func:`~.activations.pair_steps`), or
  a **session** when alternating steps of the same size follow each other
  (the rule of :func:`~.activations.find_cycling_sessions`) or when short
  uses of the same power come back regularly (a thermostat: the rule of
  :func:`~.activations.burst_series`), or an off-step whose switch-on was
  not seen;
* each event gets its best **candidates**: registry signatures near its
  power (:meth:`~.registry.Matcher.near`, the registry's own tolerance) and
  switches that changed at the same time, with a score from 0 to 1.

Everything here is pure (no I/O, no clock): the caller pushes the states
and says up to when the samples can be built. :func:`replay` streams a
stored day through the same pipeline (tests, evaluation, benchmarks).
"""

from __future__ import annotations

import bisect
import math
import statistics
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .activations import BURSTS, BurstConfig, burst_series
from .registry import GROUPS, MULTIPHASE, Matcher, is_ignored
from .signal import Sample, SampleBuilder
from .signatures import DURATION_BANDS, duration_band
from .steps import (
    CANCELLED,
    CONFIRMED,
    PROVISIONAL,
    PV_QUIET_S,
    PV_QUIET_W,
    LiveStep,
    SolarHold,
    Step,
    StepStream,
    meter_interval,
    pv_quiet,
    solar_doubt_limit,
)
from .submeters import SubmeterEchoes

# score thresholds of the candidates: "looks like" / "maybe" / "unknown"
LIKELY = 0.6
MAYBE = 0.3
SWITCH_SCORE = 0.95  # a relay that changed at the same time: nearly certain
UNAVAILABLE = {"unavailable", "unknown", ""}


def confidence(score: float | None) -> str:
    """``likely`` (>= 0.6), ``maybe`` (>= 0.3) or ``unknown``."""
    if score is None or score < MAYBE:
        return "unknown"
    return "likely" if score >= LIKELY else "maybe"


# ---------------------------------------------------------------------------
# events: the lines of the feed
# ---------------------------------------------------------------------------
@dataclass
class LiveEvent:
    """One line of the feed. Updated in place when its steps change.

    ``delta_w`` is the switch-on step (positive), or a negative off-step
    whose switch-on was not seen (before the view started). ``steps`` are
    the confirmed ``(ts, delta)`` steps of the event: on, off, on, off...
    for a session. ``series``: a series of regular bursts (#87), which took
    the steps of the events of its bursts.
    """

    id: int
    t_on: float
    delta_w: float
    state: str = PROVISIONAL
    t_off: float | None = None
    off_w: float | None = None  # size of the last off-step (positive)
    steps: list[tuple[float, float]] = field(default_factory=list)
    pv_moved: bool = False
    candidates: list[dict] = field(default_factory=list)
    step_id: int | None = None  # the live step that started it
    pending: LiveStep | None = field(default=None, repr=False)  # provisional next cycle
    series: bool = False

    @property
    def running(self) -> bool:
        """Switched on and not (yet) off."""
        if self.delta_w <= 0 or self.state == CANCELLED:
            return False
        return not self.steps or self.steps[-1][1] > 0

    @property
    def cycles(self) -> int:
        return sum(1 for _, d in self.steps if d > 0)

    @property
    def is_session(self) -> bool:
        return self.cycles >= 2

    @property
    def last_on(self) -> float:
        ons = [t for t, d in self.steps if d > 0]
        return ons[-1] if ons else self.t_on

    @property
    def power_w(self) -> float:
        """Typical power: a session's median step, an activation's mean of
        its two steps (as :func:`pair_steps`), else the step itself."""
        if self.is_session:
            return statistics.median(abs(d) for _, d in self.steps)
        if self.off_w is not None and not self.running:
            return (self.delta_w + self.off_w) / 2
        return abs(self.delta_w)

    @property
    def duration_s(self) -> float | None:
        if self.t_off is None or self.running:
            return None
        return self.t_off - self.t_on

    def session(self, min_steps: int = 6) -> dict | None:
        """A session's cycles; ``confirmed`` once it has as many steps as the
        night needs to call it a session (a series of bursts is one from the
        start: the night's rule made it)."""
        if not self.is_session:
            return None
        ons, periods = [], []
        for (ta, da), (tb, db) in zip(self.steps, self.steps[1:], strict=False):
            if da > 0 and db < 0:
                ons.append(tb - ta)
        ups = [t for t, d in self.steps if d > 0]
        periods = [b - a for a, b in zip(ups, ups[1:], strict=False)]
        end = self.steps[-1][0]
        span = end - self.t_on
        return {
            "count": len(ups),
            "steps": len(self.steps),
            "on_s": round(statistics.median(ons), 1) if ons else None,
            "period_s": round(statistics.median(periods), 1) if periods else None,
            "duty": round(min(1.0, sum(ons) / span), 3) if span > 0 else None,
            "confirmed": self.series or len(self.steps) >= min_steps,
        }

    def to_dict(self) -> dict:
        top = self.candidates[0]["score"] if self.candidates else None
        return {
            "id": self.id,
            "t_on": self.t_on,
            "delta_w": round(self.delta_w),
            "state": self.state,
            "running": self.running,
            "t_off": self.t_off,
            "off_w": round(self.off_w) if self.off_w is not None else None,
            "duration_s": round(self.duration_s) if self.duration_s is not None else None,
            "power_w": round(self.power_w),
            "session": self.session(),
            "pv_moved": self.pv_moved,
            "confidence": confidence(top),
            "candidates": self.candidates,
        }


@dataclass(frozen=True)
class _Unit:
    """A closed burst the pairer remembers (the unit of
    :func:`~.activations.burst_series`), with its steps and its event."""

    start: float
    end: float
    power_w: float
    event: int
    steps: tuple[tuple[float, float], ...]


class LivePairer:
    """Turns live steps into events, as the night pairs its steps.

    * a provisional switch-on opens a provisional event (a small one, under
      the threshold, opens none); confirmed, the event is confirmed;
      cancelled, the event is cancelled;
    * a confirmed off-step closes the running event closest in size within
      ``max(tol_w, tol_frac x size)`` (ties: the most recent), the rule of
      :func:`pair_steps`; none: an event of its own (negative ``delta_w``);
    * a switch-on of at least ``session_min_w`` within ``session_gap_s`` of
      the off-step of an event whose steps are all that close and of the
      same size (``session_tol`` of their median) continues that event as a
      session instead of opening a new line: the rule of
      :func:`find_cycling_sessions` (which the night applies from 6 steps);
    * a closed event that completes a series of regular bursts (a
      thermostat, #87: :func:`~.activations.burst_series` on the closed
      short events and sessions of the last ``series_memory_s``) makes one
      session of them: the earliest event still held takes the steps of
      the others, which are cancelled (they leave the feed). The next
      bursts open a line each, and join the series when they close.

    Simplifications: the night splits a step that combines a cycle and
    another appliance, and validates its pairs against the load; live does
    neither. Once in a series, the bursts of a fast train count one by one
    where the night counts their session once (only the median power of the
    series can differ).
    """

    def __init__(
        self,
        tol_frac: float = 0.25,
        tol_w: float = 60.0,
        max_on_s: float = 6 * 3600.0,
        session_min_w: float = 600.0,
        session_gap_s: float = 120.0,
        session_tol: float = 0.2,
        session_min_steps: int = 6,
        first_id: int = 1,
        bursts: BurstConfig = BURSTS,
    ):
        self.tol_frac, self.tol_w, self.max_on_s = tol_frac, tol_w, max_on_s
        self.session_min_w, self.session_gap_s = session_min_w, session_gap_s
        self.session_tol, self.session_min_steps = session_tol, session_min_steps
        self.bursts = bursts
        # the bursts of a series are looked for this far back: enough for
        # the blocks a series needs at the longest period
        self.series_memory_s = (bursts.min_blocks + 1) * bursts.max_period_s
        self.events: dict[int, LiveEvent] = {}
        self._by_step: dict[int, LiveEvent] = {}
        self._units: list[_Unit] = []  # the closed bursts, by start
        self._next_id = first_id  # the caller keeps ids unique across detectors

    def feed(self, step: LiveStep) -> list[LiveEvent]:
        """One step update; returns the events that appeared or changed."""
        ev = self._by_step.get(step.id)
        if step.state == PROVISIONAL:
            if step.delta <= 0 or step.small:
                return []
            if ev is not None:
                if ev.pending is not step:
                    ev.t_on, ev.delta_w, ev.pv_moved = step.ts, step.delta, step.pv_moved
                return [ev]
            sess = self._session_for(step.ts, step.delta)
            if sess is not None:
                sess.pending = step
                self._by_step[step.id] = sess
                return [sess]
            ev = self._new(step, PROVISIONAL)
            return [ev]
        if step.state == CANCELLED:
            if ev is None:
                return []
            del self._by_step[step.id]
            if ev.pending is step:
                ev.pending = None
                return [ev]
            if ev.state == PROVISIONAL:
                ev.state = CANCELLED
                return [ev]
            return []
        # confirmed
        if step.delta > 0:
            if ev is not None and ev.pending is not step:
                ev.state, ev.t_on, ev.delta_w = CONFIRMED, step.ts, step.delta
                ev.pv_moved = step.pv_moved
                ev.steps = [(step.ts, step.delta)]
                return [ev]
            if ev is not None:
                ev.pending = None
                del self._by_step[step.id]
            sess = self._session_for(step.ts, step.delta)
            if sess is not None:
                sess.steps.append((step.ts, step.delta))
                sess.t_off = None
                self._by_step[step.id] = sess
                return [sess]
            ev = self._new(step, CONFIRMED)
            return [ev]
        best = self._pair(step)
        if best is not None:
            best.steps.append((step.ts, step.delta))
            best.t_off, best.off_w = step.ts, -step.delta
            self._by_step[step.id] = best
            return self._series(best, step.ts)
        return [self._new(step, CONFIRMED)]

    def _new(self, step: LiveStep, state: str) -> LiveEvent:
        ev = LiveEvent(
            id=self._next_id,
            t_on=step.ts,
            delta_w=step.delta,
            state=state,
            steps=[(step.ts, step.delta)] if state == CONFIRMED else [],
            pv_moved=step.pv_moved,
            step_id=step.id,
        )
        self._next_id += 1
        self.events[ev.id] = ev
        self._by_step[step.id] = ev
        return ev

    def _pair(self, step: LiveStep) -> LiveEvent | None:
        size = -step.delta
        best, best_err = None, None
        for o in self.events.values():
            if o.state != CONFIRMED or not o.running or step.ts - o.last_on > self.max_on_s:
                continue
            ref = o.power_w
            tol = max(self.tol_w, self.tol_frac * ref)
            err = abs(ref - size)
            if err > tol:
                continue
            rel = err / tol
            if (
                best is None
                or rel < best_err - 1e-9
                or (abs(rel - best_err) <= 1e-9 and o.last_on > best.last_on)
            ):
                best, best_err = o, rel
        return best

    def _session_for(self, ts: float, delta: float) -> LiveEvent | None:
        if delta < self.session_min_w:
            return None
        best = None
        for o in self.events.values():
            if o.state != CONFIRMED or o.running or o.delta_w <= 0 or o.t_off is None or o.pending:
                continue
            if not 0 <= ts - o.t_off <= self.session_gap_s:
                continue
            sizes = [abs(d) for _, d in o.steps]
            times = [t for t, _ in o.steps]
            if min(sizes) < self.session_min_w:
                continue
            if any(b - a > self.session_gap_s for a, b in zip(times, times[1:], strict=False)):
                continue
            ref = statistics.median(sizes)
            if abs(delta - ref) > self.session_tol * ref:
                continue
            if best is None or o.t_off > best.t_off:
                best = o
        return best

    def _bursts_of(self, ev: LiveEvent) -> list[_Unit]:
        """The bursts of a closed event, as the night would see them: its
        session once it has the steps of one, else each of its on/off
        pairs that is short enough."""
        cfg, steps = self.bursts, ev.steps
        if len(steps) >= self.session_min_steps:
            power = round(statistics.median(abs(d) for _, d in steps), 1)
            if cfg.is_burst(power, steps[-1][0] - steps[0][0], cycling=True):
                return [_Unit(steps[0][0], steps[-1][0], power, ev.id, tuple(steps))]
            return []
        out = []
        for (ta, da), (tb, db) in zip(steps, steps[1:], strict=False):
            power = round((da - db) / 2, 1)
            if da > 0 and db < 0 and cfg.is_burst(power, tb - ta):
                out.append(_Unit(ta, tb, power, ev.id, ((ta, da), (tb, db))))
        return out

    def _series(self, ev: LiveEvent, now: float) -> list[LiveEvent]:
        """``ev`` just closed: the events that changed, ``ev`` first. When
        it completes or continues a series of regular bursts, the series is
        one event (the one that already is, else the earliest still held)
        and the events of its other bursts are cancelled."""
        if ev.series or ev.delta_w <= 0:
            return [ev]
        since = now - self.series_memory_s
        self._units = [u for u in self._units if u.event != ev.id and u.end >= since]
        mine = self._bursts_of(ev)
        if not mine:
            return [ev]
        self._units = sorted(self._units + mine, key=lambda u: u.start)
        units = self._units
        found = None
        for blocks in burst_series([(u.start, u.end, u.power_w) for u in units], self.bursts):
            members = [units[i] for b in blocks for i in b]
            if any(u.event == ev.id for u in members):
                found = members
                break
        if found is None:
            return [ev]
        held = []  # the events of the bursts, each once, in time order
        for u in found:
            o = self.events.get(u.event)
            if o is not None and o.state == CONFIRMED and all(o is not h for h in held):
                held.append(o)
        target = next((o for o in held if o.series), held[0])
        steps = {st for u in found for st in u.steps} | set(target.steps)
        target.steps = sorted(steps)
        target.series = True
        target.t_on, target.delta_w = target.steps[0]
        target.t_off, target.off_w = target.steps[-1][0], -target.steps[-1][1]
        others = [o for o in held if o is not target]
        for o in others:
            o.state, o.pending = CANCELLED, None
        gone = {o.id for o in others}
        # their steps are final; a provisional next cycle will open its own event
        self._by_step = {k: v for k, v in self._by_step.items() if v.id not in gone}
        self._units = [replace(u, event=target.id) if u.event in gone else u for u in units]
        return [ev, *(o for o in [target, *others] if o is not ev)]

    def prune(self, before: float) -> None:
        """Forget the events over before ``before`` (and those running for
        longer than ``max_on_s``). A series of bursts is kept for one more
        period: its next burst may still come."""
        drop = set()
        for ev in self.events.values():
            last = ev.steps[-1][0] if ev.steps else ev.t_on
            if ev.series and ev.state == CONFIRMED:
                last += self.bursts.max_period_s
            if (
                (ev.state == CANCELLED and ev.t_on < before)
                or (ev.running and before - ev.last_on > self.max_on_s)
                or (not ev.running and last < before and ev.state != PROVISIONAL)
            ):
                drop.add(ev.id)
        for i in drop:
            del self.events[i]
        self._by_step = {k: v for k, v in self._by_step.items() if v.id not in drop}


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------
@dataclass
class SwitchChange:
    """A switch (relay) that changed state: strong evidence for a step at
    the same time."""

    entity_id: str
    ts: float
    on: bool
    name: str | None = None  # the appliance's name
    appliance: str | None = None  # the appliance's key
    power_w: float | None = None  # known power, if any


class CandidateRanker:
    """The best candidates of an event, scored from 0 to 1.

    For each registry signature (named or not, except those the user
    ignored) of a group the event can still be in, within ``within`` times
    the registry's power tolerance (:meth:`Matcher.near`):

    * power: ``exp(-d^2 / 2)``, ``d`` the distance in tolerances (1 at the
      center, 0.61 at the edge of the night's tolerance);
    * habits, to break ties: the share of the signature's uses at this hour
      and its frequency move the score between 85 % and 100 %;
    * the group: a session matches cycling signatures; a closed event the
      band of its duration (and multi-phase appliances, whose first phase it
      may be) and, when it is a burst (short, at least 600 W: maybe one
      burst of a thermostat, #87), cycling signatures; a running event the
      bands its elapsed time still allows (a ``short`` one is out after
      10 min, a session after 2 min, or 5 min from 600 W);
    * PV moved at the same time: the score is halved, so it is never above
      "maybe".

    A switch that changed in the same direction within ``switch_window_s``
    scores 0.95 (0.5 when its known power is far from the step).
    """

    def __init__(
        self,
        registry: Mapping | None = None,
        local_hour: Callable[[float], int] | None = None,
        limit: int = 3,
        within: float = 2.0,
        switch_window_s: float = 30.0,
        min_score: float = 0.05,
        session_gap_s: float = 120.0,
        bursts: BurstConfig = BURSTS,
    ):
        self.bursts = bursts
        self.matcher = Matcher(registry)
        self.sigs = {s["id"]: s for s in (registry or {}).get("signatures") or [] if not is_ignored(s)}
        self.local_hour = local_hour or (lambda ts: time.localtime(ts).tm_hour)
        self.limit, self.within, self.min_score = limit, within, min_score
        self.switch_window_s, self.session_gap_s = switch_window_s, session_gap_s

    def groups(self, event: LiveEvent, now: float) -> list[tuple[str, str]]:
        if event.is_session:
            return [("cycling", "session")]
        if event.delta_w < 0:
            return [*GROUPS, MULTIPHASE]
        if not event.running and event.duration_s is not None:
            out = [("steady", duration_band(event.duration_s)), MULTIPHASE]
            if self.bursts.is_burst(event.power_w, event.duration_s):
                out.append(("cycling", "session"))  # maybe one burst of a thermostat
            return out
        elapsed = max(0.0, now - event.t_on)
        out = [("steady", name) for _lo, hi, name in DURATION_BANDS if hi > elapsed]
        if elapsed <= self.session_gap_s or self.bursts.is_burst(event.delta_w, elapsed):
            out.append(("cycling", "session"))
        return [*out, MULTIPHASE]

    def rank(self, event: LiveEvent, now: float, switches: Iterable[SwitchChange] = ()) -> list[dict]:
        power = event.power_w
        hour = self.local_hour(event.t_on) % 24
        penalty = 0.5 if event.pv_moved else 1.0
        out = []
        for kind, band in self.groups(event, now):
            for sid, d in self.matcher.near(kind, band, power, self.within):
                s = self.sigs.get(sid)
                if s is None:
                    continue
                hours = s.get("hours") or []
                top = max(hours, default=0) if len(hours) == 24 else 0
                share = hours[hour] / top if top > 0 else 0.0
                per_week = float(s.get("per_week") or 0.0)
                habit = 0.5 * share + 0.5 * min(1.0, per_week / 7.0)
                score = math.exp(-0.5 * d * d) * (0.85 + 0.15 * habit) * penalty
                out.append(
                    {
                        "source": "signature",
                        "signature_id": sid,
                        "label": s.get("label"),
                        "appliance": s.get("appliance"),
                        "kind": s.get("kind"),
                        "band": s.get("band"),
                        "power_w": round(float(s.get("power_w") or 0)),
                        "score": score,
                        "reason": {
                            "power_w": round(float(s.get("power_w") or 0)),
                            "distance": round(d, 2),
                            "usual_hour": share >= 0.5,
                            "per_week": round(per_week, 1),
                            "expected": bool(s.get("expected")),
                            "pv_moved": event.pv_moved,
                        },
                    }
                )
        up = event.delta_w > 0
        for sw in switches:
            dt = sw.ts - event.t_on
            if sw.on != up or abs(dt) > self.switch_window_s:
                continue
            far = sw.power_w and abs(power - sw.power_w) > 0.5 * sw.power_w
            out.append(
                {
                    "source": "switch",
                    "entity_id": sw.entity_id,
                    "label": sw.name,
                    "appliance": sw.appliance,
                    "power_w": round(sw.power_w) if sw.power_w else None,
                    "score": (0.5 if far else SWITCH_SCORE) * penalty,
                    "reason": {"switch": sw.entity_id, "seconds": round(dt), "pv_moved": event.pv_moved},
                }
            )
        out = [c for c in out if c["score"] >= self.min_score]
        out.sort(key=lambda c: -c["score"])
        for c in out:
            c["score"] = round(c["score"], 2)
            c["confidence"] = confidence(c["score"])
        return out[: self.limit]


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------
@dataclass
class LiveUpdate:
    """What one :meth:`LiveDetector.advance` produced."""

    samples: list[Sample] = field(default_factory=list)
    steps: list[LiveStep] = field(default_factory=list)  # appeared or changed
    events: list[LiveEvent] = field(default_factory=list)  # appeared or changed


class LiveDetector:
    """The live pipeline: push raw states (:meth:`push`), then build up to a
    bound (:meth:`advance`), a few seconds behind the clock so that every
    meter has reported (PV and grid are not sampled together).

    ``config`` is the nightly ``AnalysisConfig`` (meters, sub-meters,
    switched loads, step threshold). ``first_event_id``: the id of the first
    event (a live service keeps them unique across its sessions). ``interval_s``: the meter's reporting
    interval that sizes the detector windows (the night's is the median of
    the day; None estimates it from the last readings). A step is marked
    ``pv_moved`` when the PV median moved by at least ``pv_moved_w``
    (default: half the step threshold) across it.

    With the night threshold (``config.low_step_threshold()``, #77), smaller
    steps are kept where the PV production stays near zero around them, as
    the night does (:func:`~.steps.pv_quiet` on the PV of the samples); the
    steps are then confirmed once the minute of PV after them is known.

    With a PV meter, the samples whose solar production is in doubt (a late
    solar reading, #86) are set aside as the night does
    (:class:`~.steps.SolarHold`): while the panels produce, a sample reaches
    the step detector once its production has changed, or a minute later,
    so the steps come that much later than the samples.

    With sub-meters that measure (#85), the steps their misaligned reports
    leave are explained as the night explains them
    (:class:`~.submeters.SubmeterEchoes`, the same code on the same steps):
    a step the stream confirmed stays provisional until no sub-meter can
    explain it any more (``LAG_S`` and the merge delay after it, with
    nothing pending in the stream before that), and then comes out
    confirmed, or cancelled with ``explained_by`` the sub-meter. Without
    such a sub-meter nothing waits. The sub-meters are read against the
    points the stream read (the solar doubt set aside), as in the night.

    With the noise-adaptive threshold (``config.adaptive_threshold``, #88),
    the stream also gives the night's ``low`` steps. They are listed in the
    updates (``LiveStep.low``) and go no further: no event, no candidate.
    The feed is what it is with the option off.
    """

    PV_KEEP_S = 600.0
    ECHO_KEEP_S = 600.0  # samples kept for the sub-meters' echoes, beyond the windows
    SWITCH_KEEP = 200

    def __init__(
        self,
        config,
        registry: Mapping | None = None,
        local_hour: Callable[[float], int] | None = None,
        interval_s: float | None = None,
        pv_moved_w: float | None = None,
        first_event_id: int = 1,
    ):
        self.config = config
        self.local_hour = local_hour
        self.builder = SampleBuilder.from_config(config)
        low = config.low_step_threshold()
        self.night_threshold_w = low
        self.stream = StepStream(
            threshold=config.step_threshold_w,
            interval_s=interval_s,
            low_threshold=low,
            low_ok=self._pv_quiet if low is not None else None,
            noise=config.noise_config(),
        )
        # the solar doubt (#86): the samples wait here until theirs is known
        # no solar doubt with a house meter (#97): the production is not in the house value
        self.hold = (
            SolarHold(solar_doubt_limit(config.step_threshold_w))
            if config.pv_entity and not config.house_entity
            else None
        )
        self.pairer = LivePairer(first_id=first_event_id)
        self.ranker = CandidateRanker(registry, local_hour)
        self.pv_moved_w = pv_moved_w if pv_moved_w is not None else 0.5 * config.step_threshold_w
        self.switched = {s.entity_id: s for s in config.switched_loads}
        self.switches: deque[SwitchChange] = deque(maxlen=self.SWITCH_KEEP)
        self._switch_on: dict[str, bool] = {}
        self._pv_ts: list[float] = []
        self._pv_v: list[float] = []
        self.echoes = SubmeterEchoes.for_config(config)
        self._held: deque[LiveStep] = deque()  # confirmed by the stream, a sub-meter may explain them
        self._subs: deque[tuple[float, ...]] = deque()  # Sample.subs of the samples the hold keeps
        self.now: float | None = None  # time of the last sample

    @property
    def entities(self) -> list[str]:
        """The entities to subscribe to."""
        return list(dict.fromkeys([*self.builder.entities, *self.switched]))

    @property
    def events(self) -> dict[int, LiveEvent]:
        return self.pairer.events

    def set_registry(self, registry: Mapping | None) -> None:
        """A new registry (after the nightly update or a review decision)."""
        self.ranker = CandidateRanker(registry, self.local_hour)

    def push(self, entity_id: str, ts: float, state) -> None:
        """One raw HA state of a meter or a switched load."""
        self.builder.push(entity_id, ts, state)
        sl = self.switched.get(entity_id)
        if sl is not None and state not in UNAVAILABLE and state is not None:
            on = state in sl.on_states
            if entity_id in self._switch_on and self._switch_on[entity_id] != on:
                self.switches.append(
                    SwitchChange(entity_id, float(ts), on, name=sl.name, power_w=sl.power_w or None)
                )
            self._switch_on[entity_id] = on

    def add_switch(self, change: SwitchChange) -> None:
        """A switch change known otherwise (an appliance's switch source)."""
        self.switches.append(change)

    def advance(self, before: float) -> LiveUpdate:
        """Build the samples before ``before`` and run them through."""
        return self._process(self.builder.pop(before), final=False)

    def flush(self) -> LiveUpdate:
        """End of the stream: everything pushed, the last steps judged."""
        return self._process(self.builder.flush(), final=True)

    def rerank(self, now: float) -> list[LiveEvent]:
        """Rank the running events again (their elapsed time rules out
        bands); returns those whose candidates changed."""
        out = []
        for ev in self.pairer.events.values():
            if ev.running:
                cands = self.ranker.rank(ev, now, self.switches)
                if cands != ev.candidates:
                    ev.candidates = cands
                    out.append(ev)
        return out

    def _process(self, samples: list[Sample], final: bool) -> LiveUpdate:
        update = LiveUpdate(samples=samples)
        changed: dict[int, LiveEvent] = {}
        for s in samples:
            self.now = s.ts
            if s.pv is not None:
                self._pv_ts.append(s.ts)
                self._pv_v.append(s.pv)
            if s.residual is None:
                continue
            if self.echoes is not None:
                self._subs.append(s.subs)
            points = self.hold.push(s.ts, s.residual, s.pv) if self.hold else [(s.ts, s.residual)]
            for ts, value in points:
                self._point(ts, value, s.ts, update, changed)
            self._release(s.ts, update, changed, final=False)
        if final:
            for ts, value in self.hold.flush() if self.hold else ():
                self._point(ts, value, self.now, update, changed)
            for st in self.stream.flush():
                self._handle(st, self.now, update, changed)
            self._release(self.now, update, changed, final=True)
        if self._pv_ts and self.now is not None:
            k = bisect.bisect_left(self._pv_ts, self.now - self.PV_KEEP_S)
            if k > 256:
                del self._pv_ts[:k]
                del self._pv_v[:k]
        if self.echoes is not None and self.now is not None:
            self.echoes.prune(self.now - self.ECHO_KEEP_S - 4 * self.stream.window_s)
        update.events = list(changed.values())
        return update

    def _point(self, ts: float, value: float, now: float, update: LiveUpdate, changed: dict) -> None:
        """One point for the step detector (a sample, once its solar doubt
        is known: they come in order, each once), and for the sub-meters'
        echoes with what the sub-meters took out of that sample."""
        if self.echoes is not None:
            self.echoes.add(ts, value, self._subs.popleft())
        for st in self.stream.push(ts, value):
            self._handle(st, now, update, changed)

    def _handle(self, st: LiveStep, now: float, update: LiveUpdate, changed: dict) -> None:
        if st.low:
            # a low step of the adaptive threshold (#88): kept apart as in
            # the night (no event, no explanation), only listed
            update.steps.append(st)
            return
        if st.state == CONFIRMED and self.echoes is not None:
            # the night's explanation, on the same steps in the same order;
            # held (still provisional for the feed) until it is final
            self.echoes.feed(st, self.stream.window_s, self.stream.merge_s)
            st.state = PROVISIONAL
            self._held.append(st)
            return
        self._forward(st, now, update, changed)

    def _release(self, now: float, update: LiveUpdate, changed: dict, final: bool) -> None:
        """Give the held steps whose explanation is final, in time order: a
        step a sub-meter explained (cancelled), or one no later echo can
        claim (confirmed): every step within its reach is known."""
        settled = self.stream.settled
        reach = self.echoes.reach(self.stream.merge_s) if self.echoes is not None else 0.0
        while self._held:
            st = self._held[0]
            if not (final or st.explained_by) and not (settled is not None and st.ts + reach < settled):
                break
            self._held.popleft()
            st.state = CANCELLED if st.explained_by else CONFIRMED
            self._forward(st, now, update, changed)

    def _forward(self, st: LiveStep, now: float, update: LiveUpdate, changed: dict) -> None:
        if st.state != CANCELLED:
            st.pv_moved = self._pv_moved(st.ts)
        if not any(x is st for x in update.steps):
            update.steps.append(st)
        for ev in self.pairer.feed(st):
            if ev.state != CANCELLED:
                ev.candidates = self.ranker.rank(ev, now, self.switches)
            changed[ev.id] = ev

    def _pv_quiet(self, ts: float) -> bool:
        return pv_quiet(self._pv_ts, self._pv_v, ts)

    def pv_quiet_now(self) -> bool | None:
        """PV produces nothing now (median of the last minute below
        ``PV_QUIET_W``); None without the night threshold or any PV yet. The
        minute after a step is not known yet: the panel's best guess."""
        if self.night_threshold_w is None or self.now is None:
            return None
        i = bisect.bisect_left(self._pv_ts, self.now - PV_QUIET_S)
        if i >= len(self._pv_ts):
            return None
        return statistics.median(self._pv_v[i:]) < PV_QUIET_W

    def _pv_moved(self, ts: float) -> bool:
        w = self.stream.window_s
        i = bisect.bisect_left(self._pv_ts, ts - w)
        j = bisect.bisect_left(self._pv_ts, ts)
        k = bisect.bisect_right(self._pv_ts, ts + w)
        if i >= j or j >= k:
            return False
        before = statistics.median(self._pv_v[i:j])
        after = statistics.median(self._pv_v[j:k])
        return abs(after - before) >= self.pv_moved_w


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
@dataclass
class ReplayResult:
    # the confirmed steps and those a sub-meter explained (``explained_by``,
    # cancelled), in order: those of report.find_steps
    steps: list[Step]
    updates: list[LiveStep]  # a copy of every step update, in order
    events: list[LiveEvent]  # the events at the end, by id
    samples: int
    interval_s: float | None


def _readings(histories: Mapping, entities: Sequence[str], end: float | None):
    out = []
    for e in dict.fromkeys(entities):
        for ts, state in histories.get(e, []):
            if end is None or float(ts) < end:
                out.append((float(ts), state, e))
    out.sort(key=lambda r: r[0])  # stable: each entity keeps its order
    return out


def night_interval(histories: Mapping, config) -> float | None:
    """The meter interval the night uses for this history: the median gap
    between the readings of the analysed load."""
    b = SampleBuilder.from_config(config)
    for ts, state, e in _readings(histories, b.entities, None):
        b.push(e, ts, state)
    return meter_interval([s.ts for s in b.flush() if s.residual is not None])


def replay(
    histories: Mapping[str, list[tuple[float, str]]],
    config,
    registry: Mapping | None = None,
    local_hour: Callable[[float], int] | None = None,
    end: float | None = None,
    interval_s: float | None | str = "night",
) -> ReplayResult:
    """Stream a stored day (the raw histories of :func:`report.analyze`)
    through the live pipeline, reading by reading in time order.

    ``interval_s``: ``"night"`` (default) sizes the windows as the night does
    on the same history, so that the confirmed steps are exactly those of
    the night; None estimates it on the fly, as live does.
    """
    if interval_s == "night":
        interval_s = night_interval(histories, config)
    det = LiveDetector(config, registry, local_hour, interval_s=interval_s)
    updates: list[LiveStep] = []
    samples = 0

    def collect(u: LiveUpdate):
        nonlocal samples
        samples += len(u.samples)
        updates.extend(replace(s) for s in u.steps)

    prev = None
    for ts, state, entity in _readings(histories, det.entities, end):
        if prev is not None and ts > prev:
            collect(det.advance(ts))  # every reading before ts is in
        det.push(entity, ts, state)
        prev = ts
    collect(det.flush())
    # in time order, as they are confirmed: an update lists a step where it
    # first appeared (provisional), and the samples a solar reading
    # releases together (#86) can confirm several steps in one update
    steps = sorted(
        (u.to_step() for u in updates if u.state == CONFIRMED or u.explained_by), key=lambda s: s.ts
    )
    events = [det.events[i] for i in sorted(det.events)]
    return ReplayResult(steps, updates, events, samples, interval_s)


__all__ = [
    "CandidateRanker",
    "LiveDetector",
    "LiveEvent",
    "LivePairer",
    "LiveUpdate",
    "ReplayResult",
    "SwitchChange",
    "confidence",
    "night_interval",
    "replay",
]
