"""Tests of connected appliances as ground truth (#79): an operation-state
switched load measured over its runs (ramping start near the threshold,
multi-phase cycles, relays unchanged), the teacher without a power sensor
(runs from the state, phases, programmes, a coarse energy counter), flat
power sensors, and the add-on end to end. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api import route  # noqa: E402
from appliances import AppliancesError, analysis_options, teacher_entities, validate  # noqa: E402
from energy_config import derive, flat_power_sensors, without_flat  # noqa: E402
from nilm_core import AnalysisConfig, SwitchedLoadConfig, analyze  # noqa: E402
from nilm_core.groundtruth import MULTIPHASE, STEADY, learn, state_runs  # noqa: E402
from nilm_core.known import RUN, STEP  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady  # noqa: E402
from synth_cycles import dishwasher  # noqa: E402
from teaching import day_state_truth, program_signatures, residual_curve, status  # noqa: E402
from test_addon import TODAY, FakeHA, make_analyzer, midnight, snapshot  # noqa: E402
from test_energy_config import energy, power  # noqa: E402

DAY = 24 * HOUR
MIN = 60.0
STATE = "sensor.dryer_operation_state"
JOB = "sensor.dryer_job_state"
COUNTER = "sensor.dryer_energy"
FLAT = "sensor.dryer_power"
DW_STATE = "sensor.dishwasher_operation_state"
DW_PHASE = "sensor.dishwasher_phase"
DW_PROGRAM = "select.dishwasher_program"
GRID = "sensor.grid_power"


@dataclass
class Ramp:
    """A load rising gradually over ``ramp_s`` to ``power`` (no step), with a
    slow wobble, then falling back over ``ramp_s``: a heat-pump dryer."""

    start: float
    end: float
    power: float
    ramp_s: float = 180.0
    wobble_w: float = 30.0

    def power_at(self, t: float) -> float:
        if not self.start <= t < self.end:
            return 0.0
        rise = min((t - self.start) / self.ramp_s, (self.end - t) / self.ramp_s, 1.0)
        wobble = self.wobble_w * (((t - self.start) // 600) % 2 * 2 - 1)
        return max(rise * (self.power + wobble), 0.0)


class Sum:
    def __init__(self, *loads):
        self.loads = loads

    def power_at(self, t):
        return sum(x.power_at(t) for x in self.loads)


def state_history(periods, on="run", off="stop", before=None):
    out = [] if before is None else [(before, off)]
    for a, b in periods:
        out += [(a, on), (b, off)]
    return out


def residual_points(house: House, start: float, end: float, dt: float = 10.0):
    """The residual load as the analysis sees it (no sub-meter)."""
    return [(t, float(v)) for t, v in house.histories(start, end, dt=dt)[GRID]]


def energy_of(load, a, b, dt=10.0):
    t, e = a, 0.0
    while t < b:
        e += load.power_at(t) * dt
        t += dt
    return e / 3.6e6


def counter_series(load, start, end, step_kwh=0.1, every_s=900.0, origin=12.34):
    """A coarse cumulative counter: kWh truncated to ``step_kwh``, reported
    every ``every_s`` (strings, as recorded); ``origin`` is what it counted
    before ``start``."""
    out, t, e = [], start, 0.0
    while t < end:
        out.append((t, f"{int((origin + e) / step_kwh) * step_kwh:.1f}"))
        e += energy_of(load, t, t + every_s)
        t += every_s
    return out


# ---------------------------------------------------------------------------
# switched load from an operation state (run mode)
# ---------------------------------------------------------------------------
class RunModeTests(unittest.TestCase):
    def setUp(self):
        self.start, self.end = DAY0, DAY0 + DAY
        self.dryer = Ramp(DAY0 + 14 * HOUR, DAY0 + 14 * HOUR + 100 * MIN, 330.0)
        fridge = Cycling(DAY0 - 3 * HOUR, DAY0 + DAY, 90.0, 900, 1500)
        self.house = House(base_w=250, loads=[self.dryer, fridge], noise_w=10, seed=3)
        self.hist = self.house.histories(self.start - 3 * HOUR, self.end, dt=10)
        self.hist[STATE] = state_history([(self.dryer.start, self.dryer.end)], before=DAY0 - 3 * HOUR)

    def config(self, mode=None, entity=STATE):
        cfg = AnalysisConfig(grid_entity=GRID)
        cfg.switched_loads = [SwitchedLoadConfig(entity, "Dryer", ("run",), mode=mode)]
        return cfg

    def test_mode_by_domain(self):
        self.assertEqual(SwitchedLoadConfig(STATE, "x").measure, RUN)
        self.assertEqual(SwitchedLoadConfig("switch.heater", "x").measure, STEP)
        self.assertEqual(SwitchedLoadConfig("binary_sensor.run", "x").measure, STEP)
        self.assertEqual(SwitchedLoadConfig(STATE, "x", mode=STEP).measure, STEP)

    def test_ramp_near_threshold_is_invisible_to_the_steps(self):
        report = analyze(self.hist, self.config(mode=STEP), self.start, self.end)
        (k,) = report["known_loads"]
        self.assertEqual(k["matched"], 0)
        self.assertEqual(report["energy_kwh"]["known"].get("Dryer", 0.0), 0.0)

    def test_ramp_measured_over_the_run(self):
        truth = energy_of(self.dryer, self.dryer.start, self.dryer.end)
        report = analyze(self.hist, self.config(), self.start, self.end)
        (k,) = report["known_loads"]
        self.assertEqual((k["mode"], k["runs"], k["matched"]), (RUN, 1, 2))
        self.assertAlmostEqual(k["power_w"], 330, delta=40)
        got = report["energy_kwh"]["known"]["Dryer"]
        self.assertAlmostEqual(got, truth, delta=0.1 * truth)
        self.assertAlmostEqual(k["on_s"], 100 * MIN, delta=1)
        # what the step mode left unexplained is now the dryer's
        before = analyze(self.hist, self.config(mode=STEP), self.start, self.end)["energy_kwh"]["other"]
        self.assertAlmostEqual(before - report["energy_kwh"]["other"], got, delta=0.05)

    def test_a_run_without_power_does_not_count(self):
        hist = dict(self.hist)
        hist[STATE] = state_history([(DAY0 + 3 * HOUR, DAY0 + 4 * HOUR)], before=DAY0 - 3 * HOUR)
        report = analyze(hist, self.config(), self.start, self.end)
        (k,) = report["known_loads"]
        self.assertEqual((k["runs"], k["matched"], k["transitions"]), (0, 0, 2))
        self.assertNotIn("Dryer", report["energy_kwh"]["known"])

    def test_pause_inside_a_run(self):
        a, b = self.dryer.start, self.dryer.end
        hist = dict(self.hist)
        hist[STATE] = [
            (DAY0 - 3 * HOUR, "stop"),
            (a, "run"),
            (a + 40 * MIN, "pause"),
            (a + 43 * MIN, "run"),
            (b, "stop"),
        ]
        report = analyze(hist, self.config(), self.start, self.end)
        (k,) = report["known_loads"]
        self.assertEqual(k["runs"], 1)
        truth = energy_of(self.dryer, a, b)
        self.assertAlmostEqual(report["energy_kwh"]["known"]["Dryer"], truth, delta=0.1 * truth)

    def test_multiphase_run_measured_whole(self):
        rnd = random.Random(5)
        cycle = dishwasher(DAY0 + 19 * HOUR, rnd)
        load = Sum(*cycle.loads)
        a, b = cycle.phases[0][1], cycle.phases[-1][2]
        house = House(base_w=250, loads=[load], noise_w=10, seed=4)
        hist = house.histories(self.start - 3 * HOUR, self.end, dt=10)
        hist[DW_STATE] = state_history([(a, b)], before=DAY0 - 3 * HOUR)
        report = analyze(hist, self.config(entity=DW_STATE), self.start, self.end)
        truth = energy_of(load, a, b)
        self.assertAlmostEqual(report["energy_kwh"]["known"]["Dryer"], truth, delta=0.08 * truth)
        # transparent to the detection: the heating phases are still activations
        self.assertTrue(any(x["power_w"] > 1500 for x in report["activations"]))
        self.assertTrue(report["cycles"])

    def test_relays_unchanged(self):
        """A relay with sharp steps is measured from its steps, as before."""
        heater = Steady(DAY0 + 2 * HOUR, DAY0 + 3 * HOUR, 2000)
        house = House(base_w=250, loads=[heater], noise_w=10, seed=5)
        hist = house.histories(self.start - 3 * HOUR, self.end, dt=10)
        hist["switch.heater"] = state_history([(heater.start, heater.end)], "on", "off", DAY0 - 3 * HOUR)
        cfg = AnalysisConfig(grid_entity=GRID)
        cfg.switched_loads = [SwitchedLoadConfig("switch.heater", "Heater")]
        report = analyze(hist, cfg, self.start, self.end)
        (k,) = report["known_loads"]
        self.assertNotIn("mode", k)
        self.assertEqual(k["matched"], 2)
        self.assertAlmostEqual(k["power_w"], 2000, delta=40)
        self.assertAlmostEqual(report["energy_kwh"]["known"]["Heater"], 2.0, delta=0.05)
        self.assertFalse(report["activations"])  # its steps are explained

    def test_relay_power_left_out_of_a_run(self):
        """A relay switching during a run is not counted in the run."""
        heater = Steady(self.dryer.start + 20 * MIN, self.dryer.start + 40 * MIN, 2000)
        house = House(base_w=250, loads=[self.dryer, heater], noise_w=10, seed=6)
        hist = house.histories(self.start - 3 * HOUR, self.end, dt=10)
        hist[STATE] = self.hist[STATE]
        hist["switch.heater"] = state_history([(heater.start, heater.end)], "on", "off", DAY0 - 3 * HOUR)
        cfg = self.config()
        cfg.switched_loads.append(SwitchedLoadConfig("switch.heater", "Heater"))
        report = analyze(hist, cfg, self.start, self.end)
        truth = energy_of(self.dryer, self.dryer.start, self.dryer.end)
        self.assertAlmostEqual(report["energy_kwh"]["known"]["Dryer"], truth, delta=0.12 * truth)
        self.assertAlmostEqual(report["energy_kwh"]["known"]["Heater"], 0.667, delta=0.05)


# ---------------------------------------------------------------------------
# ground truth from an operation state
# ---------------------------------------------------------------------------
def dryer_days(n=6, seed=11):
    """``n`` days with one ramping dryer cycle each (1 h 30 to 1 h 50 at
    300-360 W), a fridge, a job state (drying then cooling) and a coarse
    counter."""
    rnd = random.Random(seed)
    days = []
    counted = 10.07  # the counter goes on from day to day
    for d in range(n):
        day0 = DAY0 + d * DAY
        a = day0 + rnd.uniform(9, 18) * HOUR
        dur = rnd.uniform(90, 110) * MIN
        dryer = Ramp(a, a + dur, rnd.uniform(300, 360))
        cooling = Steady(a + dur, a + dur + 8 * MIN, 120.0)
        load = Sum(dryer, cooling)
        fridge = Cycling(day0 - 3 * HOUR, day0 + DAY, 90.0, 900, 1500)
        house = House(base_w=250, loads=[load, fridge], noise_w=10, seed=seed + d)
        points = residual_points(house, day0 - 3 * HOUR, day0 + DAY, dt=10)
        end = a + dur + 8 * MIN
        states = state_history([(a, end)], before=day0 - 3 * HOUR)
        job = [(day0 - 3 * HOUR, "finished"), (a, "drying"), (a + dur, "cooling"), (end, "finished")]
        counter = [
            (t, float(v)) for t, v in counter_series(load, day0 - 3 * HOUR, day0 + DAY, origin=counted)
        ]
        counted += energy_of(load, day0 - 3 * HOUR, day0 + DAY)
        days.append((day0, points, states, job, counter, energy_of(load, a, end)))
    return days


class StateRunsTests(unittest.TestCase):
    def test_runs_phases_and_counter(self):
        (day0, points, states, job, counter, truth), *_ = dryer_days(1)
        runs, covered = state_runs(states, ["run"], points, day0, day0 + DAY, phases=job, counter=counter)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual((r.source, r.kind), ("state", STEADY))
        self.assertAlmostEqual(r.energy_kwh, truth, delta=0.1 * truth)
        self.assertAlmostEqual(r.baseline_w, 250 + 90 * 900 / 2400, delta=60)
        self.assertEqual([p["state"] for p in r.named_phases], ["drying", "cooling"])
        drying, cooling = r.named_phases
        self.assertAlmostEqual(drying["power_w"], 330, delta=60)
        self.assertAlmostEqual(cooling["power_w"], 120, delta=50)
        self.assertEqual(cooling["duration_s"], 8 * MIN)
        self.assertAlmostEqual(r.counter_kwh, truth, delta=0.2)
        self.assertEqual(covered, [(day0, day0 + DAY)])
        again = type(r).from_dict(r.to_dict())
        self.assertEqual((again.source, again.named_phases), ("state", r.named_phases))

    def test_pause_stays_inside_one_run(self):
        a = DAY0 + 10 * HOUR
        states = [
            (DAY0, "stop"),
            (a, "run"),
            (a + 30 * MIN, "pause"),
            (a + 35 * MIN, "run"),
            (a + HOUR, "stop"),
        ]
        runs, _ = state_runs(states, ["run"], [], DAY0, DAY0 + DAY)
        self.assertEqual([(r.start, r.end) for r in runs], [(a, a + HOUR)])
        self.assertEqual(runs[0].energy_kwh, 0.0)  # no curve: when, not how much

    def test_learn_with_a_coarse_counter(self):
        runs, truth = [], 0.0
        for day0, points, states, job, counter, t in dryer_days(8):
            got, _ = state_runs(states, ["run"], points, day0, day0 + DAY, phases=job, counter=counter)
            runs += got
            truth += t
        learned = learn(runs, 300.0)
        self.assertEqual((learned.kind, learned.band, learned.source), (STEADY, "long", "state"))
        self.assertEqual(learned.uses, 8)
        self.assertAlmostEqual(learned.counter_kwh, truth, delta=0.25)
        self.assertAlmostEqual(learned.counter_ratio, 1.0, delta=0.15)
        self.assertAlmostEqual(learned.energy_kwh, truth / 8, delta=0.15 * truth / 8)
        self.assertEqual([p["state"] for p in learned.named_phases], ["drying", "cooling"])
        d = learned.to_dict()
        self.assertEqual(d["named_phases"][0]["state"], "drying")
        self.assertIsNotNone(d["measured_kwh"])

    def test_counter_too_coarse_does_not_scale(self):
        (day0, points, states, job, counter, _), *_ = dryer_days(1)
        runs, _ = state_runs(states, ["run"], points, day0, day0 + DAY, counter=counter)
        learned = learn(runs, 300.0)
        self.assertIsNone(learned.counter_ratio)
        self.assertEqual(learned.energy_kwh, learned.measured_kwh)


def dishwasher_days(n=6, seed=21, programs=("eco",)):
    rnd = random.Random(seed)
    days = []
    for d in range(n):
        day0 = DAY0 + d * DAY
        program = programs[d % len(programs)]
        cycle = dishwasher(day0 + rnd.uniform(12, 19) * HOUR, rnd)
        if program == "quick":
            # a short programme: one heating, no prewash
            cycle.loads, cycle.phases = cycle.loads[2:6], cycle.phases[1:3]
        load = Sum(*cycle.loads)
        a, b = cycle.phases[0][1], cycle.phases[-1][2]
        fridge = Cycling(day0 - 3 * HOUR, day0 + DAY, 90.0, 900, 1500)
        house = House(base_w=250, loads=[load, fridge], noise_w=10, seed=seed + d)
        points = residual_points(house, day0 - 3 * HOUR, day0 + DAY, dt=10)
        states = state_history([(a, b)], before=day0 - 3 * HOUR)
        phase = [(day0 - 3 * HOUR, "none")] + [(x, name) for name, x, _ in cycle.phases] + [(b, "none")]
        prog = [(day0 - 3 * HOUR, program)]
        days.append((day0, points, states, phase, prog, energy_of(load, a, b)))
    return days


class DishwasherTests(unittest.TestCase):
    def test_multiphase_bounded_by_the_state(self):
        runs = []
        for day0, points, states, phase, prog, truth in dishwasher_days(5):
            got, _ = state_runs(states, ["run"], points, day0, day0 + DAY, phases=phase, programs=prog)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, MULTIPHASE)
            self.assertAlmostEqual(got[0].energy_kwh, truth, delta=0.1 * truth)
            runs += got
        learned = learn(runs, 300.0)
        self.assertEqual((learned.kind, learned.program), (MULTIPHASE, "eco"))
        self.assertGreaterEqual(learned.uses, 4)
        self.assertTrue(learned.recognisable)
        self.assertGreater(learned.power_w, 1800)
        heat = [p for p in learned.named_phases if p["state"] == "heat"]
        self.assertTrue(heat and heat[0]["power_w"] > 1800)
        self.assertEqual([p["program"] for p in learned.programs], ["eco"])

    def test_one_template_per_programme(self):
        runs = []
        for day0, points, states, phase, prog, _ in dishwasher_days(6, programs=("eco", "quick")):
            got, _ = state_runs(states, ["run"], points, day0, day0 + DAY, phases=phase, programs=prog)
            runs += got
        learned = learn(runs, 300.0)
        self.assertEqual({p["program"] for p in learned.programs}, {"eco", "quick"})
        self.assertEqual(learned.uses, 3)
        main = learned.program
        other = next(p for p in learned.programs if p["program"] != main)
        self.assertEqual(other["kind"], MULTIPHASE)
        appliance = {"key": "appliance:dw", "name": "Dishwasher", "teacher": {"entity_id": DW_STATE}}
        registry = {"signatures": []}
        made = program_signatures(appliance, learned.to_dict(), registry, DAY0)
        self.assertEqual(len(made), 1)
        sig, created = made[0]
        self.assertTrue(created)
        self.assertEqual(
            (sig["kind"], sig["appliance"], sig["label"]), (MULTIPHASE, "appliance:dw", "Dishwasher")
        )
        self.assertEqual(registry["signatures"], [sig])
        # the same programme only: nothing more
        single = learn([r for r in runs if r.program == main], 300.0)
        self.assertEqual(program_signatures(appliance, single.to_dict(), {"signatures": []}, DAY0), [])


# ---------------------------------------------------------------------------
# flat power sensors
# ---------------------------------------------------------------------------
class FlatPowerTests(unittest.TestCase):
    def derived(self, extra_states=()):
        snap = snapshot()
        snap["states"] += [
            energy(COUNTER),
            power(FLAT),
            energy("sensor.oven_energy"),
            power("sensor.oven_power"),
        ]
        snap["states"] += list(extra_states)
        snap["prefs"]["device_consumption"] = [
            {"stat_consumption": COUNTER},
            {"stat_consumption": "sensor.oven_energy"},
        ]
        return derive(snap["prefs"], snap["states"], snap["registry"], {})

    def test_flat_power_while_counting(self):
        derived = self.derived()
        self.assertEqual([t.role for t in derived.tracked], ["submeter", "submeter"])
        hist = {
            FLAT: [(DAY0 - HOUR, "0.0")],
            COUNTER: [(DAY0 - HOUR, "10.0"), (DAY0 + 15 * HOUR, "10.3"), (DAY0 + 16 * HOUR, "10.6")],
            "sensor.oven_power": [(DAY0, "0"), (DAY0 + HOUR, "2000"), (DAY0 + 2 * HOUR, "0")],
            "sensor.oven_energy": [(DAY0, "1.0"), (DAY0 + 2 * HOUR, "3.0")],
        }
        found = flat_power_sensors(derived.tracked, hist, DAY0, DAY0 + DAY)
        self.assertEqual([f.kind for f in found], ["flat_power_sensor"])
        self.assertEqual(found[0].entity_id, COUNTER)
        self.assertEqual(found[0].params["power_entity"], FLAT)
        self.assertAlmostEqual(found[0].params["energy_kwh"], 0.6)
        day = without_flat(derived, found)
        self.assertEqual([m.entity_id for m in day.config.submeters], ["sensor.oven_power"])
        self.assertEqual([t.role for t in day.tracked], ["energy_only", "submeter"])
        self.assertEqual(day.findings[-1].kind, "flat_power_sensor")
        # the shared configuration is left untouched
        self.assertEqual(len(derived.config.submeters), 2)
        self.assertEqual(derived.tracked[0].role, "submeter")

    def test_not_flat(self):
        derived = self.derived()
        # a constant standby that explains the counter, or a counter that did not move
        hist = {FLAT: [(DAY0 - HOUR, "5")], COUNTER: [(DAY0 - HOUR, "10.0"), (DAY0 + 20 * HOUR, "10.12")]}
        self.assertEqual(flat_power_sensors(derived.tracked, hist, DAY0, DAY0 + DAY), [])
        hist = {FLAT: [(DAY0 - HOUR, "0")], COUNTER: [(DAY0 - HOUR, "10.0")]}
        self.assertEqual(flat_power_sensors(derived.tracked, hist, DAY0, DAY0 + DAY), [])
        hist = {
            FLAT: [(DAY0 - HOUR, "0"), (DAY0 + HOUR, "300")],
            COUNTER: [(DAY0, "1"), (DAY0 + 2 * HOUR, "2")],
        }
        self.assertEqual(flat_power_sensors(derived.tracked, hist, DAY0, DAY0 + DAY), [])


# ---------------------------------------------------------------------------
# the connected appliance in the add-on
# ---------------------------------------------------------------------------
DRYER = {
    "key": "appliance:dryer",
    "name": "Dryer",
    "sources": [{"type": "switched_load", "entity_id": STATE, "on_states": ["run"]}],
    "teacher": {
        "entity_id": STATE,
        "since": DAY0,
        "mode": "state",
        "on_states": ["run"],
        "phase_entity": JOB,
        "energy_entity": COUNTER,
        "program_entity": "select.dryer_program",
    },
}


class DeclarationTests(unittest.TestCase):
    def test_validate(self):
        (got,) = validate({"appliances": [DRYER]})
        self.assertEqual(got["teacher"]["mode"], "state")
        self.assertEqual(got["teacher"]["on_states"], ["run"])
        self.assertEqual(teacher_entities([got]), [STATE, JOB, "select.dryer_program", COUNTER])
        # a connected appliance's state is read, never "not subtracted"
        self.assertNotIn("teachers", analysis_options({}, [got]))

    def test_rejects(self):
        base = DRYER["teacher"]
        for teacher in (
            {**base, "mode": "x"},
            {**base, "on_states": []},
            {k: v for k, v in base.items() if k != "on_states"},
            {**base, "energy_entity": "switch.x"},
            {"entity_id": "sensor.plug_power", "phase_entity": JOB},
        ):
            with self.assertRaises(AppliancesError, msg=teacher):
                validate({"appliances": [{**DRYER, "teacher": teacher}]})


class ConnectedHA(FakeHA):
    """The fake house with a ramping dryer, its operation and job states, a
    coarse counter, a power sensor stuck at 0 W and a programme state that
    the recorder does not keep."""

    def __init__(self, data_start):
        super().__init__(data_start)
        self.dryers = []
        for d in range(1, 4):
            a = midnight(TODAY - dt.timedelta(days=d)) + 15 * HOUR
            self.dryers.append(Ramp(a, a + 100 * MIN, 330.0))
        self.house.loads = [*self.house.loads, *self.dryers]

    def snapshot(self):
        snap = super().snapshot()
        snap["states"] += [energy(COUNTER), power(FLAT)]
        snap["registry"] += [
            {"entity_id": COUNTER, "device_id": "dryer", "platform": "test"},
            {"entity_id": FLAT, "device_id": "dryer", "platform": "test"},
        ]
        snap["prefs"]["device_consumption"] = [{"stat_consumption": COUNTER}]
        return snap

    def history(self, entities, start, end):
        out = super().history(entities, start, end)
        if not out:
            return out
        periods = [(x.start, x.end) for x in self.dryers]
        if STATE in entities:
            out[STATE] = state_history(periods, before=start)
        if JOB in entities:
            out[JOB] = [(start, "finished")] + [
                (x, s) for a, b in periods for x, s in ((a, "drying"), (b, "finished"))
            ]
        if FLAT in entities:
            out[FLAT] = [(start, "0.0")]
        if COUNTER in entities:
            out[COUNTER] = counter_series(Sum(*self.dryers), start, end, step_kwh=0.1, every_s=900)
        out.pop("select.dryer_program", None)  # excluded from the recorder
        return out


class AnalyzerConnectedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ha = ConnectedHA(data_start=midnight(TODAY) - 4 * 86400)
        self.analyzer, self.store = make_analyzer(self._tmp.name, self.ha)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, method, target, body=b""):
        return route(self.analyzer, self.store, method, target, body)

    def test_ground_truth_status_and_flat_power(self):
        self.analyzer.set_appliances(validate({"appliances": [DRYER]}))
        for d in (3, 2, 1):
            report = self.analyzer.analyze_day(TODAY - dt.timedelta(days=d))
        entities = self.ha.history_calls[0][0]
        for e in (STATE, JOB, COUNTER, "select.dryer_program"):
            self.assertIn(e, entities)
        # the switched load measures the ramp over the run
        (k,) = report["known_loads"]
        self.assertEqual((k["mode"], k["runs"]), (RUN, 1))
        self.assertAlmostEqual(report["energy_kwh"]["known"]["Dryer"], 0.55, delta=0.06)
        # the stuck power sensor is not a sub-meter
        kinds = {f["kind"]: f for f in report["findings"]}
        self.assertEqual(kinds["flat_power_sensor"]["params"]["power_entity"], FLAT)
        self.assertEqual([t["role"] for t in report["config"]["tracked"]], ["energy_only"])
        (truth,) = report["ground_truth"]
        self.assertEqual(
            (truth["mode"], truth["entity_id"], truth["program_recorded"]), ("state", STATE, False)
        )
        (run,) = truth["runs"]
        self.assertAlmostEqual(run["energy_kwh"], 0.55, delta=0.06)
        self.assertEqual(run["named_phases"][0]["state"], "drying")
        status_code, body = self.call("GET", "/api/teaching")
        self.assertEqual(status_code, 200)
        (st,) = body["teachers"]
        self.assertEqual((st["mode"], st["days"], st["uses"]), ("state", 3, 3))
        self.assertIs(st["program_recorded"], False)
        self.assertEqual((st["learned"]["kind"], st["learned"]["source"]), (STEADY, "state"))
        self.assertAlmostEqual(st["learned"]["counter_kwh"], 1.65, delta=0.25)
        self.assertTrue(st["learned"]["recognisable"])  # 330 W, above the 300 W threshold
        status_code, body = self.call(
            "POST", "/api/teaching", json.dumps({"appliance": DRYER["key"]}).encode()
        )
        self.assertEqual(status_code, 200, body)
        self.assertEqual(body["programs"], [])

    def test_residual_curve_keeps_its_own_load(self):
        report = {
            "curve": [{"t": 0, "house": 1000, "submetered": 100, "known": {"Dryer": 300, "Heater": 400}}],
            "known_loads": [
                {"name": "Dryer", "entity_id": STATE},
                {"name": "Heater", "entity_id": "switch.h"},
            ],
        }
        self.assertEqual(residual_curve(report, STATE), [(0.0, 500.0)])
        self.assertEqual(residual_curve(report), [(0.0, 200.0)])

    def test_status_without_days(self):
        st = status(validate({"appliances": [DRYER]})[0], {}, None, DAY0)
        self.assertEqual((st["mode"], st["on_states"], st["learned"]), ("state", ["run"], None))
        self.assertIsNone(st["program_recorded"])

    def test_day_state_truth_energy_unit(self):
        report = {"curve": [], "known_loads": []}
        hist = {STATE: state_history([(DAY0 + HOUR, DAY0 + 2 * HOUR)], before=DAY0 - HOUR)}
        hist[COUNTER] = [(DAY0, "1000"), (DAY0 + 2.2 * HOUR, "1500")]
        teacher = validate({"appliances": [DRYER]})[0]["teacher"]
        gt = day_state_truth("k", teacher, hist, report, DAY0, DAY0 + DAY, energy_scale=0.001)
        self.assertAlmostEqual(gt["runs"][0]["counter_kwh"], 0.5)


if __name__ == "__main__":
    unittest.main()
