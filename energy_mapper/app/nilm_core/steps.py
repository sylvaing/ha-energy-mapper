"""Step detection: an appliance switching on or off is a step in the load."""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise


@dataclass
class Step:
    ts: float
    delta: float  # W, positive = something switched on
    before: float
    after: float
    explained_by: str | None = None  # name of a known load, if any
    used: bool = False  # consumed by an activation


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
    window_s: float = 20.0,
    merge_s: float = 15.0,
) -> list[Step]:
    """Find level changes larger than ``threshold``.

    For each reading that jumps from the previous one, the median of the
    readings in the ``window_s`` before and after is compared (the windows
    grow with the meter's median reporting interval). Medians make
    the detector robust to single-sample spikes (motor inrush, meter noise).
    Neighbouring candidates of the same sign are merged, keeping the largest.
    """
    sig = Signal(points)
    # adapt the windows to the meter's reporting interval (1 s to 60 s meters)
    if len(sig.ts) > 2:
        dt = statistics.median(b - a for a, b in pairwise(sig.ts))
        window_s = max(window_s, 3.1 * dt)  # at least 3 readings per side
        merge_s = max(merge_s, 1.1 * dt)
    cand: list[Step] = []
    for i in range(1, len(sig.ts)):
        if abs(sig.v[i] - sig.v[i - 1]) < threshold * 0.6:
            continue
        t = sig.ts[i]
        before = sig.median(t - window_s, t - 1e-3)
        after = sig.median(t, t + window_s)
        if before is None or after is None:
            continue
        d = after - before
        if abs(d) >= threshold:
            cand.append(Step(t, d, before, after))
    out: list[Step] = []
    for c in cand:
        if out and c.ts - out[-1].ts < merge_s and (c.delta > 0) == (out[-1].delta > 0):
            if abs(c.delta) > abs(out[-1].delta):
                out[-1] = c
        else:
            out.append(c)
    return out
