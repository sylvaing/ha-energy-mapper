"""The noise-adaptive step threshold (#88): smaller steps are kept where they
stand out of the measured noise of the curve, flagged ``low`` and paired into
small uses reported apart; everything else is found as without the option.
Synthetic days only (no real data)."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import benchmark  # noqa: E402
import synth_cycles  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig, analyze, detect_steps  # noqa: E402
from nilm_core.groundtruth import Run, learn  # noqa: E402
from nilm_core.live import LiveDetector, replay  # noqa: E402
from nilm_core.quality import meter_quality, robust_sigma, visible_w  # noqa: E402
from nilm_core.registry import update as update_registry  # noqa: E402
from nilm_core.report import find_steps  # noqa: E402
from nilm_core.steps import (  # noqa: E402
    NOISE,
    NoiseConfig,
    NoiseTracker,
    Step,
    StepMerger,
    StepStream,
    low_candidate,
)
from service import derive_options  # noqa: E402
from settings import SettingsError, validate  # noqa: E402
from synth import DAY0, HOUR, Steady, plug_history, small_loads_day, solar_drift_day  # noqa: E402
from test_live import flat, jitter, key, level, night_samples, with_gap  # noqa: E402

GRID = "sensor.grid_power"
PV = "sensor.pv_power"
PLUG = "sensor.plug_power"
START, END = DAY0, DAY0 + 24 * HOUR
HOUR_OF = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731


def config(adaptive=True, pv=False, **kwargs) -> AnalysisConfig:
    return AnalysisConfig(
        grid_entity=GRID, pv_entity=PV if pv else None, adaptive_threshold=adaptive, **kwargs
    )


def steps_of(hist, cfg):
    return find_steps(night_samples(hist, cfg), cfg)


def report_of(hist, cfg, start=START) -> dict:
    return analyze(hist, cfg, start, start + 24 * HOUR, local_hour=HOUR_OF)


def hour(ts: float) -> float:
    return (ts - DAY0) / HOUR


def stable(report: dict) -> dict:
    """A report without what changes from one run to the next."""
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


def without_option(report: dict) -> dict:
    """A report of the option on, without what only the option adds."""
    out = stable(report)
    out.pop("small_uses")
    out["quality"].pop("low_steps")
    return out


# ---------------------------------------------------------------------------
class RuleTest(unittest.TestCase):
    def test_threshold_is_the_floor_or_k_times_the_noise(self):
        self.assertEqual((NOISE.window_s, NOISE.k, NOISE.floor_w), (1800.0, 4.0, 40.0))
        self.assertIsNone(NOISE.threshold(None))
        self.assertEqual(NOISE.threshold(3.0), 40.0)
        self.assertEqual(NOISE.threshold(25.0), 100.0)

    def test_tracker_is_the_robust_spread_of_the_window(self):
        tracker = NoiseTracker(NoiseConfig(window_s=600, min_readings=5))
        for i in range(4):
            tracker.push(i * 10.0, 5.0)
        self.assertIsNone(tracker.noise_w)  # too few readings
        self.assertIsNone(tracker.threshold_w)
        tracker.push(40.0, -5.0)
        self.assertAlmostEqual(tracker.noise_w, 1.4826 * 5.0)
        # a switching appliance: a few large values do not move the median
        for i in range(5, 9):
            tracker.push(i * 10.0, 2000.0 if i == 6 else 5.0)
        self.assertAlmostEqual(tracker.noise_w, 1.4826 * 5.0)
        # the curve gets noisy: the noise follows, then forgets the quiet readings
        for i in range(9, 70):
            tracker.push(i * 10.0, 80.0)
        self.assertAlmostEqual(tracker.noise_w, 1.4826 * 80.0)
        self.assertAlmostEqual(tracker.threshold_w, 4 * 1.4826 * 80.0)

    def test_low_candidate(self):
        self.assertTrue(low_candidate(80.0, 70.0, 300.0, 40.0))
        self.assertTrue(low_candidate(-80.0, 70.0, 300.0, 40.0))
        self.assertFalse(low_candidate(80.0, 70.0, 300.0, None))  # the noise is not known
        self.assertFalse(low_candidate(80.0, 70.0, 300.0, 100.0))  # within the noise
        self.assertFalse(low_candidate(80.0, 20.0, 300.0, 40.0))  # a slow drift, no jump
        self.assertFalse(low_candidate(320.0, 70.0, 300.0, 40.0))  # not under the fixed threshold
        self.assertFalse(low_candidate(200.0, 150.0, 150.0, 40.0))  # nor under the night threshold

    def test_merger_keeps_the_fixed_steps_as_they_are(self):
        def run(candidates):
            merger = StepMerger()
            for ts, delta, low in candidates:
                merger.add(Step(ts, delta, 0.0, delta, low=low), 15.0)
            return [(s.ts, s.delta, s.low) for s in merger.pop(None, 15.0)]

        # two candidates of the fixed thresholds closer than the merge delay:
        # the largest, whatever the low candidates around and between them
        fixed = [(100.0, 500.0, False), (110.0, 800.0, False), (200.0, -800.0, False)]
        lows = [(95.0, 60.0, True), (104.0, -50.0, True), (118.0, 70.0, True), (190.0, 90.0, True)]
        self.assertEqual(run(fixed), [(110.0, 800.0, False), (200.0, -800.0, False)])
        self.assertEqual(
            run(sorted(fixed + lows)),
            # 95 and 118 are parts of the +800 W step; -50 and +90 are steps of their own
            [(104.0, -50.0, True), (110.0, 800.0, False), (190.0, 90.0, True), (200.0, -800.0, False)],
        )
        # low candidates merge per direction, the largest kept
        self.assertEqual(
            run([(10.0, 60.0, True), (14.0, -55.0, True), (20.0, 75.0, True), (40.0, 65.0, True)]),
            [(14.0, -55.0, True), (20.0, 75.0, True), (40.0, 65.0, True)],
        )

    def test_merger_gives_the_steps_in_order_as_time_passes(self):
        merger = StepMerger()
        merger.add(Step(100.0, 60.0, 0, 60, low=True), 15.0)
        merger.add(Step(105.0, -500.0, 500, 0), 15.0)
        self.assertEqual(merger.pop(110.0, 15.0), [])  # both may still be merged
        self.assertEqual(merger.waiting, 100.0)
        self.assertEqual([s.ts for s in merger.pop(116.0, 15.0)], [100.0])
        self.assertEqual([s.ts for s in merger.pop(121.0, 15.0)], [105.0])
        self.assertIsNone(merger.waiting)


# ---------------------------------------------------------------------------
class QuietNightTest(unittest.TestCase):
    """An 80 W fridge on a quiet curve: found, and kept apart."""

    @classmethod
    def setUpClass(cls):
        cls.hist = small_loads_day()
        cls.off = report_of(cls.hist, config(False))
        cls.on = report_of(cls.hist, config())

    def test_the_fixed_threshold_does_not_see_the_fridge(self):
        self.assertEqual([round(a["power_w"], -1) for a in self.off["activations"]], [2000, 2000])
        self.assertNotIn("small_uses", self.off)
        self.assertNotIn("low_steps", self.off["quality"])

    def test_every_cycle_of_the_fridge_is_a_small_use(self):
        small = self.on["small_uses"]
        self.assertEqual(len(small), 24)
        for i, use in enumerate(small):
            self.assertAlmostEqual(use["start"], START + i * HOUR, delta=20)
            self.assertAlmostEqual(use["end"] - use["start"], 1200, delta=30)
            for name in ("power_w", "on_w", "off_w"):
                self.assertTrue(70 <= use[name] <= 90, use)
        self.assertEqual(self.on["quality"]["low_steps"], 48)

    def test_small_uses_reach_neither_the_activations_nor_the_signatures(self):
        self.assertEqual(self.on["activations"], self.off["activations"])
        self.assertEqual(self.on["signatures"], self.off["signatures"])
        self.assertEqual(self.on["energy_kwh"], self.off["energy_kwh"])
        self.assertEqual(self.on["curve"], self.off["curve"])
        # nor the registry: nothing more is proposed for review
        registries = [
            update_registry(None, {"2023-11-15": r}, END, local_hour=HOUR_OF) for r in (self.off, self.on)
        ]
        self.assertEqual(registries[0], registries[1])
        self.assertEqual([round(s["power_w"], -1) for s in registries[1]["signatures"]], [2000])

    def test_the_low_steps_are_flagged(self):
        steps = steps_of(self.hist, config())
        low = [s for s in steps if s.low]
        self.assertEqual(len([s for s in low if START <= s.ts < END]), 48)
        self.assertTrue(all(60 <= abs(s.delta) <= 100 for s in low), [s.delta for s in low])
        self.assertTrue(all(abs(s.delta) >= 300 for s in steps if not s.low))
        self.assertEqual(key([s for s in steps if not s.low]), key(steps_of(self.hist, config(False))))

    def test_under_the_floor_nothing_is_kept(self):
        hist = small_loads_day(fridge_w=30.0)
        self.assertEqual(report_of(hist, config())["small_uses"], [])

    def test_no_low_step_before_the_noise_is_known(self):
        # 30 readings are needed: the fridge starts at the 10th and stops at the 50th
        points = [(DAY0 + 10 * i, 250.0 + (80.0 if 10 <= i < 50 else 0.0)) for i in range(70)]
        self.assertEqual([round(s.delta) for s in detect_steps(points, noise=NOISE)], [-80])
        quick = NoiseConfig(min_readings=3)
        self.assertEqual([round(s.delta) for s in detect_steps(points, noise=quick)], [80, -80])


# ---------------------------------------------------------------------------
class NoisySolarDayTest(unittest.TestCase):
    """Under a changing sky the threshold rises by itself: no false step."""

    @classmethod
    def setUpClass(cls):
        cls.hist = small_loads_day(pv_peak_w=3000.0, solar_noise=0.03)
        cls.cfg = config(pv=True)
        cls.steps = steps_of(cls.hist, cls.cfg)
        cls.report = report_of(cls.hist, cls.cfg)

    def test_a_low_fixed_threshold_would_be_flooded(self):
        # the test has teeth: at 40 W everywhere the solar hours make false steps
        points = [(s.ts, s.residual) for s in night_samples(self.hist, self.cfg)]
        solar = [s for s in detect_steps(points, threshold=40.0) if 9.5 <= hour(s.ts) < 16.5]
        self.assertGreater(len(solar), 100)

    def test_no_false_use_while_the_curve_is_noisy(self):
        # the noise is 40 W to 90 W there: the threshold is 200 W and more
        # (the fixed threshold when it reaches it). At 4 times the noise a
        # false step is rare, not impossible (one every other such day
        # over ten draws of this day, against more than 100 at a fixed
        # 40 W), and the noise may carry one of the fridge's own steps over
        # the threshold; none of them finds a partner to make a use
        solar = [s for s in self.steps if s.low and 9.5 <= hour(s.ts) < 16.5]
        false = [s for s in solar if min(hour(s.ts) % 1, abs(hour(s.ts) % 1 - 1 / 3)) > 1 / 60]
        self.assertLessEqual(len(false), 1)
        self.assertLessEqual(len(solar), 3)
        self.assertTrue(all(abs(s.delta) >= 150 for s in solar), solar)
        self.assertEqual([u for u in self.report["small_uses"] if 9.5 <= hour(u["start"]) < 16.5], [])

    def test_other_draws_of_the_day_make_no_false_use(self):
        for seed in (1, 2):
            hist = small_loads_day(pv_peak_w=3000.0, solar_noise=0.03, seed=seed)
            uses = report_of(hist, self.cfg)["small_uses"]
            # the only small uses of the solar hours are the fridge's own 20 minutes
            odd = [
                u for u in uses if 9.5 <= hour(u["start"]) < 16.5 and abs(u["end"] - u["start"] - 1200) > 60
            ]
            self.assertEqual(odd, [])

    def test_the_fridge_is_still_found_at_night(self):
        night = [u for u in self.report["small_uses"] if not 7.5 <= hour(u["start"]) < 18.5]
        self.assertEqual(len(night), 13)  # 00:00 to 07:00 and 19:00 to 23:00
        self.assertTrue(all(70 <= u["power_w"] <= 90 for u in night))

    def test_the_large_steps_are_those_of_the_option_off(self):
        off = steps_of(self.hist, config(False, pv=True))
        self.assertEqual(key([s for s in self.steps if not s.low]), key(off))
        self.assertEqual(len([s for s in off if abs(s.delta) > 1500 and START <= s.ts < END]), 4)

    def test_the_noise_is_measured_apart_from_the_solar_hours(self):
        meters = self.report["quality"]["meters"]
        self.assertTrue(2 <= meters["noise_w"]["no_solar"] <= 8, meters)
        self.assertTrue(30 <= meters["noise_w"]["solar"] <= 120, meters)
        self.assertEqual(meters["visible_w"], 40.0)  # at its quietest: the floor


# ---------------------------------------------------------------------------
class OptionOffTest(unittest.TestCase):
    """Off (the default), nothing changes; on, only the small uses are added."""

    def days(self):
        yield "fridge", small_loads_day(), {}
        yield "noisy solar", small_loads_day(pv_peak_w=3000.0, solar_noise=0.03), {"pv": True}
        yield (
            "night threshold",
            solar_drift_day(),
            {"pv": True, "night_step_threshold_w": 150.0},
        )
        house, day0, _ = synth_cycles.make_day(1)
        yield "appliances", house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10), {"start": day0}

    def test_off_is_the_default_and_adds_nothing(self):
        self.assertFalse(AnalysisConfig(grid_entity=GRID).adaptive_threshold)
        self.assertIsNone(AnalysisConfig(grid_entity=GRID).noise_config())
        for name, hist, kwargs in self.days():
            with self.subTest(day=name):
                kwargs = dict(kwargs)
                pv, start = kwargs.pop("pv", False), kwargs.pop("start", START)
                plain = AnalysisConfig(grid_entity=GRID, pv_entity=PV if pv else None, **kwargs)
                off = config(False, pv=pv, **kwargs)
                self.assertEqual(stable(report_of(hist, off, start)), stable(report_of(hist, plain, start)))
                # the detector itself: the very same steps as without the argument
                points = [(s.ts, s.residual) for s in night_samples(hist, off)]
                self.assertEqual(detect_steps(points, noise=None), detect_steps(points))

    def test_on_changes_nothing_but_the_small_uses(self):
        for name, hist, kwargs in self.days():
            with self.subTest(day=name):
                kwargs = dict(kwargs)
                pv, start = kwargs.pop("pv", False), kwargs.pop("start", START)
                off = report_of(hist, config(False, pv=pv, **kwargs), start)
                on = report_of(hist, config(True, pv=pv, **kwargs), start)
                self.assertEqual(without_option(on), stable(off))
                self.assertGreater(on["quality"]["low_steps"], 0)
                steps = steps_of(hist, config(True, pv=pv, **kwargs))
                self.assertEqual(
                    key([s for s in steps if not s.low]),
                    key(steps_of(hist, config(False, pv=pv, **kwargs))),
                )

    def test_the_night_threshold_still_decides_what_is_not_low(self):
        # #77: the 170 W evening load is a use as before, not a small one
        cfg = config(pv=True, night_step_threshold_w=150.0)
        report = report_of(solar_drift_day(), cfg)
        self.assertIn(170, [round(a["power_w"], -1) for a in report["activations"]])
        self.assertEqual([u for u in report["small_uses"] if hour(u["start"]) >= 18], [])
        # the grid meter's drifts under a steady sun (180 W every other 10
        # minutes) stand out of the noise: kept apart, never as activations
        drifts = [u for u in report["small_uses"] if 11 <= hour(u["start"]) < 14.5]
        self.assertEqual(len(drifts), 9)
        self.assertEqual(len(report["small_uses"]), 9)
        off = report_of(solar_drift_day(), config(False, pv=True, night_step_threshold_w=150.0))
        self.assertEqual(report["activations"], off["activations"])


# ---------------------------------------------------------------------------
class LiveTest(unittest.TestCase):
    """The stream gives the night's steps, the low ones among them."""

    def assertEquivalent(self, hist, cfg, low=None, **kwargs):
        night = steps_of(hist, cfg)
        live = replay(hist, cfg, local_hour=HOUR_OF, **kwargs)
        self.assertEqual(key(night), key(live.steps))
        self.assertEqual([s.low for s in night], [s.low for s in live.steps])
        if low is not None:
            self.assertEqual(sum(1 for s in night if s.low and START <= s.ts < END), low)
        return live

    def test_synthetic_days(self):
        self.assertEquivalent(small_loads_day(), config(), low=48)
        self.assertEquivalent(jitter(small_loads_day(seed=4)), config())
        self.assertEquivalent(with_gap(small_loads_day(), DAY0 + 3 * HOUR, DAY0 + 3.3 * HOUR), config())
        self.assertEquivalent(small_loads_day(dt=30.0), config())  # a slow meter: wider windows
        for index in (1, 2, 3):
            with self.subTest(day=index):
                house, day0, _ = synth_cycles.make_day(index)
                house.loads.append(synth_cycles.Cycling(day0, day0 + 24 * HOUR, 90, 900, 2700))
                self.assertEquivalent(house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10), config())

    def test_estimated_interval(self):
        self.assertEquivalent(small_loads_day(), config(), interval_s=None)

    def test_with_solar_the_hold_and_the_night_threshold(self):
        noisy = small_loads_day(pv_peak_w=3000.0, solar_noise=0.03)
        self.assertEquivalent(noisy, config(pv=True))
        self.assertEquivalent(noisy, config(pv=True, night_step_threshold_w=150.0))
        self.assertEquivalent(jitter(noisy), config(pv=True, night_step_threshold_w=150.0))
        drift = solar_drift_day()
        self.assertEquivalent(drift, config(pv=True, night_step_threshold_w=150.0))
        # a slow solar meter under passing clouds (#86): readings set aside
        from synth import cloudy_day

        cloudy, _ = cloudy_day(loads=(synth_cycles.Cycling(DAY0 - 3 * HOUR, END, 80, 1200, 2400),))
        self.assertEquivalent(cloudy, config(pv=True))

    def test_with_a_sub_meter_off_the_grid_clock(self):
        hist, cfg = late_plug_day()
        live = self.assertEquivalent(hist, cfg)
        night = steps_of(hist, cfg)
        # the sub-meter's echoes are explained as without the option (#85)
        self.assertEqual([s.explained_by for s in night], [s.explained_by for s in live.steps])
        self.assertTrue(any(s.explained_by for s in night))

    def test_stream_alone(self):
        points = level(
            DAY0, DAY0 + 4 * HOUR, 10, lambda t: 250 + (80 if (t - DAY0) % 3600 < 1200 else 0), 3.0
        )
        night = detect_steps(points, noise=NOISE)
        self.assertEqual(len([s for s in night if s.low]), 7)
        stream = StepStream(interval_s=10.0, noise=NOISE)
        out = []
        delays = []
        for ts, value in points:
            for st in stream.push(ts, value):
                out.append(st)
                delays.append(ts - st.ts)
        out += stream.flush()
        self.assertEqual(key(night), key([s.to_step() for s in out]))
        self.assertTrue(all(s.low and s.state == "confirmed" for s in out))
        self.assertTrue(all(s.to_dict()["low"] for s in out))
        # as soon as without the option: the window and the merge delay after the step
        self.assertLessEqual(max(delays), stream.window_s + stream.merge_s + 10.0)
        self.assertLess(len(stream._ts), 1100)  # the readings are pruned

    def test_the_feed_is_the_one_of_the_option_off(self):
        hist = small_loads_day()
        feeds = []
        for adaptive in (False, True):
            det = LiveDetector(config(adaptive), local_hour=HOUR_OF, interval_s=10.0)
            low = 0
            for ts, state in hist[GRID]:
                det.push(GRID, ts, state)
                low += sum(1 for s in det.advance(ts).steps if s.low)
            low += sum(1 for s in det.flush().steps if s.low)
            feeds.append([e.to_dict() for e in det.events.values()])
            self.assertEqual(low > 48, adaptive)  # the 24 cycles of the day and those before it
            self.assertEqual(low > 0, adaptive)
        self.assertEqual(feeds[0], feeds[1])
        self.assertEqual(len(feeds[0]), 2)  # the kettle, twice


def late_plug_day():
    """A 900 W heater and a 75 W pump on plugs that report up to 25 s late,
    next to an unmetered 80 W fridge."""
    heater = [Steady(DAY0 + h * HOUR + 1800, DAY0 + h * HOUR + 3300, 900) for h in (2, 9, 20)]
    pump = [Steady(DAY0 + h * HOUR + 600, DAY0 + h * HOUR + 2400, 75) for h in range(1, 23, 2)]
    hist = small_loads_day()
    stamps = [t for t, _ in hist[GRID]]
    values = dict(hist[GRID])
    extra = lambda t: sum(x.power_at(t) for x in (*heater, *pump))  # noqa: E731
    hist[GRID] = [(t, f"{float(values[t]) + extra(t):.1f}") for t in stamps]
    hist[PLUG] = plug_history(lambda t: sum(x.power_at(t) for x in heater), stamps[0], stamps[-1])
    hist["sensor.pump_power"] = plug_history(
        lambda t: sum(x.power_at(t) for x in pump), stamps[0], stamps[-1], seed=2
    )
    cfg = config(submeters=[SubmeterConfig(PLUG, "Heater"), SubmeterConfig("sensor.pump_power", "Pump")])
    return hist, cfg


class SubmeterTest(unittest.TestCase):
    def test_a_small_sub_meter_reporting_late_makes_no_small_use(self):
        # the pump's late reports leave +75 W / -75 W low steps in the
        # residual load: explained by the sub-meter, as the heater's are
        hist, cfg = late_plug_day()
        report = report_of(hist, cfg)
        fridge = [u for u in report["small_uses"] if u["end"] - u["start"] > 600]
        self.assertEqual(len(fridge), 24)
        self.assertEqual([u for u in report["small_uses"] if u["end"] - u["start"] <= 600], [])
        self.assertGreater(report["quality"]["low_steps"], 48 + 10)
        # what the heater's echoes explain does not change with the option
        off = report_of(hist, AnalysisConfig(grid_entity=GRID, submeters=cfg.submeters))
        self.assertEqual(without_option(report), stable(off))


# ---------------------------------------------------------------------------
class TooLowForTheCurveTest(unittest.TestCase):
    """Whether the curve can show an appliance is decided from its noise."""

    def runs(self, power):
        return [
            Run(DAY0 + i * HOUR, DAY0 + i * HOUR + 1200, power * 1200 / 3.6e6, power, power, "steady")
            for i in range(5)
        ]

    def test_meter_quality(self):
        hist = small_loads_day()
        samples = night_samples(hist, config())
        q = meter_quality(samples, None, START, END)
        self.assertEqual(q["grid"], {"median_s": 10.0, "p90_s": 10.0})
        self.assertIsNone(q["solar"])
        self.assertEqual(q["window_s"], 31.0)
        self.assertTrue(2 <= q["noise_w"]["no_solar"] <= 8, q)
        self.assertIsNone(q["noise_w"]["solar"])
        self.assertEqual(q["visible_w"], 40.0)
        # a noisy curve cannot show a small appliance
        noisy = night_samples(flat(level(START, END, 10, lambda t: 300.0, noise=40.0)), config())
        q = meter_quality(noisy, None, START, END)
        self.assertTrue(100 <= q["visible_w"] <= 250, q)
        self.assertEqual(meter_quality([], None, START, END), {"grid": None, "solar": None, "noise_w": {}})
        self.assertIsNone(robust_sigma([1.0] * 29))

    def test_it_is_in_the_report_whatever_the_option(self):
        hist = small_loads_day()
        on, off = report_of(hist, config()), report_of(hist, config(False))
        self.assertEqual(on["quality"]["meters"], off["quality"]["meters"])
        self.assertEqual(
            sorted(off["quality"]["meters"]), ["grid", "noise_w", "solar", "visible_w", "window_s"]
        )

    def test_visible_w_of_the_reports(self):
        reports = [{"quality": {"meters": {"visible_w": v}}} for v in (40.0, 44.0, 90.0)]
        self.assertEqual(visible_w(reports), 44.0)
        # reports written before the noise was measured
        self.assertIsNone(visible_w([{"quality": {}}, {}, None]))
        self.assertEqual(visible_w([{"quality": {}}, *reports[:1]]), 40.0)

    def test_the_verdict_comes_from_the_noise_not_from_the_threshold(self):
        # 70 W on a quiet curve: under the threshold (not tracked), yet the curve shows it
        seen = learn(self.runs(70), 300.0, visible_w=40.0)
        self.assertEqual((seen.recognisable, seen.visible), (False, True))
        self.assertTrue(seen.to_dict()["visible"])
        # the same appliance on a noisy curve, or a 20 W one: too low for the curve
        self.assertFalse(learn(self.runs(70), 300.0, visible_w=120.0).visible)
        self.assertFalse(learn(self.runs(20), 300.0, visible_w=40.0).visible)
        # above the threshold it is tracked, whatever the noise says
        big = learn(self.runs(400), 300.0, visible_w=600.0)
        self.assertEqual((big.recognisable, big.visible), (True, True))
        # the noise not measured yet: as before, the threshold decides
        old = learn(self.runs(70), 300.0)
        self.assertEqual((old.recognisable, old.visible), (False, False))

    def test_service_reads_it_from_the_stored_reports(self):
        from test_addon import FakeHA, make_analyzer

        with tempfile.TemporaryDirectory() as tmp:
            analyzer, _ = make_analyzer(tmp, FakeHA(0), {})
            self.assertIsNone(analyzer.visible_w({}))
            summaries = {
                f"2023-11-{d:02d}": {"quality": {"meters": {"visible_w": 40.0 + d}}} for d in range(1, 21)
            }
            self.assertEqual(analyzer.visible_w(summaries), 53.5)  # the last 14 days


# ---------------------------------------------------------------------------
class SettingsTest(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate({"adaptive_threshold": True}), {"adaptive_threshold": True})
        self.assertEqual(validate({"adaptive_threshold": False}), {"adaptive_threshold": False})
        for bad in (1, "true", 40.0):
            with self.subTest(value=bad), self.assertRaises(SettingsError):
                validate({"adaptive_threshold": bad})

    def test_from_the_options_to_the_analysis(self):
        from test_addon import FakeHA, make_analyzer

        self.assertNotIn("adaptive_threshold", derive_options({}))
        self.assertTrue(derive_options({"adaptive_threshold": True})["adaptive_threshold"])
        for options, expected in (({}, False), ({"adaptive_threshold": True}, True)):
            with tempfile.TemporaryDirectory() as tmp:
                analyzer, _ = make_analyzer(tmp, FakeHA(0), options)
                self.assertIs(analyzer.prepare().derived.config.adaptive_threshold, expected)


# ---------------------------------------------------------------------------
class BenchmarkTest(unittest.TestCase):
    """The registry-free score: a use is found when activations of similar
    power cover half of it."""

    def run_of(self, start, end, power):
        return Run(start, end, 0.0, power, power, "steady")

    def use(self, start, end, power, **extra):
        return {"start": start, "end": end, "power_w": power, **extra}

    def test_found_by_overlap_and_power(self):
        runs = [self.run_of(0, 1000, 900), self.run_of(5000, 6000, 900), self.run_of(9000, 10000, 900)]
        hidden = {
            "activations": [
                self.use(100, 700, 1000),  # 60 % of the first run, 11 % above its power
                self.use(5000, 5400, 950),  # 40 % of the second: not enough
                self.use(5500, 6000, 400),  # another power
                self.use(20000, 21000, 880),  # the device was off: a false detection
                self.use(30000, 31000, 900),  # another appliance: the real analysis has it too
            ],
            "small_uses": [self.use(9000, 9900, 700)],  # 22 % under: similar
        }
        visible = {"activations": [self.use(30010, 30990, 905)]}
        score = benchmark.free_score(runs, 900, hidden, visible, solar=lambda ts: ts >= 5000)
        # a detection is true when the device ran during it, found or not
        self.assertEqual(score["all"], {"uses": 3, "found": 2, "detections": 4, "true": 3})
        self.assertEqual(score["no_solar"], {"uses": 1, "found": 1, "detections": 1, "true": 1})
        self.assertEqual(score["solar"], {"uses": 2, "found": 1, "detections": 3, "true": 2})
        merged = benchmark._free_merge([score, score])
        self.assertEqual((merged["uses"], merged["found"], merged["recall"]), (6, 4, 0.667))
        self.assertEqual((merged["detections"], merged["true"], merged["precision"]), (8, 6, 0.75))
        self.assertEqual(merged["solar"]["recall"], 0.5)
        self.assertEqual(merged["no_solar"]["precision"], 1.0)

    def test_a_burst_series_counts_when_it_is_on(self):
        runs = [self.run_of(0, 1000, 900)]
        hidden = {"activations": [self.use(0, 4000, 900, blocks=[[0, 300], [3000, 4000]])]}
        score = benchmark.free_score(runs, 900, hidden, {})
        self.assertEqual(score["all"]["found"], 0)  # on for 30 % of the run only

    def test_the_hidden_fridge_is_found_with_the_option(self):
        from test_benchmark import FRIDGE, TZ, export
        from test_benchmark import config as bench_config

        hist = export()
        results = {
            a: benchmark.run(hist, bench_config(adaptive_threshold=a), TZ, [FRIDGE]) for a in (False, True)
        }
        off, on = (results[a]["devices"][0] for a in (False, True))
        # through the registry nothing changes: under the threshold, never tested
        self.assertEqual(
            {k: v for k, v in on.items() if k != "free"}, {k: v for k, v in off.items() if k != "free"}
        )
        self.assertEqual(off["status"], benchmark.BELOW_THRESHOLD)
        self.assertEqual((off["free"]["uses"], off["free"]["found"], off["free"]["recall"]), (96, 0, 0.0))
        self.assertEqual(on["free"]["uses"], 96)
        self.assertGreaterEqual(on["free"]["recall"], 0.95)
        self.assertGreaterEqual(on["free"]["precision"], 0.95)
        self.assertEqual(on["free"]["solar"]["uses"], 0)
        self.assertEqual(results[True]["thresholds"]["adaptive"], True)
        self.assertNotIn("adaptive", results[False]["thresholds"])
        # the real analysis subtracts the fridge: no small use is left in it
        self.assertEqual(results[True]["small_uses_per_day"], 0)
        self.assertEqual(results[False]["small_uses_per_day"], 0)
        self.assertEqual(results[True]["signatures"], results[False]["signatures"])
        text = benchmark.table(results[True])
        self.assertIn("adaptive to the noise", text)
        self.assertIn("without the registry", text)
        self.assertIn("free recall", benchmark.compare(results[False], results[True]))
        self.assertIn("0% -> 100%", benchmark.compare(results[False], results[True]))

    def test_command_line(self):
        import argparse

        args = argparse.Namespace(
            grid=GRID, grid_export=None, pv=None, submeter=[], estimated=[], switch=[], scale=[],
            threshold=300.0, night_threshold=None, adaptive=True,
        )  # fmt: skip
        self.assertTrue(benchmark.build_config(args).adaptive_threshold)
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "empty.csv"
            out.write_text("")
            # no reading in the export: the flag itself is accepted
            self.assertEqual(benchmark.main([str(out), "--grid", GRID, "--adaptive"]), 1)


if __name__ == "__main__":
    unittest.main()
