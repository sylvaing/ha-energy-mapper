"""Tests of the hourly breakdown (nilm_core.hourly) and of GET /api/hourly:
UTC hours, DST days, half-hour time zones, switched loads by entity id and
signatures by registry id. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import tempfile
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import route  # noqa: E402
from nilm_core import AnalysisConfig, SwitchedLoadConfig, analyze  # noqa: E402
from nilm_core.hourly import UNMATCHED, curve_resolution, hourly_breakdown  # noqa: E402
from nilm_core.registry import update  # noqa: E402
from service import Analyzer, day_bounds  # noqa: E402
from storage import ReportStore  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady, switch_history  # noqa: E402

UTC = dt.UTC
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
KOLKATA = zoneinfo.ZoneInfo("Asia/Kolkata")
UTC_HOUR = lambda ts: dt.datetime.fromtimestamp(ts, UTC).hour  # noqa: E731


def flat_report(start: float, end: float, known=None, signatures=None, activations=(), house=1000):
    """A report whose every minute has the same breakdown (W)."""
    curve = []
    t = start
    while t < end:
        curve.append(
            {
                "t": t,
                "house": house,
                "base": 100,
                "other": 50,
                "known": dict(known or {}),
                "signatures": dict(signatures or {}),
            }
        )
        t += 60
    return {
        "day": "2027-03-28",
        "start": start,
        "end": end,
        "generated_at": 123.0,
        "curve": curve,
        "energy_kwh": {"base_w": 100},
        "known_loads": [{"name": "Water heater", "entity_id": "switch.water_heater", "power_w": 2000}],
        "activations": list(activations),
    }


def registry_with(*signatures):
    return {"version": 1, "power_tol": 0.12, "signatures": list(signatures), "switched_loads": []}


def sig(sid, power, kind="steady", band="medium"):
    return {"id": sid, "kind": kind, "band": band, "power_w": power}


class HourlyBreakdownTests(unittest.TestCase):
    def test_utc_day_hours_and_totals(self):
        start = DAY0
        report = flat_report(start, start + 24 * HOUR, known={"Water heater": 600})
        out = hourly_breakdown(report, None)
        self.assertEqual(len(out["hours"]), 24)
        self.assertEqual([h["start"] for h in out["hours"]], [int(start + i * HOUR) for i in range(24)])
        first = out["hours"][0]
        self.assertEqual(first["known"], {"switch.water_heater": 0.6})  # by entity id
        self.assertAlmostEqual(first["house"], 1.0)
        self.assertAlmostEqual(first["base"], 0.1)
        self.assertAlmostEqual(first["other"], 0.05)
        self.assertEqual(out["energy_kwh"]["known"], {"switch.water_heater": 14.4})
        self.assertEqual(out["energy_kwh"]["base_w"], 100)
        self.assertEqual(out["names"], {"switch.water_heater": "Water heater"})
        self.assertEqual(out["uses"]["known"], {"switch.water_heater": 1})
        self.assertEqual(out["last_active"]["known"]["switch.water_heater"], start + 24 * HOUR)
        self.assertEqual(out["resolution_s"], 60)

    def test_dst_days_have_23_and_25_hours(self):
        for day, n in ((dt.date(2027, 3, 28), 23), (dt.date(2027, 10, 31), 25), (dt.date(2027, 6, 1), 24)):
            start, end = day_bounds(day, PARIS)
            out = hourly_breakdown(flat_report(start, end, known={"Water heater": 1000}), None)
            self.assertEqual(len(out["hours"]), n, day)
            self.assertTrue(all(h["start"] % 3600 == 0 for h in out["hours"]))
            self.assertEqual(out["hours"][0]["start"], start)  # midnight local = a UTC hour
            self.assertAlmostEqual(out["energy_kwh"]["known"]["switch.water_heater"], float(n), places=3)

    def test_half_hour_time_zone_shares_the_boundary_hours(self):
        day = dt.date(2027, 6, 1)
        start, end = day_bounds(day, KOLKATA)
        self.assertEqual(start % 3600, 1800)
        out = hourly_breakdown(flat_report(start, end, known={"Water heater": 1000}), None)
        hours = out["hours"]
        self.assertEqual(len(hours), 25)
        self.assertEqual(hours[0]["start"], start - 1800)
        # only the day's own half of the first and last hours
        self.assertAlmostEqual(hours[0]["known"]["switch.water_heater"], 0.5)
        self.assertAlmostEqual(hours[-1]["known"]["switch.water_heater"], 0.5)
        self.assertAlmostEqual(hours[1]["known"]["switch.water_heater"], 1.0)
        self.assertAlmostEqual(out["energy_kwh"]["known"]["switch.water_heater"], 24.0)

    def test_signatures_go_to_their_registry_id(self):
        start = DAY0
        # two activations of the same daily cluster that the registry tells apart
        acts = [
            {"start": start, "end": start + 1800, "power_w": 2000, "signature": "short-2000w"},
            {"start": start, "end": start + 1800, "power_w": 2300, "signature": "short-2000w"},
            # unknown to the registry
            {"start": start + 7200, "end": start + 7800, "power_w": 800, "signature": "short-800w"},
        ]
        report = flat_report(start, start + 3 * HOUR, activations=acts)
        for row in report["curve"]:
            row["signatures"] = {}
            if row["t"] < start + 1800:
                row["signatures"]["short-2000w"] = 4300
            if start + 7200 <= row["t"] < start + 7800:
                row["signatures"]["short-800w"] = 800
        registry = registry_with(sig("kettle", 2000), sig("oven", 2300))
        out = hourly_breakdown(report, registry)
        h0 = out["hours"][0]["signatures"]
        self.assertAlmostEqual(h0["kettle"], 1.0, places=3)
        self.assertAlmostEqual(h0["oven"], 1.15, places=3)
        self.assertAlmostEqual(out["hours"][2]["signatures"][UNMATCHED], 0.8 * 600 / 3600, places=3)
        self.assertEqual(out["uses"]["signatures"], {"kettle": 1, "oven": 1, UNMATCHED: 1})
        self.assertEqual(out["last_active"]["signatures"]["kettle"], start + 1800)
        self.assertEqual(out["hours"][1]["signatures"], {})

    def test_cycling_duty_weights_the_split(self):
        start = DAY0
        acts = [
            {
                "start": start,
                "end": start + 600,
                "power_w": 1400,
                "cycling": True,
                "duty": 0.5,
                "signature": "x",
            },
            {"start": start, "end": start + 600, "power_w": 700, "signature": "x"},
        ]
        report = flat_report(start, start + HOUR, activations=acts)
        for row in report["curve"][:10]:
            row["signatures"] = {"x": 1400}
        registry = registry_with(sig("hob", 1400, "cycling", "session"), sig("heater", 700))
        h0 = hourly_breakdown(report, registry)["hours"][0]["signatures"]
        self.assertAlmostEqual(h0["hob"], h0["heater"], places=4)  # 700 W each on average

    def test_resolution(self):
        self.assertEqual(curve_resolution([]), 60)
        self.assertEqual(curve_resolution([{"t": 0}, {"t": 300}, {"t": 330}]), 30)

    def test_matches_the_report_totals(self):
        heater_on = [(DAY0 + 2 * HOUR, DAY0 + 4.5 * HOUR)]
        loads = [
            Steady(*heater_on[0], 1850),
            Steady(DAY0 + 7 * HOUR, DAY0 + 7 * HOUR + 180, 2000),
            Steady(DAY0 + 16 * HOUR, DAY0 + 16 * HOUR + 200, 2000),
            Cycling(DAY0 + 19 * HOUR, DAY0 + 19.5 * HOUR, 1400, 45, 25),
        ]
        house = House(base_w=250, loads=loads)
        hist = house.histories(DAY0 - 3 * HOUR, DAY0 + 24 * HOUR, dt=5)
        hist["switch.heater"] = switch_history(heater_on)
        cfg = AnalysisConfig(
            grid_entity="sensor.grid_power",
            switched_loads=[SwitchedLoadConfig("switch.heater", "Water heater")],
        )
        report = analyze(hist, cfg, DAY0, DAY0 + 24 * HOUR, local_hour=UTC_HOUR)
        report["day"] = "2027-01-15"
        registry = update(None, {"2027-01-15": report}, DAY0 + 24 * HOUR, local_hour=UTC_HOUR)
        out = hourly_breakdown(report, registry)
        totals = report["energy_kwh"]
        self.assertAlmostEqual(
            out["energy_kwh"]["known"]["switch.heater"], totals["known"]["Water heater"], delta=0.005
        )
        self.assertAlmostEqual(out["energy_kwh"]["house"], totals["house"], delta=0.01)
        self.assertAlmostEqual(out["energy_kwh"]["other"], totals["other"], delta=0.01)
        self.assertAlmostEqual(
            sum(out["energy_kwh"]["signatures"].values()), sum(totals["signatures"].values()), delta=0.01
        )
        self.assertNotIn(UNMATCHED, out["energy_kwh"]["signatures"])
        self.assertEqual(out["uses"]["known"], {"switch.heater": 1})


class HourlyApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReportStore(self._tmp.name)
        self.analyzer = Analyzer(None, self.store, {}, clock=lambda: DAY0 + 10 * 86400)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, target):
        return route(self.analyzer, self.store, "GET", target)

    def test_hourly_and_days(self):
        start, end = day_bounds(dt.date(2027, 3, 28), PARIS)
        report = flat_report(start, end, known={"Water heater": 500})
        self.store.save("2027-03-28", report)
        status, body = self.call("/api/days")
        self.assertEqual(status, 200)
        self.assertEqual(body["generated"], {"2027-03-28": 123.0})
        status, body = self.call("/api/hourly/2027-03-28")
        self.assertEqual(status, 200)
        self.assertEqual((body["day"], body["generated_at"], len(body["hours"])), ("2027-03-28", 123.0, 23))

    def test_errors(self):
        self.assertEqual(self.call("/api/hourly/2027-13-01")[0], 400)
        self.assertEqual(
            self.call("/api/hourly/2027-03-01"),
            (404, {"error": "not_found", "message": "no report for 2027-03-01"}),
        )
        # a report whose curve was pruned: summary only
        report = flat_report(DAY0, DAY0 + 24 * HOUR)
        self.store.save("2027-03-02", report)
        self.store._report_path("2027-03-02").unlink()
        status, body = self.call("/api/hourly/2027-03-02")
        self.assertEqual((status, body["message"]), (404, "no per-minute curve for 2027-03-02"))
        self.assertEqual(route(self.analyzer, self.store, "POST", "/api/hourly/2027-03-02")[0], 405)


if __name__ == "__main__":
    unittest.main()
