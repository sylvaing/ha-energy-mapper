"""Long small uses (#110): about 100 W for hours (a television) is found as
one use beside a cold appliance of the same power, by its level when a big
load hides one of its steps, never from two unrelated steps; those that
come back over the days become a signature of kind ``small``. Synthetic
days only (no real data)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import benchmark  # noqa: E402
from appliances import validate as validate_appliances  # noqa: E402
from nilm_core import AnalysisConfig, analyze  # noqa: E402
from nilm_core import small as sm  # noqa: E402
from nilm_core.hourly import hourly_breakdown  # noqa: E402
from nilm_core.registry import annotate_report, expected_signature, small_models  # noqa: E402
from nilm_core.registry import update as update_registry  # noqa: E402
from nilm_core.report import find_steps  # noqa: E402
from nilm_core.signal import build_samples, parse_numeric  # noqa: E402
from nilm_core.steps import Step  # noqa: E402
from synth import DAY0, HOUR, Compressor, Cycling, Steady, cold_day  # noqa: E402

GRID = "sensor.grid_power"
DAY = 24 * HOUR
START, END = DAY0, DAY0 + DAY
HOUR_OF = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731


def config(cold=True, adaptive=True, **kwargs) -> AnalysisConfig:
    return AnalysisConfig(grid_entity=GRID, adaptive_threshold=adaptive, cold_appliances=cold, **kwargs)


def fridge(day0=DAY0) -> Compressor:
    """About 95 W (100 W at the start, 90 W at the end), 9 minutes every 43."""
    return Compressor(day0 - 3 * HOUR, day0 + DAY, 100, 90, 540, 2040, seed=1)


def tv(day0=DAY0, at=20.0, hours=2.0, power=100.0) -> Steady:
    return Steady(day0 + at * HOUR, day0 + (at + hours) * HOUR, power)


def report_of(loads, cfg=None, day0=DAY0, seed=11) -> dict:
    hist = cold_day(loads, day0=day0, seed=seed)
    return analyze(hist, cfg or config(), day0, day0 + DAY, local_hour=HOUR_OF)


def find_steps_of(loads, cfg=None, seed=11) -> list:
    hist = cold_day(loads, seed=seed)
    samples = build_samples(grid=parse_numeric(hist[GRID]), pv=None, submeters=[], estimated=[])
    return find_steps(samples, cfg or config())


def long_uses(report: dict) -> list[dict]:
    return [u for u in report["small_uses"] if u.get("long")]


def stable(report: dict) -> dict:
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


class Ramp:
    """A load with one step only: it fades out after its on-step, or
    builds up to its off-step."""

    def __init__(self, start: float, end: float, power: float, fading: bool):
        self.start, self.end, self.power, self.fading = start, end, power, fading

    def power_at(self, t: float) -> float:
        if not self.start <= t < self.end:
            return 0.0
        share = (t - self.start) / (self.end - self.start)
        return self.power * (1 - share if self.fading else share)


# ---------------------------------------------------------------------------
# two steps: beside a cold appliance of the same power
# ---------------------------------------------------------------------------
class BesideAColdApplianceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alone = report_of([fridge()])
        cls.report = report_of([fridge(), tv()])

    def test_the_use_is_found_whole(self):
        (use,) = long_uses(self.report)
        self.assertAlmostEqual(use["start"], DAY0 + 20 * HOUR, delta=60)
        self.assertAlmostEqual(use["end"], DAY0 + 22 * HOUR, delta=60)
        self.assertAlmostEqual(use["power_w"], 100, delta=10)
        self.assertEqual((use["how"], use["on"], use["off"]), ("detected", "detected", "detected"))

    def test_nothing_else_is_made_of_its_steps(self):
        """No fragment: the television's on-step with the fridge's off-step."""
        self.assertEqual(len(self.report["small_uses"]), 1)

    def test_the_cold_track_is_unchanged(self):
        self.assertEqual(len(self.report["cold"]), 1)
        self.assertEqual(self.report["cold"], self.alone["cold"])
        self.assertEqual(self.alone["small_uses"], [])

    def test_without_the_cold_appliances_it_is_found_too(self):
        """The fridge is then a string of small uses, and its steps around
        the television's are the same raised level seen twice: one use."""
        report = report_of([fridge(), tv()], config(cold=False))
        (use,) = long_uses(report)
        self.assertAlmostEqual(use["start"], DAY0 + 20 * HOUR, delta=60)
        self.assertAlmostEqual(use["end"], DAY0 + 22 * HOUR, delta=60)
        self.assertGreater(len(report["small_uses"]), 20)  # the fridge's runs
        self.assertEqual(long_uses(report_of([fridge()], config(cold=False))), [])

    def test_a_one_off_is_only_listed(self):
        self.assertEqual(self.report["activations"], [])
        self.assertEqual([s for s in self.report["signatures"] if s["kind"] == "small"], [])


# ---------------------------------------------------------------------------
# one step hidden
# ---------------------------------------------------------------------------
class HiddenStepTest(unittest.TestCase):
    def test_an_on_step_hidden_by_a_big_load_is_found_by_the_level(self):
        """A 2 kW load starts with the television and stops ten minutes
        later: one step of 2.1 kW, no on-step of 100 W."""
        heater = Steady(DAY0 + 20 * HOUR, DAY0 + 20 * HOUR + 600, 2000)
        report = report_of([fridge(), tv(), heater])
        (use,) = long_uses(report)
        self.assertEqual((use["how"], use["on"], use["off"]), ("completed", "level", "detected"))
        # the start is where the raised level is first seen: the end of the big load
        self.assertGreaterEqual(use["start"], DAY0 + 20 * HOUR)
        self.assertLessEqual(use["start"], DAY0 + 20 * HOUR + 600 + 180)
        self.assertAlmostEqual(use["end"], DAY0 + 22 * HOUR, delta=60)
        self.assertAlmostEqual(use["power_w"], 100, delta=10)
        self.assertEqual(len(report["cold"]), 1)

    def test_an_off_step_hidden_by_a_big_load(self):
        heater = Steady(DAY0 + 22 * HOUR - 600, DAY0 + 22 * HOUR, 2000)
        (use,) = long_uses(report_of([fridge(), tv(), heater]))
        self.assertEqual((use["how"], use["on"], use["off"]), ("completed", "detected", "level"))
        self.assertAlmostEqual(use["start"], DAY0 + 20 * HOUR, delta=60)
        self.assertGreaterEqual(use["end"], DAY0 + 22 * HOUR - 600 - 180)
        self.assertLessEqual(use["end"], DAY0 + 22 * HOUR)

    def test_a_stop_in_unknown_minutes_is_not_kept(self):
        """The level is unknown for 40 minutes around the end (a cycling
        session): the use ended somewhere in there, which is not enough.
        Five unknown minutes still give an end by the level. With
        ``open_end`` the use is kept as far as it was seen, its end marked
        unknown."""
        points = [(DAY0 + 10 * k, 300.0 + (100.0 if 360 <= k < 1080 else 0.0)) for k in range(1440)]
        on, off = DAY0 + 3600, DAY0 + 10800
        wide = sm.Level(points, masks=[(off - 1200, off + 1200)])
        self.assertIsNone(sm.find_stop(wide, on, 100, True))
        narrow = sm.Level(points, masks=[(off - 240, off + 60)])
        stop, how = sm.find_stop(narrow, on, 100, True)
        self.assertEqual(how, "level")
        self.assertAlmostEqual(stop, off - 240, delta=90)
        opened = dataclasses.replace(sm.SMALL, open_end=True)
        stop, how = sm.find_stop(wide, on, 100, True, opened)
        self.assertEqual(how, "unknown")
        self.assertAlmostEqual(stop, off - 1200, delta=90)
        # seen for less than half an hour before the unknown minutes: nothing
        early = sm.Level(points, masks=[(on + 900, off + 1200)])
        self.assertIsNone(sm.find_stop(early, on, 100, True, opened))

    def test_an_on_step_lost_in_a_cooking_session(self):
        """A hob cycles for 40 minutes and the television starts in the
        middle of it: no on-step, and 40 minutes during which the level is
        not known. The off-step is seen two hours later. By default the use
        is not reported (its start is not known within a quarter of an
        hour); with ``open_end`` it is, from the end of the session, its
        start marked unknown. A session of 8 minutes hides nothing."""
        hob = Cycling(DAY0 + 19.6 * HOUR, DAY0 + 19.6 * HOUR + 2400, 1400, 30, 30)
        loads = [tv(), hob]
        self.assertEqual(long_uses(report_of(loads, config(cold=False))), [])
        opened = config(cold=False, small=dataclasses.replace(sm.SMALL, open_end=True))
        (use,) = long_uses(report_of(loads, opened))
        self.assertEqual((use["how"], use["on"], use["off"]), ("completed", "unknown", "detected"))
        self.assertAlmostEqual(use["start"], hob.end, delta=180)
        self.assertAlmostEqual(use["end"], DAY0 + 22 * HOUR, delta=60)
        short = Cycling(DAY0 + 20 * HOUR - 240, DAY0 + 20 * HOUR + 240, 1400, 30, 30)
        (use,) = long_uses(report_of([tv(), short], config(cold=False)))
        self.assertEqual((use["on"], use["off"]), ("level", "detected"))
        self.assertAlmostEqual(use["start"], short.end, delta=180)

    def test_a_level_that_does_not_come_back_is_not_kept(self):
        """An on-step, then the level drifts away instead of stepping back."""
        up = Steady(DAY0 + 14 * HOUR, DAY0 + 30 * HOUR, 100)
        self.assertEqual(long_uses(report_of([up], config(cold=False))), [])


# ---------------------------------------------------------------------------
# an end in two steps; loads taken out of the level; two loads of one power
# ---------------------------------------------------------------------------
class TwoStage:
    """An appliance that stops (or starts) in two stages ``gap_s`` apart:
    ``share`` of its power first."""

    def __init__(self, start: float, end: float, power: float, share=0.6, gap_s=30.0, at_start=False):
        self.start, self.end, self.power = start, end, power
        self.share, self.gap_s, self.at_start = share, gap_s, at_start

    def power_at(self, t: float) -> float:
        if self.at_start:
            if self.start <= t < self.start + self.gap_s:
                return self.share * self.power
            return self.power if self.start + self.gap_s <= t < self.end else 0.0
        if self.end <= t < self.end + self.gap_s:
            return (1 - self.share) * self.power
        return self.power if self.start <= t < self.end else 0.0


class Settling:
    """A heating element: it draws ``on_w`` when it starts and settles to
    ``off_w`` with the time constant ``tau_s``."""

    def __init__(self, start: float, end: float, on_w: float, off_w: float, tau_s: float = 1200.0):
        self.start, self.end, self.on_w, self.off_w, self.tau_s = start, end, on_w, off_w, tau_s

    def power_at(self, t: float) -> float:
        if not self.start <= t < self.end:
            return 0.0
        return self.off_w + (self.on_w - self.off_w) * math.exp(-(t - self.start) / self.tau_s)


class EdgesAndLoadsTest(unittest.TestCase):
    def test_an_off_in_two_steps_is_one_edge(self):
        """60 W then 40 W half a minute later: neither is the size of the
        100 W on-step, the two together are."""
        load = TwoStage(DAY0 + 20 * HOUR, DAY0 + 22 * HOUR, 100)
        steps = find_steps_of([fridge(), load])
        first, second = (s.delta for s in steps if abs(s.ts - load.end) < 90)
        self.assertAlmostEqual(first + second, -100, delta=15)
        self.assertFalse(sm.SMALL.same_size(100, -first) or sm.SMALL.same_size(100, -second))
        (use,) = long_uses(report_of([fridge(), load]))
        self.assertEqual((use["how"], use["on"], use["off"]), ("detected", "detected", "detected"))
        self.assertAlmostEqual(use["start"], DAY0 + 20 * HOUR, delta=60)
        self.assertAlmostEqual(use["end"], DAY0 + 22 * HOUR + 30, delta=60)
        self.assertAlmostEqual(use["off_w"], 100, delta=10)
        self.assertAlmostEqual(use["power_w"], 100, delta=10)
        # without the merging no pair is made: only what one step and the level give
        alone = config(small=dataclasses.replace(sm.SMALL, merge_s=0))
        self.assertEqual({u["how"] for u in long_uses(report_of([fridge(), load], alone))}, {"completed"})

    def test_an_on_in_two_steps(self):
        load = TwoStage(DAY0 + 20 * HOUR, DAY0 + 22 * HOUR, 100, at_start=True)
        (use,) = long_uses(report_of([fridge(), load]))
        self.assertEqual(use["how"], "detected")
        self.assertAlmostEqual(use["on_w"], 100, delta=10)
        self.assertAlmostEqual(use["start"], DAY0 + 20 * HOUR, delta=60)

    def test_two_steps_far_apart_are_two_edges(self):
        edges = sm.edges_of(
            [
                Step(100.0, -60, 0, 0, low=True),
                Step(130.0, -40, 0, 0, low=True),
                Step(400.0, -50, 0, 0, low=True),
            ]
        )
        self.assertEqual(
            [[round(s.delta) for s in e.steps] for e in edges], [[-60], [-60, -40], [-40], [-50]]
        )
        self.assertEqual(edges[1].size, 100)

    def test_a_heating_load_that_settles_leaves_the_level_flat(self):
        """A 2 kW element starts at 2060 W and stops at 1940 W three hours
        later. The television starts an hour before it and stops half an
        hour before it does: taken out at one power, or along a straight
        line, the heater digs a hole under the use, which is cut short."""
        heater = Settling(DAY0 + 12 * HOUR, DAY0 + 15 * HOUR, 2060, 1940)
        loads = [tv(at=11.0, hours=3.5), heater]
        (use,) = long_uses(report_of(loads, config(cold=False)))
        self.assertAlmostEqual(use["start"], DAY0 + 11 * HOUR, delta=60)
        self.assertAlmostEqual(use["end"], DAY0 + 14.5 * HOUR, delta=60)
        for other in ({"ramp": False}, {"settle_s": 0.0}):
            cfg = config(cold=False, small=dataclasses.replace(sm.SMALL, **other))
            (use,) = long_uses(report_of(loads, cfg))
            self.assertLess(use["end"], DAY0 + 12.5 * HOUR, other)

    def test_the_power_taken_out_goes_from_one_step_to_the_other(self):
        runs = sm._Runs([(0.0, 6000.0, 2060.0, 1940.0)], settle_s=1500.0)
        self.assertAlmostEqual(runs(0.0), 2060, delta=0.5)
        self.assertAlmostEqual(runs(5999.0), 1940, delta=0.5)
        self.assertLess(runs(1500.0), 2000)  # most of the way down after one time constant
        line = sm._Runs([(0.0, 6000.0, 2060.0, 1940.0)])
        self.assertAlmostEqual(line(3000.0), 2000, delta=0.5)
        self.assertEqual(line(6000.0), 0.0)

    def test_two_loads_of_the_same_power(self):
        """One runs from 19:00 to 21:00, the other from 20:00 to 22:00. At
        21:00 an off-step of their power arrives and the level stays
        raised: nothing ends there for the load that is still on. The level
        cannot tell which of the two went off: the uses reported are the
        long one and the hour both were on, the same energy."""
        report = report_of([tv(at=19.0), tv(at=20.0)], config(cold=False))
        uses = long_uses(report)
        spans = [(round((u["start"] - DAY0) / HOUR, 1), round((u["end"] - DAY0) / HOUR, 1)) for u in uses]
        self.assertEqual(spans, [(19.0, 22.0), (20.0, 21.0)])
        self.assertAlmostEqual(
            sum(u["power_w"] * (u["end"] - u["start"]) for u in uses) / 3.6e6, 0.4, delta=0.03
        )


# ---------------------------------------------------------------------------
# what is not a long small use
# ---------------------------------------------------------------------------
class UnrelatedStepsTest(unittest.TestCase):
    def test_two_unrelated_low_steps_hours_apart_are_not_paired(self):
        """An on-step at 14:00 of a load that fades out, an off-step at
        18:00 of another one that had built up: same size, four hours."""
        loads = [
            Ramp(DAY0 + 14 * HOUR, DAY0 + 14.6 * HOUR, 100, fading=True),
            Ramp(DAY0 + 17.4 * HOUR, DAY0 + 18 * HOUR, 100, fading=False),
        ]
        for cold in (True, False):
            report = report_of([*loads, fridge()] if cold else loads, config(cold=cold))
            self.assertEqual(long_uses(report), [], cold)
            self.assertEqual([u for u in report["small_uses"] if u["end"] - u["start"] > 3 * HOUR], [], cold)

    def test_a_step_paired_with_the_step_of_another_use_is_not_one(self):
        """The level tells the pairs apart: the on-step of one use with the
        off-step of a shorter one that ran during it is refused."""
        inner = Steady(DAY0 + 20.5 * HOUR, DAY0 + 21.2 * HOUR, 95)
        uses = long_uses(report_of([tv(), inner], config(cold=False)))
        spans = sorted(
            (round((u["start"] - DAY0) / HOUR, 1), round((u["end"] - DAY0) / HOUR, 1)) for u in uses
        )
        self.assertEqual(spans, [(20.0, 22.0), (20.5, 21.2)])

    def test_the_rule_of_the_level(self):
        points = [(DAY0 + 10 * k, 300.0 + (100.0 if 360 <= k < 1080 else 0.0)) for k in range(1440)]
        level = sm.Level(points)
        on, off = DAY0 + 3600, DAY0 + 10800
        self.assertTrue(sm.pair_ok(level, on, off, 100))
        self.assertFalse(sm.pair_ok(level, on, off + 1800, 100))  # back down before the off-step
        self.assertFalse(sm.pair_ok(level, on - 1800, off, 100))  # not raised yet after the on-step
        self.assertFalse(sm.pair_ok(level, on, off, 200))  # raised by half the step only
        self.assertAlmostEqual(sm.find_stop(level, on, 100, True)[0], off, delta=60)
        self.assertAlmostEqual(sm.find_stop(level, off, 100, False)[0], on, delta=60)
        self.assertIsNone(sm.find_stop(level, on, 200, True))
        # the minutes of a use of the fixed thresholds are unknown
        masked = sm.Level(points, masks=[(on + 600, off - 600)])
        self.assertFalse(sm.pair_ok(masked, on, off, 100))
        self.assertIsNone(sm.find_stop(masked, on, 100, True))


# ---------------------------------------------------------------------------
# signatures: the uses that come back
# ---------------------------------------------------------------------------
def summary(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "curve"}


def days_of_tv(sessions: list[list[tuple[float, float]]], power=100.0):
    """The days analysed in order, the registry carried: ``(reports,
    registries)``; ``sessions``: per day, the ``(hour, hours)`` of the uses."""
    reports, registries = {}, []
    registry = None
    for i, uses in enumerate(sessions):
        day0 = DAY0 + i * DAY
        day = f"2027-01-{15 + i:02d}"
        loads = [fridge(day0), *(tv(day0, at, hours, power) for at, hours in uses)]
        cfg = config(small_models=small_models(registry))
        reports[day] = report_of(loads, cfg, day0, seed=30 + i)
        reports[day]["day"] = day
        registry = update_registry(
            registry, {d: summary(r) for d, r in reports.items()}, day0 + DAY, local_hour=HOUR_OF
        )
        registries.append(registry)
    return reports, registries


class SignatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports, cls.registries = days_of_tv(
            [[(20, 2)], [(13, 1.5), (20.5, 2.5)], [(19, 3)], [(19, 3)], [(21, 1)]]
        )
        cls.days = sorted(cls.reports)

    def small(self, registry: dict) -> list[dict]:
        return [s for s in registry["signatures"] if s["kind"] == "small"]

    def test_a_one_off_makes_no_signature(self):
        self.assertEqual(self.small(self.registries[0]), [])
        self.assertEqual(small_models(self.registries[0]), [])
        self.assertEqual(len(long_uses(self.reports[self.days[0]])), 1)
        # three uses on two days are not enough either
        self.assertEqual(self.small(self.registries[1]), [])
        self.assertEqual(len(long_uses(self.reports[self.days[1]])), 2)

    def test_several_days_make_one_signature(self):
        for registry in self.registries[2:]:
            (sig,) = self.small(registry)
            self.assertEqual((sig["kind"], sig["band"]), ("small", "small"))
            self.assertRegex(sig["id"], r"^small-\d+w$")
            self.assertAlmostEqual(sig["power_w"], 100, delta=10)
        ids = {self.small(r)[0]["id"] for r in self.registries[2:]}
        self.assertEqual(len(ids), 1)  # the same one from day to day
        sig = self.small(self.registries[-1])[0]
        self.assertEqual((sig["count"], sig["days_seen"]), (6, 5))
        self.assertEqual(len(sig["occurrences"]), 6)
        self.assertAlmostEqual(sig["duration_s"], 2.5 * HOUR, delta=0.6 * HOUR)
        self.assertAlmostEqual(sig["energy_kwh"], 1.3, delta=0.15)
        self.assertEqual(small_models(self.registries[-1]), [{"id": sig["id"], "power_w": sig["power_w"]}])
        # the cold appliance is still the only other signature
        self.assertEqual({s["kind"] for s in self.registries[-1]["signatures"]}, {"small", "cold"})

    def test_once_known_its_uses_are_activations_of_the_day(self):
        report = self.reports[self.days[3]]
        self.assertEqual(long_uses(report), [])
        (act,) = report["activations"]
        self.assertTrue(act["small"])
        self.assertEqual((act["how"], act["on"], act["off"]), ("detected", "detected", "detected"))
        self.assertAlmostEqual(act["start"], DAY0 + 3 * DAY + 19 * HOUR, delta=60)
        (sig,) = [s for s in report["signatures"] if s["kind"] == "small"]
        self.assertEqual((sig["id"], sig["band"], sig["count"]), (act["signature"], "small", 1))
        # its energy is in the day's split, out of the unexplained share
        self.assertAlmostEqual(report["energy_kwh"]["signatures"][sig["id"]], 0.3, delta=0.04)
        self.assertEqual(sum(r["signatures"].get(sig["id"], 0) > 0 for r in report["curve"]), 180)

    def test_the_report_and_the_hours_carry_the_registry_id(self):
        registry = self.registries[-1]
        rid = self.small(registry)[0]["id"]
        report = annotate_report(dict(self.reports[self.days[3]]), registry)
        self.assertEqual(report["activations"][0]["registry_id"], rid)
        self.assertEqual([s["registry_id"] for s in report["signatures"] if s["kind"] == "small"], [rid])
        hourly = hourly_breakdown(report, registry)
        self.assertAlmostEqual(hourly["energy_kwh"]["signatures"][rid], 0.3, delta=0.04)
        self.assertEqual(hourly["uses"]["signatures"][rid], 1)

    def test_another_power_is_not_that_signature(self):
        cfg = config(small_models=small_models(self.registries[-1]))
        report = report_of([fridge(), tv(power=60.0)], cfg)
        self.assertEqual(report["activations"], [])
        (use,) = long_uses(report)
        self.assertAlmostEqual(use["power_w"], 60, delta=8)

    def test_uses_on_one_day_only_make_none(self):
        _, registries = days_of_tv([[(9, 1), (13, 1.5), (17, 1), (20, 2)], []])
        self.assertEqual(self.small(registries[-1]), [])

    def test_it_can_be_named_and_made_an_appliance(self):
        sig = self.small(self.registries[-1])[0]
        source = {"type": "signature", "id": sig["id"], "kind": "small", "band": "small", "power_w": 100}
        (appliance,) = validate_appliances(
            {"appliances": [{"key": "tv", "name": "Television", "sources": [source]}]}
        )
        self.assertEqual(appliance["sources"][0]["kind"], "small")
        expected = expected_signature({"signatures": []}, "small", "small", 100, "Television", 0.0)
        self.assertEqual(
            (expected["kind"], expected["band"], expected["label"]), ("small", "small", "Television")
        )
        # a label keeps it through a registry update, and the id stays
        labelled = {
            **self.registries[-1],
            "signatures": [
                {**s, "label": "Television"} if s["id"] == sig["id"] else s
                for s in self.registries[-1]["signatures"]
            ],
        }
        again = update_registry(
            labelled, {d: summary(r) for d, r in self.reports.items()}, DAY0 + 5 * DAY, local_hour=HOUR_OF
        )
        self.assertEqual([(s["id"], s["label"]) for s in self.small(again)], [(sig["id"], "Television")])


# ---------------------------------------------------------------------------
# the option
# ---------------------------------------------------------------------------
class OptionOffTest(unittest.TestCase):
    def test_without_the_adaptive_threshold_nothing_changes(self):
        loads = [fridge(), tv(), Steady(DAY0 + 12 * HOUR, DAY0 + 12 * HOUR + 600, 2000)]
        off = report_of(loads, config(adaptive=False, cold=False))
        self.assertNotIn("small_uses", off)
        self.assertEqual([a["power_w"] for a in off["activations"]], [2000])
        models = [{"id": "small-100w", "power_w": 100}]
        known = report_of(loads, config(adaptive=False, cold=False, small_models=models))
        self.assertEqual(stable(known), stable(off))
        # with it, what the fixed thresholds find is the same
        on = report_of(loads, config(cold=False))
        for key in ("activations", "signatures", "known_loads", "peaks", "energy_kwh", "curve"):
            self.assertEqual(on[key], off[key], key)


# ---------------------------------------------------------------------------
# the benchmark: a plug that is only the truth
# ---------------------------------------------------------------------------
PLUG = "sensor.tv_plug_power"
TZ = __import__("datetime").timezone(__import__("datetime").timedelta(hours=-8))  # DAY0 is midnight there


def export() -> dict:
    """Five days of a fridge and a television; the television has a plug
    the analysis does not know."""
    first, last = DAY0 - DAY, DAY0 + 4 * DAY + 6 * HOUR
    cold = Compressor(first, last, 100, 90, 540, 2040, seed=1)
    sets = [
        tv(DAY0 + i * DAY, at, hours)
        for i, (at, hours) in enumerate([(20, 2), (19, 3), (20.5, 1.5), (18, 2)])
    ]
    sets.append(tv(DAY0 + DAY, 13, 1.5))  # a second one on the second day
    hist: dict = {GRID: []}
    for i in range(-1, 5):
        day0 = DAY0 + i * DAY
        rows = cold_day([cold, *sets], day0=day0, seed=60 + i)[GRID]
        hist[GRID] += [(t, v) for t, v in rows if day0 <= t < day0 + DAY and t < last]
    hist[PLUG] = [(t, f"{sum(s.power_at(t) for s in sets) * 0.93:.1f}") for t, _ in hist[GRID][::3]]
    return hist


class BenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hist = export()
        cls.cfg = AnalysisConfig(grid_entity=GRID, adaptive_threshold=True, cold_appliances=True)
        cls.result = benchmark.run(cls.hist, cls.cfg, TZ, truths=[(PLUG, "Television")])

    def test_the_plug_is_the_truth_and_nothing_else(self):
        (t,) = self.result["truths"]
        self.assertEqual((t["name"], t["uses"], t["found"], t["whole"]), ("Television", 5, 5, 5))
        self.assertEqual((t["detections"], t["true"], t["precision"]), (5, 5, 1.0))
        self.assertEqual((t["long_detections"], t["long_true"]), (5, 5))
        self.assertAlmostEqual(t["power_w"], 93, delta=2)
        # the plug reads 7 % less than the curve shows
        self.assertAlmostEqual(t["energy_ratio"], 1 / 0.93, delta=0.12)
        # all of it within the plug's runs, nothing outside them
        self.assertAlmostEqual(t["inside_ratio"], 1 / 0.93, delta=0.12)
        self.assertAlmostEqual(t["outside_ratio"], 0.0, delta=0.02)
        self.assertAlmostEqual(t["inside_kwh"] + t["outside_kwh"], t["energy_kwh"], delta=0.002)
        self.assertEqual(self.result["devices"], [])  # nothing was hidden: it is no sub-meter

    def test_the_signature_it_became(self):
        (sig,) = self.result["small"]
        self.assertEqual((sig["count"], sig["days_seen"]), (5, 4))
        self.assertEqual(self.result["truths"][0]["signature"], sig)
        self.assertGreater(self.result["truths"][0]["signature_kwh"], 0)
        self.assertGreater(self.result["energy"]["small"], 0)
        self.assertEqual(self.result["signatures"], 0)  # counted apart, as the cold ones
        self.assertEqual(len(self.result["cold"]), 1)

    def test_without_the_option_there_is_nothing_of_it(self):
        off = benchmark.run(self.hist, AnalysisConfig(grid_entity=GRID), TZ, truths=[(PLUG, "Television")])
        for key in ("truths", "small", "long_small_uses_per_day"):
            self.assertNotIn(key, off)
        self.assertNotIn("small", off["energy"])
        self.assertNotIn("long small uses", benchmark.table(off))

    def test_energy_inside_and_outside_the_runs(self):
        """Two detections at the same time count once inside the runs (the
        larger); what lies outside them is summed."""
        run = benchmark.runs_of([(0.0, 0.0), (1000.0, 100.0), (8200.0, 0.0), (20000.0, 0.0)], 0.0, 20000.0)[0]
        self.assertEqual([(r.start, r.end) for r in run], [(1000.0, 8200.0)])
        report = {
            "start": 0.0,
            "end": 20000.0,
            "activations": [],
            "small_uses": [
                {"start": 1000.0, "end": 4600.0, "power_w": 100, "long": True},  # inside
                {
                    "start": 1900.0,
                    "end": 5600.0,
                    "power_w": 90,
                    "long": True,
                },  # inside, partly at the same time
                {"start": 6400.0, "end": 10000.0, "power_w": 100, "long": True},  # half inside
                {"start": 12000.0, "end": 15600.0, "power_w": 110, "long": True},  # outside
            ],
        }
        score = benchmark.truth_score(run, 100.0, 7200.0, None, report)
        self.assertAlmostEqual(score["inside_kwh"], (100 * 3600 + 90 * 1000 + 100 * 1800) / 3.6e6)
        self.assertAlmostEqual(score["outside_kwh"], (100 * 1800 + 110 * 3600) / 3.6e6)
        self.assertEqual((score["all"]["long_detections"], score["all"]["long_true"]), (4, 3))

    def test_table_and_comparison(self):
        text = benchmark.table(self.result)
        self.assertIn("long small uses", text)
        self.assertIn("appliances left in the curve", text)
        self.assertRegex(text, r"Television\s+93 W\s+5\s+5\s+5\s+100%")
        self.assertIn("left in the curve", benchmark.compare(self.result, self.result))

    def test_command_line(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            benchmark.main(["x.csv", "--grid", GRID, "--truth", PLUG])


if __name__ == "__main__":
    unittest.main()
