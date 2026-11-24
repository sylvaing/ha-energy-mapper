"""Build the house load signal from Home Assistant state histories.

All functions work on plain lists of ``(timestamp, value)`` tuples so the
library has no dependency on Home Assistant and can be replayed offline.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

UNAVAILABLE = ("unavailable", "unknown", "", "none", "None")


def parse_numeric(states: Iterable[tuple[float, str]]) -> list[tuple[float, float | None]]:
    """Convert raw HA states to floats; non numeric states become ``None``."""
    out: list[tuple[float, float | None]] = []
    for ts, raw in states:
        try:
            out.append((float(ts), float(raw)))
        except (TypeError, ValueError):
            out.append((float(ts), None))
    out.sort(key=lambda x: x[0])
    return out


class StepSeries:
    """A piecewise-constant series: a value holds until the next state.

    HA only records a state when it changes, so "hold the last value" is the
    right interpolation. ``None`` means the source was unavailable.
    """

    def __init__(self, points: Sequence[tuple[float, float | None]]):
        self.points = list(points)
        self.ts = [p[0] for p in self.points]

    def value_at(self, ts: float) -> float | None:
        i = bisect.bisect_right(self.ts, ts) - 1
        if i < 0:
            return None
        return self.points[i][1]

    def last_valid_before(self, ts: float) -> tuple[float, float] | None:
        i = bisect.bisect_right(self.ts, ts) - 1
        while i >= 0:
            v = self.points[i][1]
            if v is not None:
                return self.points[i][0], v
            i -= 1
        return None

    def __bool__(self) -> bool:
        return bool(self.points)


@dataclass
class Sample:
    ts: float
    grid: float  # signed: + import, - export
    pv: float | None  # None: production unknown (inverter unavailable in daytime)
    battery: float  # + discharge (feeds the house), - charge
    submetered: float
    house: float | None  # grid + pv + battery, or the house meter's reading (#97)
    residual: float | None  # house - submetered: what the analysis looks at
    estimated: float = 0.0  # the part of ``submetered`` that is estimated
    # what each sub-meter took out of ``residual``, in the configured order
    # (its sum is ``submetered``): the step explanation of #85 reads it
    subs: tuple[float, ...] = ()


def pv_value(pv: StepSeries | None, ts: float, hold_s: float, night_w: float) -> float | None:
    """PV production at ``ts``.

    Solar inverters often drop out (timeouts) for a few seconds or minutes.
    Treating "unavailable" as 0 W would create fake steps of several kW, so
    the last valid value is held for ``hold_s`` seconds. Past that, the value
    is 0 if the inverter was already near zero (night) and unknown otherwise.
    """
    if pv is None or not pv:
        return 0.0
    v = pv.value_at(ts)
    if v is not None:
        return max(v, 0.0)
    last = pv.last_valid_before(ts)
    if last is None:
        return 0.0
    last_ts, last_v = last
    if last_v <= night_w:
        return 0.0
    if ts - last_ts <= hold_s:
        return last_v
    return None


def held_value(series: StepSeries, ts: float, hold_s: float) -> float:
    """Value of a sub-meter or battery: an unavailable sensor keeps its last
    value for ``hold_s`` seconds, then counts as 0 (and is reported)."""
    v = series.value_at(ts)
    if v is not None:
        return v
    last = series.last_valid_before(ts)
    if last is not None and ts - last[0] <= hold_s:
        return last[1]
    return 0.0


def scaled(points, factor: float = 1.0, invert: bool = False):
    """Apply a unit factor (kW -> W) and an optional sign inversion."""
    k = -factor if invert else factor
    return [(t, None if v is None else v * k) for t, v in points]


def build_samples(
    grid: Sequence[tuple[float, float | None]],
    pv: Sequence[tuple[float, float | None]] | None = None,
    submeters: Sequence[Sequence[tuple[float, float | None]]] = (),
    grid_export: Sequence[tuple[float, float | None]] | None = None,
    battery: Sequence[tuple[float, float | None]] | None = None,
    pv_hold_s: float = 300.0,
    pv_night_w: float = 50.0,
    hold_s: float = 300.0,
    estimated: Sequence[bool] = (),
    house: Sequence[tuple[float, float | None]] | None = None,
) -> list[Sample]:
    """One sample per grid reading (the grid meter drives the timeline).

    ``grid`` is signed (+ import), unless ``grid_export`` is given: then
    ``grid`` is the import and ``grid_export`` the (positive) export.
    ``battery`` is + when discharging into the house. All values in W.
    ``estimated``: for each sub-meter, whether it is an estimate (its power
    is also counted in ``Sample.estimated``).

    ``house``: the readings of a meter that measures the house itself (#97).
    It then drives the timeline and gives ``Sample.house``; grid, PV and
    battery are only read at its readings (held as the other meters), for
    the totals and the solar hours, and never enter the house value: meters
    polled at different instants make no false step.
    """
    pv_s = StepSeries(pv) if pv else None
    exp_s = StepSeries(grid_export) if grid_export else None
    bat_s = StepSeries(battery) if battery else None
    subs = [StepSeries(s) for s in submeters]
    if house is not None:
        grid_s = StepSeries(grid) if grid else None
        return [
            sample_at(
                ts,
                held_value(grid_s, ts, hold_s) if grid_s else 0.0,
                pv_s,
                exp_s,
                bat_s,
                subs,
                estimated,
                pv_hold_s,
                pv_night_w,
                hold_s,
                house=h,
            )
            for ts, h in house
            if h is not None
        ]
    timeline = list(grid)
    if exp_s:  # the timeline is the union of both meters' readings
        timeline = sorted(set(t for t, _ in grid) | set(exp_s.ts))
        imp_s = StepSeries(grid)
        timeline = [(t, imp_s.value_at(t)) for t in timeline]
    out: list[Sample] = []
    for ts, g in timeline:
        if g is None:
            continue
        out.append(sample_at(ts, g, pv_s, exp_s, bat_s, subs, estimated, pv_hold_s, pv_night_w, hold_s))
    return out


def sample_at(
    ts: float,
    g: float,
    pv_s,
    exp_s,
    bat_s,
    subs: Sequence,
    estimated: Sequence[bool],
    pv_hold_s: float,
    pv_night_w: float,
    hold_s: float,
    house: float | None = None,
) -> Sample:
    """The sample of one grid reading ``g`` at ``ts``: the rules of the held
    values (PV, battery, sub-meters), shared by the nightly
    :func:`build_samples` and the live :class:`SampleBuilder`. The series
    only need ``value_at`` and ``last_valid_before`` (:class:`StepSeries`).
    ``house``: the house meter's reading at ``ts`` (#97), the house value
    instead of grid + PV + battery; ``g`` is then the grid held at ``ts``."""
    if exp_s:
        e = exp_s.value_at(ts)
        g = g - (e or 0.0)
    p = pv_value(pv_s, ts, pv_hold_s, pv_night_w)
    b = held_value(bat_s, ts, hold_s) if bat_s else 0.0
    values = tuple(max(held_value(s, ts, hold_s), 0.0) for s in subs)
    sub = sum(values)
    est = sum(v for v, e in zip(values, estimated, strict=False) if e)
    if house is None:
        house = g + p + b if p is not None else None
    residual = house - sub if house is not None else None
    return Sample(ts, g, p, b, sub, house, residual, est, values)


def sum_series(
    series: Sequence[Sequence[tuple[float, float | None]] | None],
) -> list[tuple[float, float | None]]:
    """Sum several meters of the same kind (one per phase, several inverters)
    on the union of their timestamps. The sum is unknown while one is."""
    parts = [s for s in series if s]
    if len(parts) <= 1:
        return list(parts[0]) if parts else []
    steps = [StepSeries(p) for p in parts]
    out: list[tuple[float, float | None]] = []
    for t in sorted({t for p in parts for t, _ in p}):
        values = [s.value_at(t) for s in steps]
        if any(v is None for v in values):
            # before a meter's first reading, or while it is unavailable
            out.append((t, None))
        else:
            out.append((t, sum(values)))
    return out


# ---------------------------------------------------------------------------
# live: the same samples, built one reading at a time
# ---------------------------------------------------------------------------
def parse_state(raw) -> float | None:
    """One raw HA state as a float, ``None`` when not numeric (as :func:`parse_numeric`)."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class LiveSeries:
    """A :class:`StepSeries` that grows reading by reading and forgets its
    past (:meth:`prune`), keeping what the hold rules still need."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float | None]] = []
        self.ts: list[float] = []

    def add(self, ts: float, value: float | None) -> None:
        if not self.ts or ts >= self.ts[-1]:
            self.points.append((ts, value))
            self.ts.append(ts)
        else:  # a late reading: in place (after the readings of the same time)
            i = bisect.bisect_right(self.ts, ts)
            self.points.insert(i, (ts, value))
            self.ts.insert(i, ts)

    value_at = StepSeries.value_at
    last_valid_before = StepSeries.last_valid_before

    def __bool__(self) -> bool:
        return bool(self.points)

    def prune(self, cutoff: float) -> None:
        """Forget the readings before ``cutoff``, except the last one before
        it (the value held at ``cutoff``) and the last valid one (the hold
        rules look back to it, however old)."""
        i = bisect.bisect_left(self.ts, cutoff) - 1
        if i <= 0:
            return
        keep = self.points[i:]
        if keep[0][1] is None:
            last = self.last_valid_before(cutoff)
            if last is not None:
                keep.insert(0, last)
        self.points = keep
        self.ts = [p[0] for p in keep]


class _Combined:
    """The sum of meters of the same kind (:func:`sum_series`), built up to
    a time bound. One meter: the meter itself, every reading kept."""

    def __init__(self, n: int):
        self.parts = [LiveSeries() for _ in range(n)]
        self.single = n == 1
        self.out = self.parts[0] if self.single else LiveSeries()
        self._new: list[tuple[float, float | None]] = []  # single: readings not yet taken
        self._pending: set[float] = set()  # several: union timestamps not yet summed

    def add(self, i: int, ts: float, value: float | None) -> None:
        self.parts[i].add(ts, value)
        if self.single:
            self._new.append((ts, value))
        else:
            self._pending.add(ts)

    def advance(self, before: float | None) -> list[tuple[float, float | None]]:
        """The new points of the sum with a time before ``before`` (None: all)."""
        if self.single:
            self._new.sort(key=lambda p: p[0])  # stable: same-time readings keep their order
            k = len(self._new)
            if before is not None:
                k = bisect.bisect_left(self._new, before, key=lambda p: p[0])
            out, self._new = self._new[:k], self._new[k:]
            return out
        due = sorted(t for t in self._pending if before is None or t < before)
        self._pending.difference_update(due)
        # unknown before every meter's first reading, as sum_series (which
        # only leaves out a meter with no state at all, a missing entity:
        # the caller leaves it out of the config)
        out = []
        for t in due:
            values = [p.value_at(t) for p in self.parts]
            v = None if any(x is None for x in values) else sum(values)
            self.out.add(t, v)
            out.append((t, v))
        return out

    def prune(self, cutoff: float) -> None:
        for p in self.parts:
            p.prune(cutoff)
        if not self.single:
            self.out.prune(cutoff)


class SampleBuilder:
    """The samples of :func:`build_samples`, built from raw states pushed one
    at a time (the live view).

    A sample needs every meter's reading up to its time, and PV, battery and
    grid readings are not reported together: the caller pushes readings as
    they come (:meth:`push`) and takes the samples up to a bound
    (:meth:`pop`), a few seconds behind the clock in live, or the time of the
    next reading when replaying a sorted history. Fed with the same
    readings, it gives exactly the samples of :func:`build_samples` (except
    readings later than the bound, which are placed where they belong but
    do not change the samples already given).
    """

    KEEP_S = 600.0  # readings kept behind the bound, beyond the hold rules

    def __init__(
        self,
        grid: Sequence[str],
        pv: Sequence[str] = (),
        submeters: Sequence[str] = (),
        grid_export: str | None = None,
        battery: str | None = None,
        scales: dict[str, float] | None = None,
        grid_invert: bool = False,
        battery_invert: bool = False,
        estimated: Sequence[bool] = (),
        pv_hold_s: float = 300.0,
        pv_night_w: float = 50.0,
        hold_s: float = 300.0,
        house: str | None = None,
    ):
        scales = scales or {}
        self.grid = _Combined(len(grid))
        # the house meter (#97): it drives the timeline, as in build_samples
        self.house = _Combined(1) if house else None
        self.pv = _Combined(len(pv)) if pv else None
        self.exp = LiveSeries() if grid_export else None
        self.bat = LiveSeries() if battery else None
        self.subs = [LiveSeries() for _ in submeters]
        self.estimated = list(estimated)
        self.pv_hold_s, self.pv_night_w, self.hold_s = pv_hold_s, pv_night_w, hold_s
        # entity -> [(add(ts, value), factor)]; one entity may have several roles
        self._routes: dict[str, list[tuple[object, float]]] = {}

        def route(entity, add, invert=False):
            k = scales.get(entity, 1.0)
            self._routes.setdefault(entity, []).append((add, -k if invert else k))

        for i, e in enumerate(grid):
            route(e, lambda ts, v, i=i: self.grid.add(i, ts, v), grid_invert)
        for i, e in enumerate(pv):
            route(e, lambda ts, v, i=i: self.pv.add(i, ts, v))
        if grid_export:
            route(grid_export, self._add_export)
        if battery:
            route(battery, self.bat.add, battery_invert)
        for e, s in zip(submeters, self.subs, strict=True):
            route(e, s.add)
        if house:
            route(house, lambda ts, v: self.house.add(0, ts, v))
        self._export_ts: set[float] = set()

    @classmethod
    def from_config(cls, config, **kwargs) -> SampleBuilder:
        """From an ``AnalysisConfig``: the meters :func:`report.analyze` reads."""
        return cls(
            grid=[config.grid_entity, *config.extra_grid_entities],
            pv=[config.pv_entity, *config.extra_pv_entities] if config.pv_entity else (),
            submeters=[s.entity_id for s in config.submeters],
            grid_export=config.grid_export_entity,
            battery=config.battery_entity,
            scales=config.scales,
            grid_invert=config.grid_invert,
            battery_invert=config.battery_invert,
            estimated=[s.estimated for s in config.submeters],
            house=config.house_entity,
            **kwargs,
        )

    @property
    def entities(self) -> list[str]:
        return list(self._routes)

    def _add_export(self, ts: float, value: float | None) -> None:
        self.exp.add(ts, value)
        self._export_ts.add(ts)

    def push(self, entity_id: str, ts: float, state) -> None:
        """One raw HA state (a string, as recorded); other entities are ignored."""
        value = parse_state(state)
        for add, k in self._routes.get(entity_id, ()):
            add(float(ts), None if value is None else value * k)

    def pop(self, before: float | None = None) -> list[Sample]:
        """The samples of the grid readings (the house meter's, with one)
        before ``before`` (None: all), each one once, in time order."""
        if self.pv:
            self.pv.advance(before)
        grid = self.grid.advance(before)
        timeline: list[tuple[float, float | None, float | None]]  # (ts, grid, house)
        if self.house:
            # the house meter's readings drive the timeline (#97); the grid
            # is held at each of them, its own readings and the export's
            # make no sample
            if self.exp:
                self._export_ts = {t for t in self._export_ts if before is not None and t >= before}
            timeline = [
                (t, held_value(self.grid.out, t, self.hold_s), h)
                for t, h in self.house.advance(before)
                if h is not None
            ]
        elif self.exp:  # the timeline is the union of both meters' readings
            due = {t for t in self._export_ts if before is None or t < before}
            self._export_ts.difference_update(due)
            timeline = [(t, self.grid.out.value_at(t), None) for t in sorted(due | {t for t, _ in grid})]
        else:
            timeline = [(t, g, None) for t, g in grid]
        pv_s = self.pv.out if self.pv else None
        out = [
            sample_at(
                ts,
                g,
                pv_s,
                self.exp,
                self.bat,
                self.subs,
                self.estimated,
                self.pv_hold_s,
                self.pv_night_w,
                self.hold_s,
                h,
            )  # fmt: skip
            for ts, g, h in timeline
            if g is not None
        ]
        if out:
            self._prune(out[-1].ts - max(self.pv_hold_s, self.hold_s) - self.KEEP_S)
        return out

    def flush(self) -> list[Sample]:
        return self.pop(None)

    def _prune(self, cutoff: float) -> None:
        self.grid.prune(cutoff)
        if self.house:
            self.house.prune(cutoff)
        if self.pv:
            self.pv.prune(cutoff)
        for s in (self.exp, self.bat, *self.subs):
            if s is not None:
                s.prune(cutoff)
