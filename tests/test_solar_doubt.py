"""The solar doubt (#86): a solar reading that comes late makes a cloud look
like an appliance. The readings whose production is in doubt are set aside.
Synthetic days only (no real data)."""

from __future__ import annotations

import datetime as dt
import pathlib
import random
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from energy_config import slow_solar_meter  # noqa: E402
from nilm_core import AnalysisConfig, analyze, detect_steps  # noqa: E402
from nilm_core.live import LiveDetector, replay  # noqa: E402
from nilm_core.report import find_steps, solar_interval  # noqa: E402
from nilm_core.steps import (  # noqa: E402
    CONFIRMED,
    PV_LATE_S,
    PvQuiet,
    SolarHold,
    hold_doubtful,
    solar_doubt_limit,
)
from synth import DAY0, HOUR, Steady, cloudy_day, solar_drift_day  # noqa: E402
from test_live import jitter, key, night_samples, with_gap  # noqa: E402

GRID = "sensor.grid_power"
PV = "sensor.pv_power"
HOUR_OF = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731
SOLAR = AnalysisConfig(grid_entity=GRID, pv_entity=PV)


def raw_points(hist, cfg=SOLAR):
    return [(s.ts, s.residual) for s in night_samples(hist, cfg) if s.residual is not None]


def steps_of(hist, cfg=SOLAR, held=None):
    return find_steps(night_samples(hist, cfg), cfg, held)


def daytime(steps):
    return [s for s in steps if 8 * HOUR <= s.ts - DAY0 <= 18 * HOUR]


def hours(steps):
    return [(round((s.ts - DAY0) / HOUR, 2), round(s.delta)) for s in steps]


def stable(report: dict) -> dict:
    """A report without what changes from one run to the next."""
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


def loads_near_clouds(seed: int, near_s: float = 20.0, **day) -> list[Steady]:
    """Appliances of 400 W to 2200 W for 5 to 15 minutes, each switched on
    within ``near_s`` of a cloud edge of the day ``seed``."""
    rnd = random.Random(1000 + seed)
    _, edges = cloudy_day(seed=seed, **day)
    out = []
    t = DAY0 + 9.2 * HOUR
    while t < DAY0 + 16.5 * HOUR:
        edge = min(edges, key=lambda e: abs(e[0] - t))[0]
        t = edge + rnd.uniform(-near_s, near_s)
        length = rnd.uniform(300, 900)
        out.append(Steady(t, t + length, rnd.choice([400, 600, 900, 1500, 2200])))
        t += length + rnd.uniform(600, 1200)
    return out


def switches_found(steps, loads) -> tuple[int, int, int]:
    """``(found, switches, other steps)``: a switch is found when a daytime
    step of its size (20 %) comes from 15 s before to 2 minutes after it."""
    day = daytime(steps)
    used: set[int] = set()
    found = total = 0
    for load in loads:
        for t, delta in ((load.start, load.power), (load.end, -load.power)):
            total += 1
            near = [
                s
                for s in day
                if id(s) not in used and -15 <= s.ts - t <= 120 and abs(s.delta - delta) <= 0.2 * abs(delta)
            ]
            if near:
                used.add(id(min(near, key=lambda s: abs(s.ts - t))))
                found += 1
    return found, total, len(day) - len(used)


class SolarHoldTest(unittest.TestCase):
    def test_nothing_waits_while_the_panels_produce_nothing(self):
        hold = SolarHold(150)
        for i in range(5):
            self.assertEqual(hold.push(DAY0 + 10 * i, 300.0 + i, 0.0), [(DAY0 + 10 * i, 300.0 + i)])
        self.assertEqual((hold.pending, hold.count), (0, 0))

    def test_a_reading_waits_for_the_production_to_change(self):
        hold = SolarHold(150)
        self.assertEqual(hold.push(DAY0, 300, 1000), [])
        self.assertEqual(hold.push(DAY0 + 10, 310, 1000), [])
        self.assertEqual(hold.pending, 2)
        # a small change: both are trusted
        self.assertEqual(hold.push(DAY0 + 20, 305, 1010), [(DAY0, 300), (DAY0 + 10, 310)])
        self.assertEqual(hold.flush(), [(DAY0 + 20, 305)])
        self.assertEqual(hold.count, 0)

    def test_a_late_solar_reading_is_set_aside(self):
        # a cloud takes 600 W at 40 s: the grid shows it at once, the solar
        # reading comes at 70 s. The house load (grid + solar) shows 900 W
        # in between; the detector gets the level before instead
        held: list[float] = []
        hold = SolarHold(150, held=held)
        rows = [(0, 300, 990), (10, 304, 1000), (20, 296, 1010), (30, 302, 1000)]
        rows += [(40, 900, 1000), (50, 905, 1000), (60, 895, 1000), (70, 300, 400), (80, 302, 400)]
        out = []
        for t, value, pv in rows:
            out += hold.push(DAY0 + t, value, pv)
        out += hold.flush()
        self.assertEqual([t - DAY0 for t, _ in out], [t for t, _, _ in rows])  # every reading, in order
        # the readings since the last solar value are in doubt by 600 W:
        # they are given the median of the three trusted ones before
        self.assertEqual([v for _, v in out], [300, 304, 296, 300, 300, 300, 300, 300, 302])
        self.assertEqual([t - DAY0 for t in held], [30, 40, 50, 60])
        self.assertEqual(hold.count, 4)

    def test_the_level_before_the_doubt_is_held(self):
        hold = SolarHold(150)
        out = []
        # trusted readings (the production moves by 10 W each time), then
        # the grid follows a cloud the solar meter has not seen yet
        for i, value in enumerate([300, 310, 2000, 305]):  # 2000: a spike
            out += hold.push(DAY0 + 10 * i, value, 1000 + 10 * i)
        for i in range(4, 8):
            out += hold.push(DAY0 + 10 * i, 1100, 1030)
        out += hold.push(DAY0 + 80, 310, 230)  # the solar reading at last
        out += hold.flush()
        values = [v for _, v in out]
        self.assertEqual(values[:3], [300, 310, 2000])
        # the last trusted reading (305) is itself in doubt (its next
        # production is 800 W away); the level held is the median of the
        # three trusted ones, not the spike
        self.assertEqual(values[3:8], [310] * 5)
        self.assertEqual(values[8], 310)

    def test_only_a_change_within_the_minute_counts(self):
        hold = SolarHold(150)
        self.assertEqual(hold.push(DAY0, 300, 1000), [])
        # released by the first reading later than PV_LATE_S, trusted
        self.assertEqual(hold.push(DAY0 + PV_LATE_S + 1, 900, 200), [(DAY0, 300)])
        self.assertEqual(hold.count, 0)
        # exactly at the limit, the change still counts
        hold = SolarHold(150)
        hold.push(DAY0 - 10, 310, 1005)
        self.assertEqual(hold.push(DAY0, 300, 1000), [(DAY0 - 10, 310)])
        self.assertEqual(hold.push(DAY0 + PV_LATE_S, 900, 200), [(DAY0, 310)])  # held
        self.assertEqual(hold.count, 1)

    def test_nothing_trusted_yet(self):
        hold = SolarHold(150)
        hold.push(DAY0, 900, 1000)
        self.assertEqual(hold.push(DAY0 + 10, 300, 400), [(DAY0, 900)])  # as it is
        self.assertEqual(hold.count, 1)

    def test_whole_series_equals_the_stream(self):
        hist, _ = cloudy_day(seed=3)
        samples = [s for s in night_samples(hist, SOLAR) if s.residual is not None]
        points = [(s.ts, s.residual) for s in samples]
        pv = [s.pv for s in samples]
        held: list[float] = []
        whole = hold_doubtful(points, pv, 150, held)
        self.assertEqual([t for t, _ in whole], [t for t, _ in points])  # same readings, same order
        self.assertGreater(len(held), 100)
        self.assertEqual(sum(1 for a, b in zip(points, whole, strict=True) if a != b), len(held))
        self.assertEqual(solar_doubt_limit(300), 150)


class CloudyDayTest(unittest.TestCase):
    """A cloudy day seen by a slow solar meter (a new value every 30 s)."""

    def test_clouds_make_no_step(self):
        for name, day in (
            ("slow", {}),
            ("slow and 5 s late", {"pv_lag_s": 5.0}),
            ("every 60 s", {"pv_dt": 60.0}),
        ):
            for seed in range(3):
                with self.subTest(meter=name, seed=seed):
                    hist, edges = cloudy_day(seed=seed, **day)
                    self.assertGreater(len(edges), 50)
                    # the test has teeth: without the rule, the clouds are steps
                    self.assertGreater(len(daytime(detect_steps(raw_points(hist), threshold=300))), 60)
                    held: list[float] = []
                    steps = daytime(steps_of(hist, held=held))
                    self.assertEqual(hours(steps), [])
                    self.assertGreater(len(held), 100)

    def test_appliance_switching_during_a_cloud_is_found(self):
        # 6 days, 112 appliances of 400 W to 2200 W switched on within 20 s
        # of a cloud edge: 222 of their 224 switches are found (99 %; 95 %
        # with a solar meter every 60 s), against 65 % without the rule,
        # where the cloud changes the size of the step
        found = total = found_raw = other = 0
        for seed in range(6):
            loads = loads_near_clouds(seed)
            hist, _ = cloudy_day(seed=seed, loads=tuple(loads))
            f, n, extra = switches_found(steps_of(hist), loads)
            found, total, other = found + f, total + n, other + extra
            found_raw += switches_found(detect_steps(raw_points(hist), threshold=300), loads)[0]
        self.assertGreater(total, 200)
        self.assertGreaterEqual(found / total, 0.97, (found, total))
        self.assertLess(found_raw / total, 0.8, (found_raw, total))
        self.assertLessEqual(other, 3)  # steps that are no appliance, on 6 days

    def test_the_analysis_finds_the_appliance(self):
        _, edges = cloudy_day(seed=2)
        t_on = min(edges, key=lambda e: abs(e[0] - (DAY0 + 12 * HOUR)))[0] + 5  # 5 s into a cloud edge
        load = Steady(t_on, t_on + 1200, 900)
        hist, _ = cloudy_day(seed=2, loads=(load,))
        report = analyze(hist, SOLAR, DAY0, DAY0 + 24 * HOUR, local_hour=HOUR_OF)
        (a,) = [a for a in report["activations"] if a["start"] > DAY0 + 8 * HOUR]
        self.assertAlmostEqual(a["power_w"], 900, delta=60)
        self.assertAlmostEqual(a["start"], t_on, delta=90)
        self.assertAlmostEqual(a["end"] - a["start"], 1200, delta=120)
        self.assertEqual(report["quality"]["solar_interval_s"], 30.0)
        self.assertGreater(report["quality"]["solar_held"], 100)

    def test_night_threshold_still_applies(self):
        evening = Steady(DAY0 + 20 * HOUR, DAY0 + 20 * HOUR + 1500, 170)
        hist, _ = cloudy_day(seed=4, loads=(evening,))
        cfg = AnalysisConfig(grid_entity=GRID, pv_entity=PV, night_step_threshold_w=150)
        steps = steps_of(hist, cfg)
        self.assertEqual([h[0] for h in hours(steps)], [20.0, 20.42])
        self.assertAlmostEqual(steps[0].delta, 170, delta=25)
        # and the day of #77 (meter drift, no cloud) gives the steps it gave
        drift = solar_drift_day()
        samples = night_samples(drift, cfg)
        self.assertEqual([h[0] for h in hours(find_steps(samples, cfg))], [12.5, 12.83, 20.0, 20.42])
        self.assertEqual(
            key(find_steps(samples, cfg)),
            key(
                detect_steps(
                    raw_points(drift, cfg),
                    300,
                    low_threshold=150,
                    low_ok=PvQuiet([(s.ts, s.pv) for s in samples]),
                )
            ),
        )

    def test_clear_sky_changes_nothing(self):
        # a fast solar meter and no cloud: nothing is in doubt, the steps
        # are those of the plain detector
        loads = (
            Steady(DAY0 + 11 * HOUR, DAY0 + 11.3 * HOUR, 1200),
            Steady(DAY0 + 14 * HOUR, DAY0 + 14.1 * HOUR, 450),
        )
        hist, _ = cloudy_day(seed=1, clouds=False, pv_dt=10.0, loads=loads)
        held: list[float] = []
        steps = steps_of(hist, held=held)
        self.assertEqual(held, [])
        self.assertEqual(key(steps), key(detect_steps(raw_points(hist), threshold=300)))
        self.assertEqual(len(steps), 4)

    def test_without_a_solar_meter_nothing_changes(self):
        loads = (Steady(DAY0 + 11 * HOUR, DAY0 + 11.3 * HOUR, 1200),)
        hist, _ = cloudy_day(seed=1, loads=loads)
        cfg = AnalysisConfig(grid_entity=GRID)  # the solar production is not known to the analysis
        # the plain detector on the grid readings, as before #86
        self.assertEqual(steps_of(hist, cfg), detect_steps(raw_points(hist, cfg), threshold=300))
        with unittest.mock.patch("nilm_core.report.hold_doubtful", side_effect=AssertionError("not used")):
            report = analyze(hist, cfg, DAY0, DAY0 + 24 * HOUR, local_hour=HOUR_OF)
        self.assertNotIn("solar_held", report["quality"])
        self.assertNotIn("solar_interval_s", report["quality"])
        self.assertEqual(
            sorted(report["quality"]),
            ["compute_s", "max_gap_s", "meters", "pv_unknown_s", "samples", "steps", "submeters"],
        )
        # the live view has nothing to hold either
        self.assertIsNone(LiveDetector(cfg).hold)
        self.assertEqual(key(replay(hist, cfg, local_hour=HOUR_OF).steps), key(steps_of(hist, cfg)))


class LiveTest(unittest.TestCase):
    """The live view sets the same readings aside: the steps of the night."""

    def assertEquivalent(self, hist, cfg, **kwargs):
        night = steps_of(hist, cfg)
        live = replay(hist, cfg, local_hour=HOUR_OF, **kwargs)
        self.assertEqual(key(night), key(live.steps))
        return night

    def test_live_equals_night(self):
        night = AnalysisConfig(grid_entity=GRID, pv_entity=PV, night_step_threshold_w=150)
        evening = Steady(DAY0 + 20 * HOUR, DAY0 + 20 * HOUR + 1500, 170)
        for seed in range(3):
            loads = (*loads_near_clouds(seed), evening)
            hist, _ = cloudy_day(seed=seed, loads=loads)
            with self.subTest(day=seed):
                self.assertGreater(len(self.assertEquivalent(hist, SOLAR)), 25)
            with self.subTest(day=seed, variant="night threshold"):
                steps = self.assertEquivalent(hist, night)
                self.assertEqual(sum(1 for s in steps if abs(s.delta) < 300), 2)
        hist, _ = cloudy_day(seed=7, pv_lag_s=15.0, pv_dt=20.0, loads=tuple(loads_near_clouds(7)))
        with self.subTest(variant="late meter"):
            self.assertEquivalent(hist, SOLAR)
        with self.subTest(variant="irregular readings and a gap"):
            self.assertEquivalent(with_gap(jitter(hist), DAY0 + 13 * HOUR, DAY0 + 13.2 * HOUR), SOLAR)
        with self.subTest(variant="estimated interval"):
            self.assertEquivalent(hist, SOLAR, interval_s=None)

    def test_steps_wait_for_the_solar_reading_only_while_the_panels_produce(self):
        night_load = Steady(DAY0 + 2 * HOUR, DAY0 + 2.2 * HOUR, 1000)
        day_load = Steady(DAY0 + 12 * HOUR, DAY0 + 12.2 * HOUR, 1000)
        hist, _ = cloudy_day(seed=1, clouds=False, loads=(night_load, day_load))
        det = LiveDetector(SOLAR, interval_s=10.0)
        readings = sorted((ts, state, e) for e in (GRID, PV) for ts, state in hist[e])
        seen: dict[float, float] = {}  # step time -> when it was confirmed
        waiting = {"night": 0, "day": 0}
        prev = None
        for ts, state, entity in readings:
            if prev is not None and ts > prev:
                for st in det.advance(ts).steps:
                    if st.state == CONFIRMED:
                        seen.setdefault(st.ts, det.now)
                waiting["day" if 7.9 * HOUR < ts - DAY0 < 18.1 * HOUR else "night"] += det.hold.pending
            det.push(entity, ts, state)
            prev = ts
        self.assertEqual(waiting["night"], 0)  # nothing to wait for
        self.assertGreater(waiting["day"], 0)
        delay = {round((t - DAY0) / HOUR, 1): seen[t] - t for t in seen}
        self.assertEqual(sorted(delay), [2.0, 2.2, 12.0, 12.2])
        # at night as before (window + merge); in production once the
        # solar value has changed (every 30 s to 60 s here), a minute at most
        self.assertLessEqual(delay[2.0], 40)
        self.assertGreater(delay[12.0], delay[2.0])
        self.assertLessEqual(delay[12.0], delay[2.0] + PV_LATE_S + 10)


class SlowSolarMeterTest(unittest.TestCase):
    def test_solar_interval(self):
        day = (DAY0, DAY0 + 24 * HOUR)
        for every in (10.0, 30.0, 45.0, 120.0):
            with self.subTest(every=every):
                hist, _ = cloudy_day(seed=1, pv_dt=every)
                pv = [(t, float(v)) for t, v in hist[PV]]
                # Home Assistant records a value when it changes: the median
                # interval of a 10 s meter is 40 s here, and says nothing
                self.assertEqual(solar_interval(pv, *day, 150), every)
        # a clear day, the night, hardly any reading: nothing to say
        clear, _ = cloudy_day(seed=1, clouds=False)
        self.assertIsNone(solar_interval([(t, float(v)) for t, v in clear[PV]], *day, 150))
        self.assertIsNone(solar_interval(pv, DAY0, DAY0 + 7 * HOUR, 150))
        self.assertIsNone(solar_interval(pv[:10], *day, 150))
        self.assertIsNone(solar_interval([], *day, 150))

    def test_finding(self):
        def findings(pv_dt):
            hist, _ = cloudy_day(seed=1, pv_dt=pv_dt)
            report = analyze(hist, SOLAR, DAY0, DAY0 + 24 * HOUR, local_hour=HOUR_OF)
            return slow_solar_meter(report, PV)

        self.assertEqual(findings(30.0), [])
        self.assertEqual(findings(60.0), [])  # the limit itself is fine
        (f,) = findings(300.0)
        self.assertEqual((f.kind, f.entity_id), ("slow_solar_meter", PV))
        self.assertEqual(f.params, {"interval_s": 300, "limit_s": 60})
        self.assertIn("300 s", f.detail)
        # no solar meter, or a report from before #86
        self.assertEqual(slow_solar_meter({"quality": {"solar_interval_s": 300}}, None), [])
        self.assertEqual(slow_solar_meter({"quality": {}}, PV), [])
        self.assertEqual(slow_solar_meter({}, PV), [])

    def test_the_nightly_report_carries_it(self):
        from test_addon import TODAY, FakeHA, make_analyzer

        class Cloudy(FakeHA):
            every = 300.0  # the solar meter's interval

            def history(self, entities, start, end):
                hist = super().history(entities, start, end)
                # clouds: the production alternates every 10 minutes
                t, rows = start - start % self.every, []
                while t < end:
                    rows.append((t, "2000" if int(t // 600) % 2 else "1200"))
                    t += self.every
                hist[PV] = rows
                return hist

        for every, expected in ((300.0, ["slow_solar_meter"]), (60.0, [])):
            with self.subTest(every=every), tempfile.TemporaryDirectory() as tmp:
                ha = Cloudy(0)
                ha.every = every
                analyzer, _ = make_analyzer(tmp, ha, {"pv_entity": PV, "backfill_days": 30})
                report = analyzer.analyze_day(TODAY - dt.timedelta(days=1))
                self.assertEqual([f["kind"] for f in report["findings"]], expected)
                self.assertEqual(report["quality"]["solar_interval_s"], every)


if __name__ == "__main__":
    unittest.main()
