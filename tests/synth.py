"""Synthetic households for the tests.

The repository never contains real consumption data: every test builds its
own day from simple, explicit appliances.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

DAY0 = 1_800_000_000.0  # an arbitrary epoch, aligned for readability
HOUR = 3600.0


@dataclass
class Steady:
    start: float
    end: float
    power: float

    def power_at(self, t: float) -> float:
        return self.power if self.start <= t < self.end else 0.0


@dataclass
class Cycling:
    start: float
    end: float
    power: float
    on_s: float
    off_s: float

    def power_at(self, t: float) -> float:
        if not self.start <= t < self.end:
            return 0.0
        phase = (t - self.start) % (self.on_s + self.off_s)
        return self.power if phase < self.on_s else 0.0


@dataclass
class House:
    base_w: float = 250.0
    loads: list = field(default_factory=list)
    pv_peak_w: float = 0.0  # 0 = no solar
    noise_w: float = 15.0
    seed: int = 1

    def pv_at(self, t: float) -> float:
        if not self.pv_peak_w:
            return 0.0
        h = (t - DAY0) / HOUR % 24
        if not 8 <= h <= 18:
            return 0.0
        return self.pv_peak_w * math.sin(math.pi * (h - 8) / 10)

    def consumption_at(self, t: float) -> float:
        return self.base_w + sum(load.power_at(t) for load in self.loads)

    def histories(
        self,
        start: float,
        end: float,
        dt: float = 10.0,
        grid="sensor.grid_power",
        pv="sensor.pv_power",
        pv_dropouts: tuple[tuple[float, float], ...] = (),
        kw: bool = False,
        split_import_export: bool = False,
    ) -> dict[str, list[tuple[float, str]]]:
        """Readings as HA would record them (strings, 'unavailable')."""
        rnd = random.Random(self.seed)
        g, p, gi, ge = [], [], [], []
        t = start
        scale = 0.001 if kw else 1.0
        while t < end:
            prod = self.pv_at(t)
            net = self.consumption_at(t) - prod + rnd.gauss(0, self.noise_w)
            g.append((t, f"{net * scale:.3f}"))
            gi.append((t, f"{max(net, 0):.1f}"))
            ge.append((t, f"{max(-net, 0):.1f}"))
            if self.pv_peak_w:
                if any(a <= t < b for a, b in pv_dropouts):
                    p.append((t, "unavailable"))
                else:
                    p.append((t, f"{prod:.0f}"))
            t += dt
        out = {}
        if split_import_export:
            out[grid] = gi
            out[grid + "_export"] = ge
        else:
            out[grid] = g
        if self.pv_peak_w:
            out[pv] = p
        return out


def switch_history(periods, entity_states=("on", "off")):
    """State history of a relay: on during each (start, end) period."""
    on, off = entity_states
    out = []
    for a, b in periods:
        out.append((a, on))
        out.append((b, off))
    return out
