"""How well the meters let the curve be read: their reporting intervals and
the noise of the residual load.

The noise is what decides the smallest appliance the whole-house curve can
show, whatever the configured thresholds: a 70 W compressor stands out of a
curve that moves by 3 W at night, and does not under a changing sky. It is
measured with the detector's own statistic (:func:`~.steps.detect_steps`:
the difference between the medians of the readings after and before an
instant), at regular instants, where no appliance switches most of the time;
its robust spread is the noise. The nightly report carries it
(``quality.meters``) and the benchmark (``tools/benchmark.py``) reads it
from there.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise

from .steps import (
    MERGE_S,
    NOISE,
    PV_QUIET_S,
    PV_QUIET_W,
    ROBUST_SIGMA,
    WINDOW_S,
    NoiseConfig,
    Signal,
    adapted_windows,
    meter_interval,
)

NOISE_EVERY_S = 60.0  # the noise is sampled at these instants
MIN_INSTANTS = 30  # under this many, no noise is given
SOLAR_GAP_S = 3600.0  # a longer silence of the solar meter is a night, not an interval


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1)))]


def intervals(ts: Sequence[float], max_gap_s: float | None = None) -> dict | None:
    """The median and the 9th decile of the gaps between ``ts`` (those of
    ``max_gap_s`` or more left out); None without any."""
    gaps = [b - a for a, b in pairwise(ts) if max_gap_s is None or b - a < max_gap_s]
    if not gaps:
        return None
    return {"median_s": round(statistics.median(gaps), 1), "p90_s": round(_quantile(gaps, 0.9), 1)}


def robust_sigma(values: Sequence[float], min_values: int = MIN_INSTANTS) -> float | None:
    """``ROBUST_SIGMA`` x the median absolute value: the standard deviation
    of the noise, whatever the appliances that switch (the outliers). None
    under ``min_values`` values."""
    if len(values) < min_values:
        return None
    return round(ROBUST_SIGMA * statistics.median(abs(v) for v in values), 1)


def meter_quality(
    samples: Iterable,
    pv: Sequence[tuple[float, float | None]] | None,
    start: float,
    end: float,
    noise: NoiseConfig = NOISE,
) -> dict:
    """How the meters report within ``[start, end)`` and the noise of the
    residual load.

    ``samples``: those of the analysis (:func:`~.signal.build_samples`);
    ``pv``: the readings of the solar meter in W, None without one.

    - ``grid`` / ``solar``: ``{median_s, p90_s}`` of the gaps between the
      samples, and between the solar readings while the panels produce
      (above ``PV_QUIET_W``; None when they never do);
    - ``window_s``: the detector's window on these samples;
    - ``noise_w``: ``{no_solar, solar}``, the robust spread of the detector's
      statistic at regular instants, apart from the solar hours (production
      above ``PV_QUIET_W`` around the instant) and during them; None where
      there are too few instants. Without a solar meter everything is
      ``no_solar``;
    - ``visible_w``: the smallest step the curve can show at its quietest,
      ``max(floor, k x noise)`` (:class:`~.steps.NoiseConfig`) with the
      lowest of the two noises; None when no noise is known.
    """
    period = [s for s in samples if start <= s.ts < end]
    points = [(s.ts, s.residual) for s in period if s.residual is not None]
    out: dict = {"grid": intervals([s.ts for s in period]), "solar": None, "noise_w": {}}
    if pv is not None:
        producing = [t for t, v in pv if start <= t < end and v is not None and v > PV_QUIET_W]
        # the nights between two productions are not reporting intervals
        out["solar"] = intervals(producing, SOLAR_GAP_S)
    if len(points) < 3:
        return out
    sig = Signal(points)
    win, _ = adapted_windows(meter_interval(sig.ts), WINDOW_S, MERGE_S)
    production = Signal([(s.ts, s.pv) for s in period if s.pv is not None])
    quiet: list[float] = []
    sunny: list[float] = []
    t = start + win
    while t < end - win:
        before = sig.median(t - win, t - 1e-3)
        after = sig.median(t, t + win)
        if before is not None and after is not None:
            around = production.median(t - PV_QUIET_S, t + PV_QUIET_S) if pv is not None else 0.0
            if around is not None:
                (sunny if around > PV_QUIET_W else quiet).append(after - before)
        t += NOISE_EVERY_S
    out["window_s"] = round(win, 1)
    out["noise_w"] = {"no_solar": robust_sigma(quiet), "solar": robust_sigma(sunny)}
    known = [v for v in out["noise_w"].values() if v is not None]
    out["visible_w"] = round(noise.threshold(min(known)), 1) if known else None
    return out


def visible_w(reports: Iterable[Mapping | None]) -> float | None:
    """The smallest step the curve of these daily reports can show: the
    median of their ``quality.meters.visible_w`` (None when none has it:
    reports written before it was measured)."""
    values = [
        v
        for r in reports
        if (v := (((r or {}).get("quality") or {}).get("meters") or {}).get("visible_w")) is not None
    ]
    return statistics.median(values) if values else None
