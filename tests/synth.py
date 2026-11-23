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


def solar_drift_day(
    day0: float = DAY0,
    dt: float = 10.0,
    pv_peak_w: float = 3000.0,
    drift_w: float = 180.0,
    big_w: float = 2000.0,
    evening_w: float = 170.0,
    noise_w: float = 10.0,
    seed: int = 7,
    grid: str = "sensor.grid_power",
    pv: str = "sensor.pv_power",
) -> dict[str, list[tuple[float, str]]]:
    """A solar day for the night threshold (#77), from 21:00 the day before.

    * base load 250 W; PV from 08:00 to 18:00 (``pv_peak_w`` at 13:00);
    * from 11:05 to 14:05 the grid meter drifts from the PV meter by
      ``drift_w`` every other 10 minutes: small fake steps of the house load;
    * a large load (``big_w``) from 12:30 to 12:50, during PV;
    * a small evening load (``evening_w``) from 20:00 to 20:25.

    The PV meter reports 5 s after the grid meter.
    """
    rnd = random.Random(seed)
    house = House(base_w=250.0, pv_peak_w=pv_peak_w, noise_w=0.0)
    big = Steady(day0 + 12.5 * HOUR, day0 + 12.5 * HOUR + 1200, big_w)
    evening = Steady(day0 + 20 * HOUR, day0 + 20 * HOUR + 1500, evening_w)

    def drift(t: float) -> float:
        k = (t - (day0 + 11 * HOUR + 300)) // 600
        if not day0 + 11 * HOUR + 300 <= t < day0 + 14 * HOUR + 300:
            return 0.0
        return drift_w if k % 2 == 0 else 0.0

    g, p = [], []
    t = day0 - 3 * HOUR
    while t < day0 + 24 * HOUR:
        use = house.base_w + big.power_at(t) + evening.power_at(t)
        g.append((t, f"{use - house.pv_at(t) + drift(t) + rnd.gauss(0, noise_w):.1f}"))
        tp = t + 5
        p.append((tp, f"{house.pv_at(tp):.0f}"))
        t += dt
    return {grid: g, pv: p}


def cloudy_day(
    day0: float = DAY0,
    dt: float = 10.0,
    pv_dt: float = 30.0,
    pv_lag_s: float = 0.0,
    pv_peak_w: float = 3000.0,
    loads: tuple = (),
    clouds: bool = True,
    noise_w: float = 10.0,
    seed: int = 5,
    grid: str = "sensor.grid_power",
    pv: str = "sensor.pv_power",
) -> tuple[dict[str, list[tuple[float, str]]], list[tuple[float, float]]]:
    """A day of passing clouds seen by a slow solar meter (#86), from 21:00
    the day before: the histories and the clouds' edges ``(time, watts)``.

    * base load 250 W plus ``loads``; PV from 08:00 to 18:00;
    * from 09:00 to 17:00 a cloud every 4 to 15 minutes takes 30 % to 80 %
      of the production for 1 to 5 minutes, its edges lasting 4 to 15 s;
    * the grid meter reports every ``dt`` and follows the clouds at once;
    * the solar meter reports every ``pv_dt`` (10 W resolution, and only a
      value that changed, as Home Assistant records it), ``pv_lag_s``
      late: each cloud edge is first a step of the house load, cancelled
      when the solar reading comes.
    """
    rnd = random.Random(seed)
    house = House(base_w=250.0, pv_peak_w=pv_peak_w, noise_w=0.0)
    passes = []  # (start, end, depth, edge)
    t = day0 + 9 * HOUR
    while clouds:
        t += rnd.uniform(240, 900)
        length = rnd.uniform(60, 300)
        if t + length > day0 + 17 * HOUR:
            break
        passes.append((t, t + length, rnd.uniform(0.3, 0.8), rnd.uniform(4, 15)))
        t += length

    def production(t: float) -> float:
        clear = house.pv_at(t)
        for a, b, depth, edge in passes:
            if a <= t < b:
                shade = min(1.0, (t - a) / edge, (b - t) / edge)
                return clear * (1 - depth * shade)
        return clear

    g, p = [], []
    t = day0 - 3 * HOUR
    while t < day0 + 24 * HOUR:
        use = house.base_w + sum(load.power_at(t) for load in loads)
        g.append((t, f"{use - production(t) + rnd.gauss(0, noise_w):.1f}"))
        t += dt
    last = None
    t = day0 - 3 * HOUR + 3
    while t < day0 + 24 * HOUR:
        value = f"{round(production(t - pv_lag_s), -1):.0f}"
        if value != last:
            p.append((t, value))
            last = value
        t += pv_dt
    edges = []
    for a, b, depth, _edge in passes:
        edges += [(a, depth * house.pv_at(a)), (b, -depth * house.pv_at(b))]
    return {grid: g, pv: p}, edges


def plug_history(
    power_at,
    start: float,
    end: float,
    every_s: float = 20.0,
    lag_s: tuple[float, float] = (0.0, 25.0),
    seed: int = 1,
) -> list[tuple[float, str]]:
    """Readings of a smart plug measuring ``power_at(t)`` (#85).

    The plug reads its appliance every ``every_s`` on its own clock, and each
    reading reaches Home Assistant after a lag drawn in ``lag_s`` (negative:
    before the grid meter shows the change). Only changes are recorded, as
    Home Assistant does: a switching is seen up to ``every_s + lag_s[1]``
    after it happened.
    """
    rnd = random.Random(seed)
    out: list[tuple[float, str]] = []
    last = None
    t = start + rnd.uniform(0, every_s)
    while t < end:
        v = power_at(t)
        if v != last:
            out.append((t + rnd.uniform(*lag_s), f"{v:.1f}"))
            last = v
        t += every_s
    out.sort(key=lambda r: r[0])
    return out


def small_loads_day(
    day0: float = DAY0,
    dt: float = 10.0,
    fridge_w: float = 80.0,
    quiet_w: float = 4.0,
    solar_noise: float = 0.0,
    pv_peak_w: float = 0.0,
    kettle_w: float = 2000.0,
    seed: int = 3,
    grid: str = "sensor.grid_power",
    pv: str = "sensor.pv_power",
) -> dict[str, list[tuple[float, str]]]:
    """A day for the noise-adaptive threshold (#88), from 21:00 the day
    before.

    * base load 250 W, noise ``quiet_w``;
    * a fridge (``fridge_w``) on for 20 minutes every hour, all day;
    * a kettle (``kettle_w``) for 4 minutes at 07:30 and 12:30;
    * with ``pv_peak_w``, PV from 08:00 to 18:00, both meters reporting
      together, and a grid reading that is off by ``solar_noise`` x the
      production (standard deviation): a changing sky, the noisier the more
      the panels produce.
    """
    rnd = random.Random(seed)
    house = House(base_w=250.0, pv_peak_w=pv_peak_w, noise_w=0.0)
    loads = [
        Cycling(day0 - 3 * HOUR, day0 + 24 * HOUR, fridge_w, 1200.0, 2400.0),
        Steady(day0 + 7.5 * HOUR, day0 + 7.5 * HOUR + 240, kettle_w),
        Steady(day0 + 12.5 * HOUR, day0 + 12.5 * HOUR + 240, kettle_w),
    ]
    g, p = [], []
    t = day0 - 3 * HOUR
    while t < day0 + 24 * HOUR:
        prod = house.pv_at(t)
        use = house.base_w + sum(x.power_at(t) for x in loads)
        noise = rnd.gauss(0, quiet_w) + rnd.gauss(0, solar_noise * prod)
        g.append((t, f"{use - prod + noise:.1f}"))
        if pv_peak_w:
            p.append((t, f"{prod:.0f}"))
        t += dt
    return {grid: g, pv: p} if pv_peak_w else {grid: g}


@dataclass
class Compressor:
    """A cold appliance (#89): it starts at ``on_w``, decays to ``off_w``
    over the run, pauses, and starts again. Each pause is the usual one
    within ``jitter`` (a share of it), drawn from ``seed``. ``long_every``:
    every that many runs, one lasts ``long_s``. ``skip``: the runs (by
    index) that do not happen."""

    start: float
    end: float
    on_w: float
    off_w: float
    on_s: float
    off_s: float
    jitter: float = 0.03
    seed: int = 7
    long_every: int = 0
    long_s: float = 0.0
    skip: tuple[int, ...] = ()
    runs: list[tuple[float, float]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        rnd = random.Random(self.seed)
        t = self.start + rnd.uniform(0, self.off_s)
        k = 0
        while t < self.end:
            length = (
                self.long_s if self.long_every and k % self.long_every == self.long_every - 1 else self.on_s
            )
            if k not in self.skip:
                self.runs.append((t, t + length))
            t += length + self.off_s * (1 + rnd.uniform(-self.jitter, self.jitter))
            k += 1
        self._starts = [a for a, _ in self.runs]

    def power_at(self, t: float) -> float:
        import bisect

        i = bisect.bisect_right(self._starts, t) - 1
        if i < 0 or t >= self.runs[i][1]:
            return 0.0
        a, b = self.runs[i]
        return self.on_w + (self.off_w - self.on_w) * (t - a) / (b - a)

    def energy_kwh(self, start: float, end: float) -> float:
        """The energy of its runs within ``[start, end)``."""
        total = 0.0
        for a, b in self.runs:
            x, y = max(a, start), min(b, end)
            if y > x:
                total += (self.power_at(x) + self.power_at(y - 1e-6)) / 2 * (y - x)
        return total / 3.6e6


def cold_day(
    loads: list,
    day0: float = DAY0,
    dt: float = 10.0,
    quiet_w: float = 4.0,
    solar_noise: float = 0.0,
    pv_peak_w: float = 0.0,
    busy: tuple[tuple[float, float, float], ...] = (),
    seed: int = 5,
    grid: str = "sensor.grid_power",
    pv: str = "sensor.pv_power",
) -> dict[str, list[tuple[float, str]]]:
    """A day for the cold appliances (#89), from 21:00 the day before: base
    load 250 W, noise ``quiet_w``, the ``loads`` (anything with
    ``power_at``). ``busy``: ``(from hour, to hour, noise W)``, hours of the
    day when the house is busy (a noise of its own on the curve). With
    ``pv_peak_w``, PV from 08:00 to 18:00 and a grid reading off by
    ``solar_noise`` x the production (a changing sky)."""
    rnd = random.Random(seed)
    house = House(base_w=250.0, pv_peak_w=pv_peak_w, noise_w=0.0)
    g, p = [], []
    t = day0 - 3 * HOUR
    while t < day0 + 24 * HOUR:
        prod = house.pv_at(t)
        use = house.base_w + sum(x.power_at(t) for x in loads)
        h = (t - day0) / HOUR
        extra = next((w for a, b, w in busy if a <= h < b), 0.0)
        noise = rnd.gauss(0, quiet_w) + rnd.gauss(0, solar_noise * prod) + rnd.gauss(0, extra)
        g.append((t, f"{use - prod + noise:.1f}"))
        if pv_peak_w:
            p.append((t, f"{prod:.0f}"))
        t += dt
    return {grid: g, pv: p} if pv_peak_w else {grid: g}
