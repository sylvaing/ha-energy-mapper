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
    house: float | None  # grid + pv + battery
    residual: float | None  # house - submetered: what the analysis looks at


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
) -> list[Sample]:
    """One sample per grid reading (the grid meter drives the timeline).

    ``grid`` is signed (+ import), unless ``grid_export`` is given: then
    ``grid`` is the import and ``grid_export`` the (positive) export.
    ``battery`` is + when discharging into the house. All values in W.
    """
    pv_s = StepSeries(pv) if pv else None
    exp_s = StepSeries(grid_export) if grid_export else None
    bat_s = StepSeries(battery) if battery else None
    subs = [StepSeries(s) for s in submeters]
    timeline = list(grid)
    if exp_s:  # the timeline is the union of both meters' readings
        timeline = sorted(set(t for t, _ in grid) | set(exp_s.ts))
        imp_s = StepSeries(grid)
        timeline = [(t, imp_s.value_at(t)) for t in timeline]
    out: list[Sample] = []
    for ts, g in timeline:
        if g is None:
            continue
        if exp_s:
            e = exp_s.value_at(ts)
            g = g - (e or 0.0)
        p = pv_value(pv_s, ts, pv_hold_s, pv_night_w)
        b = held_value(bat_s, ts, hold_s) if bat_s else 0.0
        sub = sum(max(held_value(s, ts, hold_s), 0.0) for s in subs)
        house = g + p + b if p is not None else None
        residual = house - sub if house is not None else None
        out.append(Sample(ts, g, p, b, sub, house, residual))
    return out


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
