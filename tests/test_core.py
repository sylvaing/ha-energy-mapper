"""Unit tests of nilm_core on synthetic households (python3 -m unittest)."""

from __future__ import annotations

import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from nilm_core import (  # noqa: E402
    AnalysisConfig,
    SwitchedLoadConfig,
    analyze,
    build_samples,
    detect_steps,
    find_cycling_sessions,
    pair_steps,
    parse_numeric,
)
from nilm_core.activations import validate_activations  # noqa: E402
from nilm_core.signal import scaled  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady, switch_history  # noqa: E402

UTC_HOUR = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731


def residual_points(hist, grid="sensor.grid_power", pv=None):
    samples = build_samples(parse_numeric(hist[grid]), parse_numeric(hist[pv]) if pv else None)
    return [(s.ts, s.residual) for s in samples if s.residual is not None]


# ---------------------------------------------------------------------------
# #1 signal
# ---------------------------------------------------------------------------
class SignalTests(unittest.TestCase):
    def test_no_pv_house_equals_grid(self):
        h = House(base_w=300)
        hist = h.histories(DAY0, DAY0 + 600)
        samples = build_samples(parse_numeric(hist["sensor.grid_power"]))
        self.assertTrue(all(abs(s.house - s.grid) < 1e-9 for s in samples))
        self.assertAlmostEqual(sum(s.house for s in samples) / len(samples), 300, delta=10)

    def test_pv_is_added_back(self):
        h = House(base_w=400, pv_peak_w=3000)
        hist = h.histories(DAY0 + 12 * HOUR, DAY0 + 12 * HOUR + 600)
        samples = build_samples(
            parse_numeric(hist["sensor.grid_power"]), parse_numeric(hist["sensor.pv_power"])
        )
        mean = sum(s.house for s in samples) / len(samples)
        self.assertAlmostEqual(mean, 400, delta=15)
        self.assertTrue(any(s.grid < 0 for s in samples))  # exporting

    def test_short_pv_dropout_does_not_create_steps(self):
        h = House(base_w=400, pv_peak_w=3000)
        t = DAY0 + 12 * HOUR
        hist = h.histories(t, t + 1800, pv_dropouts=((t + 600, t + 720),))
        pts = residual_points(hist, pv="sensor.pv_power")
        self.assertEqual(detect_steps(pts), [])

    def test_long_daytime_pv_dropout_is_unknown(self):
        h = House(base_w=400, pv_peak_w=3000)
        t = DAY0 + 12 * HOUR
        hist = h.histories(t, t + 3600, pv_dropouts=((t + 600, t + 3000),))
        samples = build_samples(
            parse_numeric(hist["sensor.grid_power"]), parse_numeric(hist["sensor.pv_power"])
        )
        unknown = [s for s in samples if s.house is None]
        self.assertTrue(unknown)
        self.assertTrue(all(t + 600 + 300 <= s.ts < t + 3000 for s in unknown))

    def test_pv_unavailable_at_night_is_zero(self):
        h = House(base_w=250, pv_peak_w=3000)
        t = DAY0 + 2 * HOUR
        hist = h.histories(t, t + 3600, pv_dropouts=((t, t + 3600),))
        samples = build_samples(
            parse_numeric(hist["sensor.grid_power"]), parse_numeric(hist["sensor.pv_power"])
        )
        self.assertTrue(all(s.pv == 0.0 for s in samples))

    def test_kw_and_inverted_sign(self):
        h = House(base_w=500)
        hist = h.histories(DAY0, DAY0 + 300, kw=True)
        pts = scaled(parse_numeric(hist["sensor.grid_power"]), 1000.0, invert=True)
        samples = build_samples(pts)
        self.assertAlmostEqual(sum(s.grid for s in samples) / len(samples), -500, delta=15)

    def test_import_export_sensors(self):
        h = House(base_w=400, pv_peak_w=3000)
        t = DAY0 + 12 * HOUR
        hist = h.histories(t, t + 600, split_import_export=True)
        samples = build_samples(
            parse_numeric(hist["sensor.grid_power"]),
            parse_numeric(hist["sensor.pv_power"]),
            grid_export=parse_numeric(hist["sensor.grid_power_export"]),
        )
        self.assertAlmostEqual(sum(s.house for s in samples) / len(samples), 400, delta=15)

    def test_battery_discharge_is_consumption(self):
        grid = [(DAY0 + i * 10, 100.0) for i in range(30)]
        battery = [(DAY0, 900.0)]  # discharging 900 W into the house
        samples = build_samples(grid, battery=battery)
        self.assertTrue(all(abs(s.house - 1000) < 1e-9 for s in samples))

    def test_unavailable_submeter_keeps_last_value_then_zero(self):
        grid = [(DAY0 + i * 10, 1500.0) for i in range(100)]
        pump = [(DAY0, 900.0), (DAY0 + 100, None)]
        samples = build_samples(grid, submeters=[pump])
        by_t = {s.ts - DAY0: s for s in samples}
        self.assertEqual(by_t[200].submetered, 900.0)  # held
        self.assertEqual(by_t[600].submetered, 0.0)  # given up after 5 min


# ---------------------------------------------------------------------------
# #2 steps, cycling sessions, pairing
# ---------------------------------------------------------------------------
class StepTests(unittest.TestCase):
    def test_single_spike_is_ignored(self):
        pts = [(DAY0 + i * 10, 300.0) for i in range(60)]
        pts[30] = (pts[30][0], 2500.0)  # one reading, motor inrush
        self.assertEqual(detect_steps(pts), [])

    def test_kettle_detected_at_various_rates(self):
        for dt in (1, 10, 60):
            with self.subTest(dt=dt):
                h = House(loads=[Steady(DAY0 + 600, DAY0 + 600 + 240, 2000)])
                pts = residual_points(h.histories(DAY0, DAY0 + 1800, dt=dt))
                steps = detect_steps(pts)
                self.assertEqual(len(steps), 2, steps)
                self.assertAlmostEqual(steps[0].delta, 2000, delta=100)
                self.assertAlmostEqual(steps[1].delta, -2000, delta=100)

    def test_long_load_not_closed_by_similar_short_load(self):
        # a water heater (1850 W, 3 h) while a kettle (1950 W) runs twice
        loads = [
            Steady(DAY0 + 1 * HOUR, DAY0 + 4 * HOUR, 1850),
            Steady(DAY0 + 1.5 * HOUR, DAY0 + 1.5 * HOUR + 180, 1950),
            Steady(DAY0 + 2.5 * HOUR, DAY0 + 2.5 * HOUR + 200, 1950),
        ]
        pts = residual_points(House(loads=loads).histories(DAY0, DAY0 + 5 * HOUR))
        acts = pair_steps(detect_steps(pts))
        long_ = [a for a in acts if a.duration_s > HOUR]
        self.assertEqual(len(long_), 1)
        self.assertAlmostEqual(long_[0].duration_s, 3 * HOUR, delta=60)
        short = [a for a in acts if a.duration_s < 600]
        self.assertEqual(len(short), 2)

    def test_phantom_pairing_is_rejected(self):
        # +400 W at 10:00 then -400 W at 12:00, but the load came back to the
        # base in between (two unrelated events, e.g. clouds): no 2 h appliance
        pts = []
        for i in range(0, 3 * 3600, 10):
            t = DAY0 + 9.5 * HOUR + i
            v = 300.0
            if DAY0 + 10 * HOUR <= t < DAY0 + 10 * HOUR + 120:
                v = 700.0
            if DAY0 + 12 * HOUR - 120 <= t < DAY0 + 12 * HOUR:
                v = 700.0
            pts.append((t, v))
        steps = detect_steps(pts)
        steps = [s for s in steps if s.ts in (DAY0 + 10 * HOUR, DAY0 + 12 * HOUR)]  # keep the two far ends
        pairs = pair_steps(steps)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(validate_activations(pairs, pts, base_w=300), [])

    def test_real_long_load_is_kept(self):
        h = House(loads=[Steady(DAY0 + HOUR, DAY0 + 3 * HOUR, 1850)])
        pts = residual_points(h.histories(DAY0, DAY0 + 4 * HOUR))
        acts = pair_steps(detect_steps(pts))
        self.assertEqual(len(validate_activations(acts, pts, base_w=250)), 1)

    def test_cycling_session(self):
        load = Cycling(DAY0 + 600, DAY0 + 600 + 15 * 60, 1400, on_s=45, off_s=25)
        pts = residual_points(House(loads=[load]).histories(DAY0, DAY0 + 3600, dt=3))
        steps = detect_steps(pts)
        sessions = find_cycling_sessions(steps)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertAlmostEqual(s.power_w, 1400, delta=100)
        self.assertAlmostEqual(s.duty, 45 / 70, delta=0.1)
        self.assertAlmostEqual(s.period_s, 70, delta=6)
        # nothing left to pair: the session consumed its steps
        self.assertEqual(pair_steps(steps), [])


# ---------------------------------------------------------------------------
# #3 switched loads, signatures, report
# ---------------------------------------------------------------------------
class ReportTests(unittest.TestCase):
    def day(self):
        heater_on = [(DAY0 + 12 * HOUR, DAY0 + 14.5 * HOUR), (DAY0 + 17 * HOUR, DAY0 + 17.5 * HOUR)]
        loads = [
            Steady(*heater_on[0], 1850),  # second period: tank already hot, no draw
            Steady(DAY0 + 7 * HOUR, DAY0 + 7 * HOUR + 180, 2000),  # kettle
            Steady(DAY0 + 16 * HOUR, DAY0 + 16 * HOUR + 200, 2000),  # kettle
            Steady(DAY0 + 20 * HOUR, DAY0 + 20 * HOUR + 150, 2000),  # kettle
            Cycling(DAY0 + 19 * HOUR, DAY0 + 19.5 * HOUR, 1400, 45, 25),  # hob
            Steady(DAY0 + 19.1 * HOUR, DAY0 + 19.1 * HOUR + 300, 2800),  # oven boost
        ]
        house = House(base_w=250, loads=loads, pv_peak_w=2500)
        hist = house.histories(DAY0 - 3 * HOUR, DAY0 + 24 * HOUR, dt=5)
        hist["switch.heater"] = switch_history(heater_on)
        cfg = AnalysisConfig(
            grid_entity="sensor.grid_power",
            pv_entity="sensor.pv_power",
            switched_loads=[SwitchedLoadConfig("switch.heater", "Water heater")],
        )
        return analyze(hist, cfg, DAY0, DAY0 + 24 * HOUR, local_hour=UTC_HOUR)

    def test_switched_load_power_and_periods(self):
        r = self.day()
        k = r["known_loads"][0]
        self.assertAlmostEqual(k["power_w"], 1850, delta=60)
        # only the period that really drew power counts (2.5 h, not 3 h)
        self.assertAlmostEqual(k["on_s"], 2.5 * HOUR, delta=120)
        self.assertAlmostEqual(r["energy_kwh"]["known"]["Water heater"], 1.85 * 2.5, delta=0.3)

    def test_signatures(self):
        r = self.day()
        sigs = {s["id"]: s for s in r["signatures"]}
        kettle = [s for s in sigs.values() if s["kind"] == "steady" and 1800 <= s["power_w"] <= 2200]
        self.assertEqual(len(kettle), 1, sigs)
        self.assertEqual(kettle[0]["count"], 3)
        self.assertEqual(sorted(h for h, n in enumerate(kettle[0]["hours"]) for _ in range(n)), [7, 16, 20])
        hob = [s for s in sigs.values() if s["kind"] == "cycling"]
        self.assertEqual(len(hob), 1)
        self.assertAlmostEqual(hob[0]["power_w"], 1400, delta=100)

    def test_energy_balance(self):
        r = self.day()
        e = r["energy_kwh"]
        parts = (
            e["base"]
            + e["other"]
            + e["submetered"]
            + sum(e["known"].values())
            + sum(e["signatures"].values())
        )
        self.assertAlmostEqual(parts, e["house"], delta=0.05 * e["house"])
        self.assertAlmostEqual(e["base_w"], 250, delta=40)

    def test_peak_explained(self):
        r = self.day()
        top = r["peaks"][0]
        # the evening: hob cycling + oven boost
        self.assertTrue(19 * HOUR <= top["max_ts"] - DAY0 <= 19.5 * HOUR)
        names = {p["name"] for p in top["parts"]}
        self.assertTrue(any(n.startswith("cyc-") for n in names), top)
        self.assertLess(abs(top["unexplained_w"]), 600)

    def test_one_day_is_fast(self):
        t = time.perf_counter()
        self.day()
        self.assertLess(time.perf_counter() - t, 10)


if __name__ == "__main__":
    unittest.main()
