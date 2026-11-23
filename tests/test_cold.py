"""Cold appliances (#89): the regular low-power cycles (fridge, freezer) are
learned in the quiet hours from the low steps of the noise-adaptive
threshold, followed through the day, estimated where they cannot be, and
kept by the registry as signatures of kind ``cold``. Synthetic days only
(no real data)."""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import benchmark  # noqa: E402
from appliances import validate as validate_appliances  # noqa: E402
from nilm_core import AnalysisConfig, SubmeterConfig, analyze  # noqa: E402
from nilm_core import cold as cd  # noqa: E402
from nilm_core.hourly import hourly_breakdown  # noqa: E402
from nilm_core.live import LiveDetector  # noqa: E402
from nilm_core.registry import annotate_report, cold_models, expected_signature  # noqa: E402
from nilm_core.registry import update as update_registry  # noqa: E402
from nilm_core.report import find_steps  # noqa: E402
from review import Criteria  # noqa: E402
from review import state as review_state  # noqa: E402
from service import derive_options  # noqa: E402
from settings import SettingsError, validate  # noqa: E402
from synth import DAY0, HOUR, Compressor, Cycling, Steady, cold_day  # noqa: E402
from test_live import night_samples  # noqa: E402

GRID = "sensor.grid_power"
PV = "sensor.pv_power"
DAY = 24 * HOUR
START, END = DAY0, DAY0 + DAY
HOUR_OF = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731


def config(cold=True, adaptive=True, pv=False, **kwargs) -> AnalysisConfig:
    return AnalysisConfig(
        grid_entity=GRID,
        pv_entity=PV if pv else None,
        adaptive_threshold=adaptive,
        cold_appliances=cold,
        **kwargs,
    )


def fridge(day0=DAY0, **kwargs) -> Compressor:
    """85 W at the start, 72 W at the end, 9 minutes every 43."""
    return Compressor(day0 - 3 * HOUR, day0 + DAY, 85, 72, 540, 2040, seed=1, **kwargs)


def freezer(day0=DAY0, **kwargs) -> Compressor:
    """120 W at the start, 92 W at the end, about 5 minutes every 20, and a
    long run of 14 minutes every fourth one."""
    kwargs = {"long_every": 4, "long_s": 860, **kwargs}
    return Compressor(day0 - 3 * HOUR, day0 + DAY, 120, 92, 280, 930, seed=2, **kwargs)


def report_of(hist, cfg, start=START) -> dict:
    return analyze(hist, cfg, start, start + DAY, local_hour=HOUR_OF)


def stable(report: dict) -> dict:
    """A report without what changes from one run to the next."""
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


def by_power(report: dict) -> list[dict]:
    return sorted(report["cold"], key=lambda c: c["power_w"])


def covered(a: float, b: float, cycles: list[dict]) -> float:
    return sum(max(0.0, min(b, c["end"]) - max(a, c["start"])) for c in cycles)


def found(load: Compressor, item: dict, start=START, end=END) -> float:
    """The share of the load's runs of the day the appliance's runs cover by half."""
    runs = [(a, b) for a, b in load.runs if a >= start and b <= end]
    return sum(1 for a, b in runs if covered(a, b, item["cycles"]) >= 0.5 * (b - a)) / len(runs)


def right(load: Compressor, item: dict) -> float:
    """The share of the appliance's runs that the load's runs cover by half."""
    truth = [{"start": a, "end": b} for a, b in load.runs]
    cycles = item["cycles"]
    return sum(
        1 for c in cycles if covered(c["start"], c["end"], truth) >= 0.5 * (c["end"] - c["start"])
    ) / len(cycles)


def summary(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "curve"}


def detector_input(hist, cfg):
    """``(steps, points)`` as the analysis gives them to the detector."""
    samples = night_samples(hist, cfg)
    return find_steps(samples, cfg), [(s.ts, s.residual) for s in samples if s.residual is not None]


def no_quiet(start: float, end: float) -> bool:
    return False


def always_quiet(start: float, end: float) -> bool:
    return True


# ---------------------------------------------------------------------------
# two appliances on a noisy curve
# ---------------------------------------------------------------------------
class FridgeAndFreezerTest(unittest.TestCase):
    """A fridge and a freezer with other steps and other periods, on a curve
    that is quiet at night, noisy under the sun and busy in the evening."""

    @classmethod
    def setUpClass(cls):
        cls.fridge, cls.freezer = fridge(), freezer()
        cls.hist = cold_day(
            [cls.fridge, cls.freezer],
            pv_peak_w=3000,
            solar_noise=0.01,
            busy=((7, 9, 30), (18, 22, 40)),
        )
        cls.report = report_of(cls.hist, config(pv=True))
        cls.items = by_power(cls.report)

    def test_they_are_two_cold_appliances(self):
        self.assertEqual(len(self.items), 2)
        small, large = self.items
        self.assertAlmostEqual(small["on_w"], 85, delta=6)
        self.assertAlmostEqual(small["off_w"], 72, delta=6)
        self.assertAlmostEqual(small["on_s"], 540, delta=30)
        self.assertAlmostEqual(small["period_s"], 2580, delta=120)
        self.assertAlmostEqual(large["on_w"], 120, delta=8)
        self.assertAlmostEqual(large["off_w"], 92, delta=8)
        self.assertAlmostEqual(large["pause_s"], 930, delta=60)
        # the mean power of a run is measured on the curve, between the two steps
        self.assertAlmostEqual(small["power_w"], 78.5, delta=5)
        self.assertAlmostEqual(large["power_w"], 106, delta=6)

    def test_the_long_runs_belong_to_the_same_compressor(self):
        large = self.items[1]
        self.assertEqual(len(large["on_modes_s"]), 2)
        self.assertAlmostEqual(large["on_modes_s"][0], 280, delta=20)
        self.assertAlmostEqual(large["on_modes_s"][1], 860, delta=40)

    def test_each_energy_is_within_20_percent(self):
        for load, item in zip((self.fridge, self.freezer), self.items, strict=True):
            truth = load.energy_kwh(START, END)
            self.assertAlmostEqual(item["energy_kwh"], truth, delta=0.2 * truth)

    def test_their_runs_are_the_real_ones(self):
        for load, item in zip((self.fridge, self.freezer), self.items, strict=True):
            # all of them at night; by day and in the evening the noise hides a part
            self.assertGreaterEqual(found(load, item, START, START + 7 * HOUR), 0.9)
            self.assertGreaterEqual(found(load, item), 0.5)
            self.assertGreaterEqual(right(load, item), 0.85)

    def test_the_noisy_hours_are_an_estimate_and_say_so(self):
        for item in self.items:
            self.assertGreater(item["estimated_kwh"], 0)
            self.assertLess(item["estimated_kwh"], item["energy_kwh"])
            self.assertTrue(item["estimated"])
            for span in item["estimated"]:
                # no run is reported where the appliance was not followed
                self.assertEqual(covered(span["start"], span["end"], item["cycles"]), 0.0)
        counts = self.items[0]["counts"]
        self.assertEqual(sum(counts.values()), len(self.items[0]["cycles"]))
        self.assertEqual(set(counts), set(cd.HOWS))

    def test_they_are_signatures_of_their_own_kind(self):
        sigs = [s for s in self.report["signatures"] if s["kind"] == "cold"]
        self.assertEqual({s["id"] for s in sigs}, {c["id"] for c in self.items})
        self.assertEqual([s for s in self.report["signatures"] if s["kind"] != "cold"], [])
        for s in sigs:
            item = next(c for c in self.items if c["id"] == s["id"])
            self.assertEqual(s["band"], "cold")
            self.assertEqual(s["count"], len(item["cycles"]))
            self.assertEqual(s["energy_kwh"], item["energy_kwh"])
            self.assertEqual(s["estimated_kwh"], item["estimated_kwh"])
            self.assertEqual(sum(s["hours"]), s["count"])
        # no ordinary use, no ordinary signature: the small uses stay where they were
        self.assertEqual(self.report["activations"], [])
        self.assertTrue(self.report["small_uses"])

    def test_their_energy_leaves_the_unexplained_share(self):
        off = report_of(self.hist, config(cold=False, pv=True))
        on = self.report["energy_kwh"]
        explained = sum(on["signatures"].values())
        self.assertEqual(set(on["signatures"]), {c["id"] for c in self.items})
        self.assertGreater(explained, 0.8 * sum(c["energy_kwh"] for c in self.items))
        self.assertAlmostEqual(off["energy_kwh"]["other"] - on["other"], explained, delta=0.02)
        self.assertAlmostEqual(off["energy_kwh"]["base"], on["base"], delta=0.001)
        self.assertEqual(off["energy_kwh"]["house"], on["house"])

    def test_other_draws_of_the_day(self):
        for seed in (11, 12, 13):
            hist = cold_day(
                [self.fridge, self.freezer], pv_peak_w=3000, solar_noise=0.01, busy=((18, 22, 40),), seed=seed
            )
            items = by_power(report_of(hist, config(pv=True)))
            self.assertEqual(len(items), 2, seed)
            for load, item in zip((self.fridge, self.freezer), items, strict=True):
                truth = load.energy_kwh(START, END)
                self.assertAlmostEqual(item["energy_kwh"], truth, delta=0.2 * truth, msg=seed)


# ---------------------------------------------------------------------------
# the machine: split, complete, fill, estimate
# ---------------------------------------------------------------------------
class FollowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.load = fridge()
        cls.cfg = config()
        cls.hist = cold_day([cls.load])
        cls.steps, cls.points = detector_input(cls.hist, cls.cfg)
        cls.model = cd.learn(cls.steps, cls.points, always_quiet)[0]
        # the runs of the morning, far from the edges of the day
        cls.runs = [r for r in cls.load.runs if START + 3 * HOUR <= r[0] < START + 9 * HOUR]

    def without(self, *edges: float) -> list:
        """The steps without those within 30 s of ``edges``."""
        return [s for s in self.steps if not any(abs(s.ts - t) <= 30 for t in edges)]

    def cycles_within(self, track, a: float, b: float) -> list[tuple[float, float, str]]:
        return [c for c in track.cycles if c[0] >= a and c[1] <= b]

    def test_every_run_of_a_quiet_day_is_detected(self):
        self.assertGreaterEqual(self.model.cycles, len(self.load.runs) - 2)
        track = cd.follow(self.model, self.steps, self.points, START, END)
        day = [c for c in track.cycles if c[0] >= START and c[1] <= END]
        self.assertEqual({how for _, _, how in day}, {cd.DETECTED})
        self.assertEqual(len(day), len([r for r in self.load.runs if r[0] >= START and r[1] <= END]))
        self.assertEqual(track.estimated, [])

    def test_a_detection_that_spans_two_runs_is_split(self):
        """The off-step of a run and the on-step of the next one are missed:
        pairing the steps left makes one use of 52 minutes; the machine
        makes the two runs."""
        first, second = self.runs[2], self.runs[3]
        steps = self.without(first[1], second[0])
        track = cd.follow(self.model, steps, self.points, START, END)
        got = self.cycles_within(track, first[0] - 60, second[1] + 60)
        self.assertEqual(len(got), 2)
        for (a, b, how), (x, y) in zip(got, (first, second), strict=True):
            # the missing edge is found in the curve, under the adaptive threshold
            self.assertEqual(how, cd.COMPLETED)
            self.assertAlmostEqual(a, x, delta=30)
            self.assertAlmostEqual(b, y, delta=30)
        self.assertEqual(track.estimated, [])

    def test_the_missing_edge_is_placed_when_the_curve_does_not_show_it(self):
        first = self.runs[2]
        steps = self.without(first[1])
        flat = [(t, 250.0) for t, _ in self.points]  # a curve that shows nothing
        track = cd.follow(self.model, steps, flat, START, END)
        got = self.cycles_within(track, first[0] - 60, first[1] + 120)
        self.assertEqual([how for _, _, how in got], [cd.PLACED])
        self.assertAlmostEqual(got[0][1] - got[0][0], self.model.on_s[0], delta=1)

    def test_a_missed_run_is_filled(self):
        missed = self.runs[2]
        steps = self.without(*missed)
        track = cd.follow(self.model, steps, self.points, START, END)
        got = self.cycles_within(track, missed[0] - 120, missed[1] + 120)
        self.assertEqual([how for _, _, how in got], [cd.FILLED])
        # its edges are looked for in the curve where the period says it is due
        self.assertAlmostEqual(got[0][0], missed[0], delta=30)
        self.assertAlmostEqual(got[0][1], missed[1], delta=30)
        self.assertEqual(
            len(track.cycles), len(cd.follow(self.model, self.steps, self.points, START, END).cycles)
        )

    def test_a_filled_run_is_placed_by_the_period_when_the_curve_shows_nothing(self):
        missed = self.runs[2]
        steps = self.without(*missed)
        flat = [(t, 250.0) for t, _ in self.points]
        track = cd.follow(self.model, steps, flat, START, END)
        got = [c for c in track.cycles if c[2] == cd.FILLED]
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][0], missed[0], delta=120)

    def test_more_than_two_missing_runs_are_an_estimate(self):
        gone = self.runs[1:5]
        steps = self.without(*(t for r in gone for t in r))
        track = cd.follow(self.model, steps, self.points, START, END)
        self.assertEqual(self.cycles_within(track, gone[0][0] - 60, gone[-1][1] + 60), [])
        self.assertEqual(len(track.estimated), 1)
        a, b, power = track.estimated[0]
        self.assertAlmostEqual(a, self.runs[0][1], delta=30)
        self.assertAlmostEqual(b, self.runs[5][0], delta=30)
        # the energy of the four runs, from the duty
        truth = sum(self.load.energy_kwh(x, y) for x, y in gone)
        self.assertAlmostEqual(power * (b - a) / 3.6e6, truth, delta=0.15 * truth)
        whole = self.load.energy_kwh(START, END)
        self.assertAlmostEqual(track.overlap_energy_wh(START, END) / 1000, whole, delta=0.05 * whole)
        self.assertAlmostEqual(
            track.overlap_energy_wh(START, END, estimated=True) / 1000, truth, delta=0.15 * truth
        )

    def test_an_edge_is_found_in_the_curve_only_where_it_stands_out(self):
        from nilm_core.steps import Signal

        a, b = self.runs[0]
        sig = Signal(self.points)
        up = cd.find_edge(sig, a - 200, a + 200, 1.0, self.model.on_w, cd.COLD)
        self.assertAlmostEqual(up[0], a, delta=20)
        self.assertAlmostEqual(up[1], self.model.on_w, delta=10)
        self.assertIsNone(cd.find_edge(sig, a - 200, a + 200, -1.0, self.model.off_w, cd.COLD))
        self.assertIsNone(cd.find_edge(sig, a + 100, b - 100, 1.0, self.model.on_w, cd.COLD))
        # a larger move is another appliance
        self.assertIsNone(cd.find_edge(sig, a - 200, a + 200, 1.0, self.model.on_w / 2, cd.COLD))


# ---------------------------------------------------------------------------
# what must not become a cold appliance
# ---------------------------------------------------------------------------
class NoSignatureTest(unittest.TestCase):
    def irregular(self, seed: int, fixed_length: bool) -> list:
        """Small loads switched at any time of the night and of the day."""
        rnd = random.Random(seed)
        loads = []
        t = START - 3 * HOUR + 600
        while t < END - HOUR:
            length = 420.0 if fixed_length else rnd.uniform(150, 2400)
            loads.append(Steady(t, t + length, 80.0 if fixed_length else rnd.uniform(60, 130)))
            t += length + rnd.expovariate(1 / 2400.0) + 120
        return loads

    def test_irregular_small_loads_make_no_signature(self):
        for seed in (1, 2, 3):
            report = report_of(cold_day(self.irregular(seed, False)), config())
            self.assertEqual(report["cold"], [], seed)
            self.assertEqual([s for s in report["signatures"] if s["kind"] == "cold"], [], seed)
            self.assertGreater(len(report["small_uses"]), 10)  # they stay small uses

    def test_a_load_of_constant_length_at_any_time_makes_none(self):
        """The same power and the same length each time (a fan on a timer),
        but no rhythm: its pauses are not regular."""
        for seed in (1, 2, 3):
            report = report_of(cold_day(self.irregular(seed, True)), config())
            self.assertEqual(report["cold"], [], seed)
            self.assertGreater(len(report["small_uses"]), 10)

    def test_pure_noise_makes_none(self):
        for quiet_w, seed in ((4.0, 1), (15.0, 2), (40.0, 3), (40.0, 4)):
            report = report_of(cold_day([], quiet_w=quiet_w, seed=seed), config())
            self.assertEqual(report["cold"], [], (quiet_w, seed))
        report = report_of(cold_day([], pv_peak_w=3000, solar_noise=0.02), config(pv=True))
        self.assertEqual(report["cold"], [])

    def test_too_few_runs_make_none(self):
        """Four runs in the night: not enough to call it a rhythm."""
        short = Compressor(START - 3 * HOUR, START - 0.5 * HOUR, 85, 72, 540, 2040, seed=1)
        self.assertLess(len(short.runs), cd.COLD.min_cycles)
        self.assertEqual(report_of(cold_day([short]), config())["cold"], [])

    def test_a_large_cycling_load_stays_an_ordinary_signature(self):
        """A 900 W heater regulating is seen by the fixed threshold: it is
        no business of the cold appliances."""
        heater = Cycling(START + 2 * HOUR, START + 5 * HOUR, 900, 300, 900)
        report = report_of(cold_day([heater]), config())
        self.assertEqual(report["cold"], [])
        self.assertTrue(report["activations"])

    def test_learning_only_where_the_curve_is_quiet(self):
        steps, points = detector_input(cold_day([fridge()]), config())
        self.assertEqual(cd.learn(steps, points, no_quiet), [])
        quiet = cd.Quiet([START + 2 * HOUR], None, 300.0)
        self.assertFalse(quiet(START + 2 * HOUR - 200, START + 2 * HOUR - 100))
        self.assertFalse(quiet(START + 2 * HOUR + 100, START + 2 * HOUR + 200))
        self.assertTrue(quiet(START + 3 * HOUR, START + 3 * HOUR + 500))
        sunny = cd.Quiet([], lambda ts: ts < START + 8 * HOUR, 300.0)
        self.assertTrue(sunny(START, START + 500))
        self.assertFalse(sunny(START + 8 * HOUR - 100, START + 8 * HOUR + 400))

    def test_two_appliances_with_the_same_steps_are_never_two(self):
        """The curve cannot tell their edges apart: one group at most
        (here their union has no rhythm, so none)."""
        a = Compressor(START - 3 * HOUR, END, 85, 72, 540, 2040, seed=1)
        b = Compressor(START - 3 * HOUR, END, 86, 73, 400, 1500, seed=4)
        report = report_of(cold_day([a, b]), config())
        self.assertLessEqual(len(report["cold"]), 1)


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------
class OptionOffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hist = cold_day([fridge(), freezer()], pv_peak_w=3000, solar_noise=0.01)

    def test_off_is_the_default(self):
        cfg = AnalysisConfig(grid_entity=GRID)
        self.assertFalse(cfg.cold_appliances)
        self.assertIsNone(cfg.cold_config())
        self.assertIsNone(AnalysisConfig(grid_entity=GRID, cold_appliances=True).cold_config())
        self.assertIsNone(AnalysisConfig(grid_entity=GRID, adaptive_threshold=True).cold_config())
        self.assertIsNotNone(config().cold_config())

    def test_without_the_adaptive_threshold_it_does_nothing(self):
        plain = report_of(self.hist, AnalysisConfig(grid_entity=GRID, pv_entity=PV))
        ours = report_of(self.hist, config(adaptive=False, pv=True))
        self.assertEqual(stable(ours), stable(plain))
        self.assertNotIn("cold", ours)
        self.assertNotIn("small_uses", ours)

    def test_off_the_report_is_the_one_of_the_adaptive_threshold_alone(self):
        adaptive = AnalysisConfig(grid_entity=GRID, pv_entity=PV, adaptive_threshold=True)
        off = report_of(self.hist, config(cold=False, pv=True))
        self.assertEqual(stable(off), stable(report_of(self.hist, adaptive)))
        self.assertNotIn("cold", off)
        # the models of the registry are not read either
        models = [{"id": "cold-80w", "on_w": 85, "off_w": 72, "power_w": 79, "on_s": 540, "pause_s": 2040}]
        known = report_of(self.hist, config(cold=False, pv=True, cold_models=models))
        self.assertEqual(stable(known), stable(off))

    def test_on_changes_nothing_else(self):
        """With the option, the report is the one without it plus the cold
        appliances: their list, their signatures, and their energy taken
        from the unexplained share."""
        off = stable(report_of(self.hist, config(cold=False, pv=True)))
        on = stable(report_of(self.hist, config(pv=True)))
        self.assertEqual(len(on.pop("cold")), 2)
        self.assertEqual([s for s in on["signatures"] if s["kind"] != "cold"], off["signatures"])
        for key in set(off) - {"signatures", "curve", "energy_kwh"}:
            self.assertEqual(on[key], off[key], key)
        for key in ("house", "grid_import", "grid_export", "pv", "submetered", "known", "base_w"):
            self.assertEqual(on["energy_kwh"][key], off["energy_kwh"][key], key)
        for a, b in zip(on["curve"], off["curve"], strict=True):
            for key in ("t", "house", "grid", "pv", "submetered", "base", "known"):
                self.assertEqual(a[key], b[key])

    def test_on_a_day_without_any_the_report_only_gains_an_empty_list(self):
        hist = cold_day([Steady(START + 7 * HOUR, START + 7 * HOUR + 240, 2000)])
        off = stable(report_of(hist, config(cold=False)))
        on = stable(report_of(hist, config()))
        self.assertEqual(on.pop("cold"), [])
        self.assertEqual(on, off)

    def test_the_live_view_is_the_one_without_the_option(self):
        """Nightly only: a low step is listed live and opens no event,
        whatever the option."""
        hist = cold_day([fridge(), Steady(START + 7 * HOUR, START + 7 * HOUR + 240, 2000)])
        feeds = []
        for cold in (False, True):
            det = LiveDetector(config(cold=cold), local_hour=HOUR_OF, interval_s=10.0)
            steps = []
            for ts, state in hist[GRID]:
                det.push(GRID, ts, state)
                steps += [s.to_dict() for s in det.advance(ts).steps]
            steps += [s.to_dict() for s in det.flush().steps]
            feeds.append((steps, [e.to_dict() for e in det.events.values()]))
        self.assertEqual(feeds[0], feeds[1])
        self.assertEqual(len(feeds[0][1]), 1)  # the kettle


class SettingsTest(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate({"cold_appliances": True}), {"cold_appliances": True})
        self.assertEqual(validate({"cold_appliances": False}), {"cold_appliances": False})
        for bad in (1, "true", 40.0):
            with self.subTest(value=bad), self.assertRaises(SettingsError):
                validate({"cold_appliances": bad})

    def test_from_the_options_to_the_analysis(self):
        from test_addon import FakeHA, make_analyzer

        self.assertNotIn("cold_appliances", derive_options({}))
        self.assertTrue(derive_options({"cold_appliances": True})["cold_appliances"])
        cases = (
            ({}, False),
            ({"cold_appliances": True}, False),  # needs the adaptive threshold
            ({"adaptive_threshold": True}, False),
            ({"adaptive_threshold": True, "cold_appliances": True}, True),
        )
        for options, expected in cases:
            with tempfile.TemporaryDirectory() as tmp:
                analyzer, _ = make_analyzer(tmp, FakeHA(0), options)
                cfg = analyzer.prepare().derived.config
                self.assertIs(cfg.cold_config() is not None, expected, options)

    def test_an_appliance_can_name_a_cold_signature(self):
        source = {"type": "signature", "id": "cold-80w", "kind": "cold", "band": "cold", "power_w": 79}
        out = validate_appliances({"appliances": [{"key": "fridge", "name": "Fridge", "sources": [source]}]})
        self.assertEqual(out[0]["sources"][0]["kind"], "cold")
        # declared before the registry has it (a registry rebuilt from scratch)
        sig = expected_signature({"signatures": []}, "cold", "cold", 79, "Fridge", 0.0)
        self.assertEqual(
            (sig["id"], sig["kind"], sig["band"], sig["label"]), ("cold-100w", "cold", "cold", "Fridge")
        )
        self.assertEqual(cold_models({"signatures": [sig]}), [])  # no model yet: nothing to follow


# ---------------------------------------------------------------------------
# the registry: the model persists from day to day
# ---------------------------------------------------------------------------
def three_days(loads_of=lambda day0: [fridge(day0), freezer(day0)], **kwargs):
    """``[(day, start, histories)]`` of three days of the same appliances."""
    out = []
    for i in range(3):
        day0 = DAY0 + i * DAY
        out.append(
            (f"2027-01-{15 + i:02d}", day0, cold_day(loads_of(day0), day0=day0, seed=20 + i, **kwargs))
        )
    return out


class RegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = three_days()
        cls.reports = {}
        cls.registries = []
        registry = None
        for day, start, hist in cls.days:
            cfg = config(cold_models=cold_models(registry))
            cls.reports[day] = report_of(hist, cfg, start)
            summaries = {d: summary(r) for d, r in cls.reports.items()}
            registry = update_registry(registry, summaries, start + DAY, local_hour=HOUR_OF)
            cls.registries.append(registry)
        cls.registry = registry
        cls.sigs = sorted(
            (s for s in registry["signatures"] if s["kind"] == "cold"), key=lambda s: s["power_w"]
        )

    def test_two_cold_signatures_and_nothing_else(self):
        self.assertEqual(len(self.sigs), 2)
        self.assertEqual(len(self.registry["signatures"]), 2)
        small, large = self.sigs
        self.assertEqual((small["kind"], small["band"]), ("cold", "cold"))
        self.assertRegex(small["id"], r"^cold-\d+w$")
        self.assertNotEqual(small["id"], large["id"])
        self.assertAlmostEqual(small["power_w"], 78.5, delta=5)
        self.assertAlmostEqual(small["on_w"], 85, delta=6)
        self.assertAlmostEqual(small["off_w"], 72, delta=6)
        self.assertAlmostEqual(small["on_s"], 540, delta=30)
        self.assertAlmostEqual(small["period_s"], 2580, delta=120)
        self.assertAlmostEqual(small["duty"], 0.21, delta=0.03)
        self.assertEqual(len(large["on_modes_s"]), 2)

    def test_it_counts_the_runs_and_the_energy_of_the_days(self):
        small = self.sigs[0]
        items = [by_power(r)[0] for r in self.reports.values()]
        self.assertEqual(small["days_seen"], 3)
        self.assertEqual(small["count"], sum(len(c["cycles"]) for c in items))
        self.assertAlmostEqual(small["energy_kwh"], sum(c["energy_kwh"] for c in items), places=2)
        self.assertAlmostEqual(small["estimated_kwh"], sum(c["estimated_kwh"] for c in items), places=2)
        self.assertEqual((small["first_seen"], small["last_seen"]), ("2027-01-15", "2027-01-17"))
        self.assertGreater(small["weight"], 0.9 * small["count"] * 0.9)
        self.assertEqual(len(small["examples"]), 3)
        self.assertEqual(len(small["occurrences"]), small["count"])
        self.assertAlmostEqual(sum(small["hours"]), small["weight"], delta=0.5)

    def test_the_identity_is_stable_from_the_first_day(self):
        first = sorted(s["id"] for s in self.registries[0]["signatures"])
        self.assertEqual(sorted(s["id"] for s in self.sigs), first)
        for day, report in self.reports.items():
            annotate_report(report, self.registry)
            self.assertEqual(sorted(c["registry_id"] for c in report["cold"]), first, day)
            cold = [s for s in report["signatures"] if s["kind"] == "cold"]
            self.assertEqual(sorted(s["registry_id"] for s in cold), first, day)
        # from the second day on, the analysis knows which model it follows
        later = list(self.reports.values())[1:]
        self.assertEqual({c["model"] for r in later for c in r["cold"]}, set(first))

    def test_it_is_proposed_for_review_after_a_few_nights(self):
        criteria = Criteria()
        end = DAY0 + 3 * DAY
        self.assertEqual([review_state(s, end, criteria, ()) for s in self.sigs], ["proposed"] * 2)
        one = [s for s in self.registries[0]["signatures"] if s["kind"] == "cold"]
        self.assertEqual([review_state(s, end, criteria, ()) for s in one], ["immature"] * 2)

    def test_the_label_and_the_review_follow_it(self):
        previous = {**self.registry, "signatures": [dict(s) for s in self.registry["signatures"]]}
        sid = self.sigs[0]["id"]
        mine = next(s for s in previous["signatures"] if s["id"] == sid)
        mine["label"] = "Fridge"
        mine["review"] = {"state": "validated", "at": 1.0, "appliance": "fridge"}
        summaries = {d: summary(r) for d, r in self.reports.items()}
        again = update_registry(previous, summaries, DAY0 + 3 * DAY, local_hour=HOUR_OF)
        kept = next(s for s in again["signatures"] if s["id"] == sid)
        self.assertEqual(kept["label"], "Fridge")
        self.assertEqual(kept["review"]["state"], "validated")
        # with no day left it is carried, fading, and kept while it is named
        later = update_registry(again, {}, DAY0 + 600 * DAY, local_hour=HOUR_OF)
        self.assertEqual([s["id"] for s in later["signatures"]], [sid])
        self.assertLess(later["signatures"][0]["weight"], 0.01)

    def test_the_models_go_back_to_the_analysis(self):
        models = cold_models(self.registry)
        self.assertEqual({m["id"] for m in models}, {s["id"] for s in self.sigs})
        for m in models:
            self.assertIsNotNone(cd.Model.from_dict(m))
            self.assertNotIn("ignored", m)
        ignored = {**self.registry, "signatures": [dict(s) for s in self.registry["signatures"]]}
        ignored["signatures"][0]["review"] = {"state": "ignored", "at": 1.0}
        flags = {m["id"]: m.get("ignored", False) for m in cold_models(ignored)}
        self.assertEqual(sorted(flags.values()), [False, True])

    def test_an_ignored_one_is_not_reported_again(self):
        ignored = {**self.registry, "signatures": [dict(s) for s in self.registry["signatures"]]}
        sid = self.sigs[0]["id"]
        next(s for s in ignored["signatures"] if s["id"] == sid)["review"] = {"state": "ignored", "at": 1.0}
        day, start, hist = self.days[2]
        report = report_of(hist, config(cold_models=cold_models(ignored)), start)
        self.assertEqual([c["model"] for c in report["cold"]], [self.sigs[1]["id"]])
        # its energy is unexplained again, not given to the other one
        other = by_power(self.reports[day])[1]
        self.assertAlmostEqual(report["cold"][0]["energy_kwh"], other["energy_kwh"], delta=0.05)

    def test_the_hourly_energy_goes_to_the_registry_id(self):
        day, _, _ = self.days[1]
        report = self.reports[day]
        hourly = hourly_breakdown(report, self.registry)
        ids = {s["id"] for s in self.sigs}
        self.assertEqual(set(hourly["energy_kwh"]["signatures"]), ids)
        total = sum(hourly["energy_kwh"]["signatures"].values())
        self.assertAlmostEqual(total, sum(report["energy_kwh"]["signatures"].values()), delta=0.01)
        small = by_power(report)[0]
        self.assertEqual(hourly["uses"]["signatures"][self.sigs[0]["id"]], len(small["cycles"]))

    def test_a_night_that_teaches_nothing_is_followed_from_the_stored_model(self):
        """A heater regulates all night: no quiet stretch, nothing to learn
        from. The appliances are followed with the models of the registry."""
        day0 = DAY0 + 3 * DAY
        loads = [fridge(day0), freezer(day0)]
        heater = Cycling(day0 - 3 * HOUR, day0 + DAY, 900, 60, 180)
        hist = cold_day([*loads, heater], day0=day0, seed=31)
        alone = report_of(hist, config(), day0)
        self.assertEqual(alone["cold"], [])
        report = report_of(hist, config(cold_models=cold_models(self.registry)), day0)
        items = by_power(report)
        self.assertEqual(len(items), 2)
        for load, item, sig in zip(loads, items, self.sigs, strict=True):
            self.assertEqual(item["model"], sig["id"])
            self.assertEqual(item["learned_cycles"], 0)
            self.assertEqual(item["on_w"], sig["on_w"])
            truth = load.energy_kwh(day0, day0 + DAY)
            self.assertAlmostEqual(item["energy_kwh"], truth, delta=0.2 * truth)
        # such a day adds its runs and its energy, and leaves the model as it was
        summaries = {d: summary(r) for d, r in self.reports.items()} | {"2027-01-18": summary(report)}
        again = update_registry(self.registry, summaries, day0 + DAY, local_hour=HOUR_OF)
        for sig in self.sigs:
            new = next(s for s in again["signatures"] if s["id"] == sig["id"])
            self.assertEqual(new["days_seen"], 4)
            for key in ("on_w", "off_w", "power_w", "on_s", "pause_s", "on_modes_s"):
                self.assertEqual(new[key], sig[key], key)

    def test_an_appliance_that_is_gone_is_not_estimated(self):
        """The freezer was unplugged; small loads of its size switch now
        and then. The stored model finds a few of their steps: not enough
        to report it, and nothing is estimated for it."""
        day0 = DAY0 + 3 * DAY
        rnd = random.Random(9)
        strays = []
        for _ in range(8):
            t = day0 + rnd.uniform(1, 23) * HOUR
            strays.append(Steady(t, t + rnd.uniform(200, 900), 120))
        hist = cold_day([fridge(day0), *strays], day0=day0, seed=32)
        report = report_of(hist, config(cold_models=cold_models(self.registry)), day0)
        self.assertEqual([c["model"] for c in report["cold"]], [self.sigs[0]["id"]])
        # the day does not count for the freezer: it fades in the registry
        summaries = {d: summary(r) for d, r in self.reports.items()} | {"2027-01-18": summary(report)}
        again = update_registry(self.registry, summaries, day0 + DAY, local_hour=HOUR_OF)
        gone = next(s for s in again["signatures"] if s["id"] == self.sigs[1]["id"])
        self.assertEqual((gone["days_seen"], gone["last_seen"]), (3, "2027-01-17"))

    def test_a_night_with_only_the_long_runs_does_not_change_the_model(self):
        stored = cd.Model.from_dict(cold_models(self.registry)[0])
        today = cd.Model(
            stored.on_w, stored.off_w, stored.power_w, [stored.on_s[0] * 3], 3 * stored.run_s, 4000.0
        )
        self.assertIsNotNone(cd.alike(today.to_dict(), stored.to_dict()))
        self.assertGreater(abs(today.period_s - stored.period_s), cd.COLD.model_tol * stored.period_s)

    def test_alike(self):
        a = {"on_w": 85, "off_w": 72}
        self.assertEqual(cd.alike(a, a), 0.0)
        self.assertIsNotNone(cd.alike(a, {"on_w": 90, "off_w": 75}))
        self.assertIsNone(cd.alike(a, {"on_w": 120, "off_w": 72}))
        self.assertIsNone(cd.alike(a, {"on_w": 85, "off_w": 92}))
        self.assertIsNone(cd.alike(a, {"on_w": 85}))
        self.assertIsNone(cd.alike(a, {"on_w": 0, "off_w": 72}))

    def test_a_stored_model_that_is_not_one_is_left_out(self):
        self.assertIsNone(cd.Model.from_dict({"id": "cold-80w", "power_w": 80}))
        self.assertIsNone(
            cd.Model.from_dict({"on_w": 85, "off_w": 72, "power_w": 79, "on_s": 0, "pause_s": 2000})
        )
        model = cd.Model.from_dict({"on_w": 85, "off_w": 72, "power_w": 79, "on_s": 540, "pause_s": 2040})
        self.assertEqual((model.on_s, model.run_s, model.period_s), ([540.0], 540.0, 2580.0))
        self.assertAlmostEqual(model.duty, 540 / 2580)

    def test_without_cold_appliances_the_registry_is_unchanged(self):
        hist = cold_day([Steady(START + 7 * HOUR, START + 7 * HOUR + 240, 2000)])
        on = summary(report_of(hist, config()))
        off = summary(report_of(hist, config(cold=False)))
        a = update_registry(None, {"2027-01-15": on}, END, local_hour=HOUR_OF)
        b = update_registry(None, {"2027-01-15": off}, END, local_hour=HOUR_OF)
        self.assertEqual(a, b)
        self.assertEqual(cold_models(a), [])


# ---------------------------------------------------------------------------
# the benchmark
# ---------------------------------------------------------------------------
PLUG = "sensor.fridge_power"
TZ = __import__("datetime").timezone(__import__("datetime").timedelta(hours=-8))  # DAY0 is midnight there


def export() -> tuple[dict, list]:
    """Four days of a fridge on a plug and a freezer without one."""
    first, last = DAY0 - DAY, DAY0 + 3 * DAY + 6 * HOUR
    cold = Compressor(first, last, 85, 72, 540, 2040, seed=1)
    other = Compressor(first, last, 120, 92, 280, 930, seed=2)
    hist: dict = {GRID: []}
    for i in range(-1, 4):
        day0 = DAY0 + i * DAY
        rows = cold_day([cold, other], day0=day0, seed=40 + i, busy=((19, 21, 40),))[GRID]
        hist[GRID] += [(t, v) for t, v in rows if day0 <= t < day0 + DAY and t < last]
    hist[PLUG] = [(t, f"{cold.power_at(t) * 0.85:.1f}") for t, _ in hist[GRID]]
    return hist, [cold, other]


class BenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hist, (cls.fridge, cls.freezer) = export()
        base = dict(grid_entity=GRID, submeters=[SubmeterConfig(PLUG, "Fridge")], adaptive_threshold=True)
        cls.off = benchmark.run(cls.hist, AnalysisConfig(**base), TZ)
        cls.on = benchmark.run(cls.hist, AnalysisConfig(**base, cold_appliances=True), TZ)

    def test_without_the_option_the_result_has_nothing_of_it(self):
        self.assertNotIn("cold", self.off)
        self.assertNotIn("cold", self.off["thresholds"])
        self.assertNotIn("cold", self.off["devices"][0])
        self.assertNotIn("cold", self.off["energy"])
        self.assertNotIn("cold appliances", benchmark.table(self.off))

    def test_the_real_analysis_has_the_freezer_only(self):
        self.assertEqual(self.on["thresholds"]["cold"], True)
        self.assertEqual(len(self.on["cold"]), 1)
        sig = self.on["cold"][0]
        self.assertAlmostEqual(sig["on_w"], 120, delta=8)
        self.assertEqual(sig["days_seen"], 3)
        # counted apart from the ordinary signatures, which do not move
        self.assertEqual(self.on["signatures"], self.off["signatures"])
        self.assertGreater(self.on["energy"]["cold"], 0)
        self.assertEqual(self.on["energy"]["signatures"], self.off["energy"]["signatures"])
        self.assertLess(self.on["energy"]["unexplained"], self.off["energy"]["unexplained"])

    def test_the_hidden_fridge_is_the_cold_appliance_it_adds(self):
        score = self.on["devices"][0]["cold"]
        self.assertEqual((score["days"], score["days_followed"], score["energy_days"]), (3, 3, 3))
        self.assertGreaterEqual(score["recall"], 0.8)
        self.assertGreaterEqual(score["precision"], 0.9)
        self.assertEqual(score["solar"]["uses"], 0)
        # the plug reads 15 % less than the curve shows: said, not hidden
        self.assertAlmostEqual(score["energy_ratio"], 1 / 0.85, delta=0.12)
        self.assertAlmostEqual(score["worst_day_ratio"], 1 / 0.85, delta=0.15)
        self.assertLess(score["estimated_share"], 0.5)
        self.assertAlmostEqual(score["model"]["on_w"], 85, delta=6)
        # proposed by the registry of the hidden analyses, without its plug
        self.assertAlmostEqual(score["signature"]["on_w"], 85, delta=6)
        self.assertEqual(score["signature"]["days_seen"], 3)
        self.assertFalse(score["signature"]["in_real_analysis"])

    def test_its_runs_count_in_the_registry_free_score(self):
        off, on = self.off["devices"][0]["free"], self.on["devices"][0]["free"]
        self.assertEqual(on["uses"], off["uses"])
        self.assertGreaterEqual(on["found"], off["found"])
        self.assertGreaterEqual(on["recall"], 0.85)

    def test_table_and_comparison(self):
        text = benchmark.table(self.on)
        self.assertIn("cold appliances followed", text)
        self.assertIn("cold appliances of the real analysis", text)
        self.assertIn("the cold appliance a hidden device adds", text)
        self.assertRegex(text, r"signatures 0%, cold \d+%, base")  # its share of the house energy
        self.assertIn("cold appliance of", benchmark.compare(self.off, self.on))
        self.assertNotIn("cold appliance of", benchmark.compare(self.off, self.off))

    def test_cold_score_of_one_day(self):
        run = lambda a, b: argparse.Namespace(start=a, end=b, energy_kwh=0.01)  # noqa: E731
        item = {
            "power_w": 80,
            "on_w": 85,
            "off_w": 72,
            "cycles": [
                {"start": 0, "end": 500, "how": "detected"},
                {"start": 3000, "end": 3500, "how": "filled"},
                {"start": 9000, "end": 9500, "how": "placed"},
            ],
            "estimated": [{"start": 4000, "end": 8000, "power_w": 18.0}],
        }
        other = {
            "power_w": 100,
            "on_w": 120,
            "off_w": 92,
            "cycles": [{"start": 0, "end": 300, "how": "detected"}],
        }
        hidden = {"start": 0, "end": 10000, "cold": [item, other]}
        visible = {"cold": [other]}
        runs = [run(0, 520), run(2700, 3200), run(6000, 6500)]
        score = benchmark.cold_score(runs, None, hidden, visible, solar=lambda ts: ts >= 5000)
        self.assertEqual(score["all"], {"uses": 3, "found": 1, "detections": 3, "true": 1})
        self.assertEqual(score["solar"], {"uses": 1, "found": 0, "detections": 1, "true": 0})
        self.assertAlmostEqual(score["energy_kwh"], (80 * 1500 + 18 * 4000) / 3.6e6)
        self.assertAlmostEqual(score["estimated_kwh"], 18 * 4000 / 3.6e6)
        self.assertTrue(score["whole_day"])
        # the meter of the device reported for a part of the day only
        part = benchmark.cold_score(runs, (2000, 10000), hidden, visible)
        self.assertEqual(part["all"]["detections"], 2)
        self.assertFalse(part["whole_day"])
        # nothing new in the hidden analysis: no cold appliance for the device
        self.assertIsNone(benchmark.cold_score(runs, None, {"start": 0, "end": 1, "cold": [other]}, visible))
        merged = benchmark._cold_merge([score, None], 2, None)
        self.assertEqual((merged["days"], merged["days_followed"], merged["recall"]), (2, 1, 0.333))
        self.assertEqual(benchmark._cold_merge([None], 1, None)["days_followed"], 0)

    def test_command_line(self):
        args = argparse.Namespace(
            grid=GRID, grid_export=None, pv=None, submeter=[], estimated=[], switch=[], scale=[],
            threshold=300.0, night_threshold=None, adaptive=True, cold=True,
        )  # fmt: skip
        self.assertIsNotNone(benchmark.build_config(args).cold_config())
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "empty.csv"
            out.write_text("")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(benchmark.main([str(out), "--grid", GRID, "--adaptive", "--cold"]), 1)
                with self.assertRaises(SystemExit):  # needs the adaptive threshold
                    benchmark.main([str(out), "--grid", GRID, "--cold"])
            self.assertIn("--cold needs --adaptive", err.getvalue())


if __name__ == "__main__":
    unittest.main()
