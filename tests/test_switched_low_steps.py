"""The low steps a switched load's transitions make (#99): with the adaptive
threshold (#88) they are explained by the load, so they are never paired
into a small use nor read by the cold appliances (#89). Synthetic days only
(no real data)."""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from nilm_core import AnalysisConfig, SwitchedLoadConfig, analyze  # noqa: E402
from nilm_core.known import KnownLoad, explain_low_steps  # noqa: E402
from nilm_core.steps import Step  # noqa: E402
from synth import DAY0, HOUR, Compressor, Steady, cold_day, small_loads_day, switch_history  # noqa: E402

GRID = "sensor.grid_power"
PUMP = "switch.pump"
START, END = DAY0, DAY0 + 24 * HOUR
HOUR_OF = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731

# a 100 W pump on a relay, at no regular time, never at an edge of the fridge
# of small_loads_day (on from hh:00 to hh:20)
PUMP_RUNS = [
    (DAY0 + h * HOUR + m * 60, DAY0 + h * HOUR + m * 60 + length)
    for h, m, length in (
        (1, 27, 1500),
        (3, 31, 900),
        (4, 24, 1700),
        (6, 40, 600),
        (9, 26, 1300),
        (11, 33, 1100),
        (14, 25, 1900),
        (15, 29, 800),
        (19, 36, 1000),
        (22, 23, 1600),
    )
]


def config(adaptive=True, loads=(), **kwargs) -> AnalysisConfig:
    return AnalysisConfig(grid_entity=GRID, adaptive_threshold=adaptive, switched_loads=list(loads), **kwargs)


def with_loads(hist: dict, *loads) -> dict:
    """The day with ``loads`` (anything with ``power_at``) added to the grid."""
    out = dict(hist)
    out[GRID] = [(t, f"{float(v) + sum(x.power_at(t) for x in loads):.1f}") for t, v in hist[GRID]]
    return out


def pump_day(power: float = 100.0, states=("on", "off"), hist: dict | None = None) -> dict:
    hist = with_loads(hist or small_loads_day(), *(Steady(a, b, power) for a, b in PUMP_RUNS))
    hist[PUMP] = switch_history(PUMP_RUNS, states)
    return hist


def report_of(hist, cfg) -> dict:
    return analyze(hist, cfg, START, END, local_hour=HOUR_OF)


def stable(report: dict) -> dict:
    out = dict(report)
    out.pop("generated_at")
    out["quality"] = {k: v for k, v in report["quality"].items() if k != "compute_s"}
    return out


def without_option(report: dict) -> dict:
    out = stable(report)
    out.pop("small_uses")
    out["quality"].pop("low_steps")
    return out


def of_the_pump(use: dict) -> bool:
    """A small use with an edge at a transition of the pump."""
    return any(abs(use[k] - t) <= 30 for k in ("start", "end") for run in PUMP_RUNS for t in run)


def is_fridge(report: dict, test: unittest.TestCase) -> None:
    """The small uses are the 24 cycles of the 80 W fridge, and nothing else."""
    small = report["small_uses"]
    test.assertEqual(len(small), 24, [(u["start"] - DAY0, u["power_w"]) for u in small])
    for i, use in enumerate(small):
        test.assertAlmostEqual(use["start"], START + i * HOUR, delta=20)
        test.assertAlmostEqual(use["end"] - use["start"], 1200, delta=30)
        test.assertTrue(70 <= use["power_w"] <= 90, use)


# ---------------------------------------------------------------------------
class PumpBehindASwitchTest(unittest.TestCase):
    """A 100 W pump on a relay next to an unmetered 80 W fridge."""

    @classmethod
    def setUpClass(cls):
        cls.hist = pump_day()
        cls.pump = SwitchedLoadConfig(PUMP, "Pump")
        cls.on = report_of(cls.hist, config(loads=[cls.pump]))

    def test_unknown_the_pump_makes_small_uses(self):
        # the relay is not configured: its steps are low steps like any other
        small = report_of(self.hist, config())["small_uses"]
        self.assertGreaterEqual(len([u for u in small if of_the_pump(u)]), len(PUMP_RUNS) - 2)

    def test_configured_it_makes_none_and_the_fridge_is_still_found(self):
        self.assertEqual([u for u in self.on["small_uses"] if of_the_pump(u)], [])
        is_fridge(self.on, self)
        # its steps are still low steps of the curve: only their explanation changed
        self.assertEqual(self.on["quality"]["low_steps"], 48 + 2 * len(PUMP_RUNS))

    def test_a_relay_that_runs_it_when_off(self):
        hist = pump_day(states=("off", "on"))  # the pump runs while the relay is off
        is_fridge(report_of(hist, config(loads=[self.pump])), self)
        # told not to look at the other polarity, the relay explains nothing
        # (the rule of the other steps): the pump's steps go the wrong way
        fixed = SwitchedLoadConfig(PUMP, "Pump", auto_polarity=False)
        small = report_of(hist, config(loads=[fixed]))["small_uses"]
        self.assertGreaterEqual(len([u for u in small if of_the_pump(u)]), len(PUMP_RUNS) - 2)

    def test_a_switch_that_drives_nothing_explains_nothing(self):
        # same relay, no pump on the curve: the fridge's steps are not taken
        hist = small_loads_day()
        hist[PUMP] = switch_history(PUMP_RUNS)
        is_fridge(report_of(hist, config(loads=[self.pump])), self)
        # nor when it switches right at the fridge's edges, with another power known
        hist[PUMP] = switch_history([(START + h * HOUR, START + h * HOUR + 1200) for h in range(24)])
        low = [Step(START + h * HOUR, 80.0, 250, 330, low=True) for h in range(24)]
        known = KnownLoad("Pump", PUMP, 320.0, matched=4)
        self.assertEqual(explain_low_steps(self.pump, hist[PUMP], low, END, known, 20.0), [])
        self.assertTrue(all(s.explained_by is None for s in low))

    def test_the_load_itself_is_left_as_it_is(self):
        # leaving the steps out is all that is done: no power is measured from them
        (load,) = self.on["known_loads"]
        self.assertEqual((load["power_w"], load["matched"], load["on_s"]), (0, 0, 0))
        self.assertEqual(self.on["energy_kwh"]["known"], {})

    def test_nothing_else_changes(self):
        off = report_of(self.hist, config(False, loads=[self.pump]))
        self.assertNotIn("small_uses", off)
        self.assertEqual(without_option(self.on), stable(off))
        # and the option off does not read the relay any differently
        self.assertEqual(off["known_loads"][0]["matched"], 0)
        self.assertEqual([round(a["power_w"], -1) for a in off["activations"]], [2000, 2000])


# ---------------------------------------------------------------------------
class RuleTest(unittest.TestCase):
    """:func:`explain_low_steps` on hand-made steps."""

    HIST = switch_history([(START + 1000, START + 2000), (START + 5000, START + 6000)])

    def low(self, *edges: tuple[float, float]) -> list[Step]:
        return [Step(START + t, d, 250, 250 + d, low=True) for t, d in edges]

    def test_the_power_is_learned_from_the_low_steps(self):
        low = self.low((700, 60), (1005, 100), (1990, -95), (3000, -80), (5010, 104), (6005, -99))
        marked = explain_low_steps(SwitchedLoadConfig(PUMP, "Pump"), self.HIST, low, END, None, 20.0)
        self.assertEqual([s.ts - START for s in marked], [1005, 5010, 1990, 6005])
        self.assertEqual([s.explained_by for s in low], [None, "Pump", "Pump", None, "Pump", "Pump"])

    def test_a_step_of_another_size_or_sign_is_not_its_own(self):
        low = self.low((1005, 100), (1990, -100), (5010, 180), (6005, 100))
        explain_low_steps(SwitchedLoadConfig(PUMP, "Pump"), self.HIST, low, END, None, 20.0)
        self.assertEqual([s.explained_by for s in low], ["Pump", "Pump", None, None])

    def test_a_load_the_fixed_thresholds_measured_keeps_its_power(self):
        # 310 W, just above the 300 W threshold: one of its edges fell under it
        known = KnownLoad("Pump", PUMP, 310.0, matched=3)
        low = self.low((1005, 80), (1990, -285), (5010, 90))
        explain_low_steps(SwitchedLoadConfig(PUMP, "Pump"), self.HIST, low, END, known, 20.0)
        self.assertEqual([s.explained_by for s in low], [None, "Pump", None])
        # in the polarity the fixed thresholds settled
        known = KnownLoad("Pump", PUMP, 310.0, matched=3, on_states=("off",))
        low = self.low((1005, -100), (1995, 290))
        explain_low_steps(SwitchedLoadConfig(PUMP, "Pump"), self.HIST, low, END, known, 20.0)
        self.assertEqual([s.explained_by for s in low], [None, "Pump"])

    def test_a_step_already_explained_stays(self):
        low = self.low((1005, 100), (1990, -100))
        low[0].explained_by = "Plug"
        explain_low_steps(SwitchedLoadConfig(PUMP, "Pump"), self.HIST, low, END, None, 20.0)
        self.assertEqual([s.explained_by for s in low], ["Plug", "Pump"])

    def test_an_operation_state_marks_no_step(self):
        hist = [(t, "run" if s == "on" else "stop") for t, s in self.HIST]
        low = self.low((1005, 100), (1990, -100))
        cfg = SwitchedLoadConfig("sensor.washer_state", "Washer", ("run",))
        self.assertEqual(explain_low_steps(cfg, hist, low, END, None, 20.0), [])
        self.assertEqual([s.explained_by for s in low], [None, None])


# ---------------------------------------------------------------------------
class ColdApplianceTest(unittest.TestCase):
    """The pump's steps do not feed a cold model; the fridge is still one."""

    @classmethod
    def setUpClass(cls):
        cls.fridge = Compressor(DAY0 - 3 * HOUR, END, 85, 72, 540, 2040, seed=1)
        # the pump as a regular 100 W cycle of its own: 8 minutes every 31
        cls.runs = [(DAY0 - 2 * HOUR + k * 1860, DAY0 - 2 * HOUR + k * 1860 + 480) for k in range(50)]
        cls.hist = with_loads(cold_day([cls.fridge]), *(Steady(a, b, 100.0) for a, b in cls.runs))
        cls.hist[PUMP] = switch_history(cls.runs)

    def cold(self, loads) -> list[dict]:
        report = report_of(self.hist, config(loads=loads, cold_appliances=True))
        return sorted(report["cold"], key=lambda c: c["on_w"])

    def test_unknown_the_pump_is_learned_as_a_cold_appliance(self):
        self.assertEqual([round(c["on_w"], -1) for c in self.cold([])], [80, 100])

    def test_configured_only_the_fridge_is_one(self):
        report = report_of(self.hist, config(loads=[SwitchedLoadConfig(PUMP, "Pump")], cold_appliances=True))
        (item,) = report["cold"]
        self.assertTrue(75 <= item["on_w"] <= 95 and 62 <= item["off_w"] <= 82, item)
        runs = [r for r in self.fridge.runs if r[1] > START and r[0] < END]
        self.assertAlmostEqual(len(item["cycles"]), len(runs), delta=2)
        self.assertEqual([s["kind"] for s in report["signatures"]].count("cold"), 1)
        self.assertEqual([u for u in report["small_uses"] if abs(u["power_w"] - 100) <= 8], [])


if __name__ == "__main__":
    unittest.main()
