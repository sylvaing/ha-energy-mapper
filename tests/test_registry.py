"""Tests of the signature registry (nilm_core.registry) and of its use by
the add-on: multi-day learning, stable ids, switched-load memory, the peak
window, pruning and the API. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
import random
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import route  # noqa: E402
from nilm_core.registry import (  # noqa: E402
    RegistryConfig,
    annotate_report,
    decay,
    forget_after_days,
    match,
    peak_reference,
    update,
)
from service import Analyzer  # noqa: E402
from storage import ReportStore  # noqa: E402
from synth import House, Steady  # noqa: E402

UTC = dt.UTC
DAY = 86400.0
D0 = dt.date(2027, 1, 1)
UTC_HOUR = lambda ts: dt.datetime.fromtimestamp(ts, UTC).hour  # noqa: E731


def midnight(day: dt.date) -> float:
    return dt.datetime.combine(day, dt.time(0), tzinfo=UTC).timestamp()


def act(day: dt.date, hour: float, power: float, duration_s: float, cycling=False, duty=1.0):
    start = midnight(day) + hour * 3600
    a = {"start": start, "end": start + duration_s, "power_w": power, "cycling": cycling, "duty": duty}
    if cycling:
        a.update(on_s=40.0, period_s=round(40 / duty, 1))
    return a


def summary(day: dt.date, activations=(), known_loads=(), max_house_w=3000.0, curve_rows=0):
    out = {
        "version": 1,
        "day": day.isoformat(),
        "start": midnight(day),
        "end": midnight(day + dt.timedelta(days=1)),
        "time_zone": "UTC",
        "energy_kwh": {"house": 10.0},
        "max_house_w": max_house_w,
        "peaks": [],
        "signatures": [],
        "known_loads": list(known_loads),
        "activations": list(activations),
    }
    if curve_rows:
        rnd = random.Random(day.toordinal())
        out["curve"] = [
            {
                "t": midnight(day) + 60 * i,
                "house": rnd.randint(200, 3000),
                "grid": rnd.randint(-500, 3000),
                "pv": rnd.randint(0, 2000),
                "submetered": 0,
                "known": {},
                "signatures": {"short-2000w": rnd.randint(0, 2000)} if i % 7 == 0 else {},
                "base": 250,
                "other": rnd.randint(0, 500),
            }
            for i in range(curve_rows)
        ]
    return out


def days(start: dt.date, n: int):
    return [start + dt.timedelta(days=i) for i in range(n)]


def by_id(registry, sid):
    return next((s for s in registry["signatures"] if s["id"] == sid), None)


def sig_near(registry, power, kind="steady"):
    """The signature of that kind whose power is the closest to ``power``."""
    cands = [s for s in registry["signatures"] if s["kind"] == kind]
    best = min(cands, key=lambda s: abs(s["power_w"] - power))
    return best if abs(best["power_w"] - power) <= 0.12 * power else None


def nightly(sums: dict, day: dt.date, previous, config=None):
    """The registry update of the night after ``day``."""
    return update(previous, sums, midnight(day + dt.timedelta(days=1)), config, UTC_HOUR)


class DecayTests(unittest.TestCase):
    def test_half_life(self):
        self.assertEqual(decay(0, 30), 1.0)
        self.assertAlmostEqual(decay(30 * DAY, 30), 0.5)
        self.assertAlmostEqual(decay(60 * DAY, 30), 0.25)
        self.assertEqual(decay(-DAY, 30), 1.0)  # clock skew
        self.assertAlmostEqual(forget_after_days(30), 199.3, places=1)


class RegistryTests(unittest.TestCase):
    def test_daily_appliance_one_signature(self):
        # a kettle once a day for 10 days, at slightly different powers
        rnd = random.Random(3)
        sums = {
            d.isoformat(): summary(d, [act(d, 7 + rnd.random(), 2000 + rnd.uniform(-60, 60), 180)])
            for d in days(D0, 10)
        }
        reg = nightly(sums, D0 + dt.timedelta(days=9), None)
        self.assertEqual(len(reg["signatures"]), 1)
        s = reg["signatures"][0]
        self.assertEqual((s["count"], s["days_seen"]), (10, 10))
        self.assertEqual((s["kind"], s["band"]), ("steady", "short"))
        self.assertAlmostEqual(s["power_w"], 2000, delta=60)
        self.assertEqual(s["id"], "short-2000w")
        self.assertEqual(s["hours"].index(max(s["hours"])), 7)
        self.assertEqual((s["first_seen"], s["last_seen"]), ("2027-01-01", "2027-01-10"))
        self.assertAlmostEqual(s["per_week"], 7.0, delta=0.1)
        self.assertLess(s["weight"], 10)  # older uses count less
        self.assertGreater(s["weight"], 8.5)
        self.assertEqual(len(s["examples"]), 3)
        self.assertEqual(s["examples"][0]["day"], "2027-01-10")  # the most recent first
        self.assertAlmostEqual(s["energy_kwh"], 10 * 2 * 180 / 3600, delta=0.02)
        self.assertIsNone(s["label"])
        self.assertEqual(reg["days"], 10)

    def test_cycling_session_statistics(self):
        sums = {
            d.isoformat(): summary(
                d, [act(d, 12, 1400, 1800, cycling=True, duty=0.5), act(d, 19, 1400, 1200, True, 0.5)]
            )
            for d in days(D0, 7)
        }
        reg = nightly(sums, D0 + dt.timedelta(days=6), None)
        (s,) = reg["signatures"]
        self.assertEqual((s["id"], s["kind"], s["band"], s["count"]), ("cyc-1400w", "cycling", "session", 14))
        self.assertEqual((s["on_s"], s["period_s"], s["duty"]), (40.0, 80.0, 0.5))
        self.assertEqual(sorted(sorted(range(24), key=lambda h: -s["hours"][h])[:2]), [12, 19])

    def test_weekly_appliance_still_known_after_90_days(self):
        rnd = random.Random(5)
        sums, reg = {}, None
        weekly_id = None
        for i, d in enumerate(days(D0, 180)):
            acts = [act(d, 8, 2000 + rnd.uniform(-50, 50), 150)]  # a daily kettle
            if i < 90 and i % 7 == 3:  # a washing machine heating, weekly, for 90 days
                acts.append(act(d, 10, 2600 + rnd.uniform(-80, 80), 1500))
            sums[d.isoformat()] = summary(d, acts)
            reg = nightly(sums, d, reg)
            if i == 89:
                w = sig_near(reg, 2600)
                self.assertEqual((w["band"], w["count"]), ("medium", 13))
                self.assertAlmostEqual(w["per_week"], 1.0, delta=0.35)
                weekly_id = w["id"]
        # 90 days without a use: still known, same id, seen rarely
        w = by_id(reg, weekly_id)
        self.assertIsNotNone(w)
        self.assertEqual(w["count"], 13)
        self.assertEqual(w["last_seen"], (D0 + dt.timedelta(days=87)).isoformat())
        self.assertLess(w["per_week"], 0.3)
        self.assertGreater(w["weight"], 0.5)

    def test_replaced_appliance_fades_out(self):
        rnd = random.Random(7)
        sums, reg = {}, None
        old_id = new_id = None
        for i, d in enumerate(days(D0, 400)):
            power = 1200 if i < 60 else 2000  # the old kettle is replaced on day 60
            sums[d.isoformat()] = summary(d, [act(d, 7, power + rnd.uniform(-30, 30), 150)])
            # nightly pruning of the summaries, as the add-on does
            for k in [k for k in sums if (d - dt.date.fromisoformat(k)).days > forget_after_days(30)]:
                del sums[k]
            reg = nightly(sums, d, reg)
            if i == 59:
                old_id = sig_near(reg, 1200)["id"]
            if i == 60 + 28:
                new_id = sig_near(reg, 2000)["id"]
                # four weeks later the new one already weighs more
                self.assertGreater(by_id(reg, new_id)["weight"], by_id(reg, old_id)["weight"])
            if i == 60 + 56:
                old, new = by_id(reg, old_id), by_id(reg, new_id)
                self.assertLess(old["weight"], new["weight"] / 3)
                self.assertLess(old["per_week"], 2)
                self.assertEqual(old["last_seen"], "2027-03-01")
        # long after: forgotten, while the new one is still there
        self.assertIsNone(by_id(reg, old_id))
        self.assertIsNotNone(by_id(reg, new_id))
        self.assertNotEqual(old_id, new_id)

    def test_ids_are_stable_across_runs(self):
        rnd = random.Random(11)
        appliances = [(2000, 150, 7), (1400, 240, 12), (900, 60, 18), (3000, 3600 * 1.5, 22)]
        sums, reg, ids = {}, None, None
        for i, d in enumerate(days(D0, 60)):
            acts = [
                act(d, h + rnd.random(), p * rnd.uniform(0.95, 1.05), dur * rnd.uniform(0.9, 1.1))
                for p, dur, h in appliances
                if rnd.random() < 0.8
            ]
            sums[d.isoformat()] = summary(d, acts)
            reg = nightly(sums, d, reg)
            current = {p: sig_near(reg, p)["id"] for p, _, _ in appliances if i >= 10}
            if ids and current:
                self.assertEqual(current, ids, f"night {i}")
            ids = current or ids
        self.assertEqual(len(reg["signatures"]), 4)
        # the same night again, or from scratch with the same previous registry
        again = nightly(sums, D0 + dt.timedelta(days=59), reg)
        self.assertEqual([s["id"] for s in again["signatures"]], [s["id"] for s in reg["signatures"]])
        # a user label survives the following nights
        sid = sig_near(reg, 1400)["id"]
        next(s for s in reg["signatures"] if s["id"] == sid)["label"] = "Hob"
        d = D0 + dt.timedelta(days=60)
        sums[d.isoformat()] = summary(d, [act(d, 12, 1420, 250)])
        reg = nightly(sums, d, reg)
        self.assertEqual(by_id(reg, sid)["label"], "Hob")

    def test_named_signature_is_never_forgotten(self):
        d = D0
        sums = {d.isoformat(): summary(d, [act(d, 9, 3300, 120), act(d, 9.5, 3300, 120)])}
        reg = nightly(sums, d, None)
        sid = reg["signatures"][0]["id"]
        reg["signatures"][0]["label"] = "Welder"
        unnamed = nightly(sums, d, None)
        # two years later, the day summary is long gone
        later = d + dt.timedelta(days=730)
        reg = nightly({}, later, reg)
        unnamed = nightly({}, later, unnamed)
        self.assertEqual(by_id(reg, sid)["label"], "Welder")
        self.assertLess(by_id(reg, sid)["weight"], 0.01)
        self.assertEqual(unnamed["signatures"], [])

    def test_rare_appliance_carried_after_its_days_are_pruned(self):
        # used every day for a month, then never again
        sums = {d.isoformat(): summary(d, [act(d, 15, 2500, 2400)]) for d in days(D0, 30)}
        reg = nightly(sums, D0 + dt.timedelta(days=29), None)
        sid = reg["signatures"][0]["id"]
        later = D0 + dt.timedelta(days=29 + 250)  # all its days pruned
        reg = nightly({}, later, reg)
        s = by_id(reg, sid)
        self.assertIsNotNone(s)
        self.assertEqual(s["count"], 30)
        self.assertAlmostEqual(s["weight"], 30 * decay(250 * DAY, 30), delta=0.05)

    def test_switched_load_seen_on_some_days(self):
        def known(day_index):
            if day_index % 3 == 2:  # no transition that day: nothing learned
                return {"entity_id": "switch.heater", "name": "Heater", "power_w": 0, "matched": 0,
                        "transitions": 0, "on_states": ["off"], "inverted": True}  # fmt: skip
            power = 930 + (day_index % 3) * 10
            inverted = day_index != 4  # once, the polarity was misread
            states = ["off"] if inverted else ["on"]
            return {"entity_id": "switch.heater", "name": "Heater", "power_w": power, "matched": 20,
                    "transitions": 30, "on_states": states, "inverted": inverted}  # fmt: skip

        sums = {d.isoformat(): summary(d, known_loads=[known(i)]) for i, d in enumerate(days(D0, 12))}
        reg = nightly(sums, D0 + dt.timedelta(days=11), None)
        (m,) = reg["switched_loads"]
        self.assertEqual((m["entity_id"], m["inverted"], m["on_states"]), ("switch.heater", True, ["off"]))
        self.assertAlmostEqual(m["power_w"], 935, delta=6)
        self.assertEqual(m["days_seen"], 8)
        self.assertEqual(m["last_seen"], "2027-01-11")  # the 12th day had no transition
        # days without transitions only: the values are kept, decaying
        quiet = {
            d.isoformat(): summary(d, known_loads=[known(2)]) for d in days(D0 + dt.timedelta(days=12), 5)
        }
        reg2 = nightly(quiet, D0 + dt.timedelta(days=16), reg)
        (m2,) = reg2["switched_loads"]
        self.assertEqual((m2["power_w"], m2["inverted"]), (m["power_w"], True))
        self.assertLess(m2["weight"], m["weight"])

    def test_peak_reference_window(self):
        maxima = {"2027-01-01": 9000.0, "2027-01-20": 5000.0, "2027-02-05": 4000.0}
        self.assertEqual(peak_reference(maxima, "2027-01-31", 30), 9000.0)
        self.assertEqual(peak_reference(maxima, "2027-02-01", 30), 5000.0)  # 9 kW is 31 days old
        self.assertEqual(peak_reference(maxima, "2027-02-05", 30), 5000.0)  # the day itself excluded
        self.assertIsNone(peak_reference(maxima, "2027-01-01", 30))
        self.assertIsNone(peak_reference({}, "2027-01-01", 30))
        # the registry publishes the threshold of the next day
        sums = {d: summary(dt.date.fromisoformat(d), max_house_w=m) for d, m in maxima.items()}
        reg = update(None, sums, midnight(dt.date(2027, 2, 6)), RegistryConfig(), UTC_HOUR)
        self.assertEqual(reg["peak"], {"window_days": 30, "relative": 0.8, "reference_w": 5000,
                                       "threshold_w": 4000, "days": 2})  # fmt: skip

    def test_annotate_report(self):
        sums = {
            d.isoformat(): summary(d, [act(d, 7, 2000, 150), act(d, 12, 1400, 900, True, 0.5)])
            for d in days(D0, 5)
        }
        reg = nightly(sums, D0 + dt.timedelta(days=4), None)
        d = D0 + dt.timedelta(days=5)
        a1, a2, a3 = act(d, 7, 2080, 140), act(d, 12, 1350, 1000, True, 0.5), act(d, 3, 5000, 100)
        for a, sig in ((a1, "short-2100w"), (a2, "cyc-1350w"), (a3, "short-5000w")):
            a["signature"] = sig
        report = {
            "activations": [a1, a2, a3],
            "signatures": [{"id": "short-2100w"}, {"id": "cyc-1350w"}, {"id": "short-5000w"}],
            "peaks": [{"parts": [{"kind": "signature", "name": "short-2100w", "since": a1["start"]},
                                 {"kind": "known", "name": "Heater"}]}],
        }  # fmt: skip
        annotate_report(report, reg)
        self.assertEqual(
            [a["registry_id"] for a in report["activations"]], ["short-2000w", "cyc-1400w", None]
        )
        self.assertEqual([s["registry_id"] for s in report["signatures"]], ["short-2000w", "cyc-1400w", None])
        self.assertEqual(report["peaks"][0]["parts"][0]["registry_id"], "short-2000w")
        self.assertNotIn("registry_id", report["peaks"][0]["parts"][1])
        self.assertEqual(report["activations"][0]["signature"], "short-2100w")  # daily id kept
        self.assertIsNone(match(None, a1))
        self.assertIsNone(match(reg, act(d, 7, 2000, 1500)))  # other duration band

    def test_rebuild_is_fast(self):
        # 200 days of a busy house: ~16 000 activations
        rnd = random.Random(13)
        powers = [350, 500, 700, 900, 1200, 1400, 1800, 2000, 2400, 3000]
        sums = {}
        for d in days(D0, 200):
            acts = []
            for _ in range(80):
                p = rnd.choice(powers) * rnd.uniform(0.93, 1.07)
                acts.append(act(d, rnd.uniform(0, 23.5), p, rnd.choice((60, 300, 1200, 4000)),
                                cycling=rnd.random() < 0.1, duty=0.5))  # fmt: skip
            sums[d.isoformat()] = summary(d, acts)
        t0 = time.perf_counter()
        reg = nightly(sums, D0 + dt.timedelta(days=199), None)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0)
        self.assertGreater(len(reg["signatures"]), 10)
        self.assertEqual(sum(s["count"] for s in reg["signatures"]), 16000)


# ---------------------------------------------------------------------------
# add-on: storage, pruning, registry maintenance, API
# ---------------------------------------------------------------------------
def store_day(store: ReportStore, day: dt.date, acts=(), max_house_w=3000.0, curve_rows=1440):
    report = summary(day, acts, max_house_w=max_house_w, curve_rows=curve_rows)
    store.save(day.isoformat(), report)


class StoreAnalyzer(Analyzer):
    """An analyzer without Home Assistant, for the maintenance only."""

    def __init__(self, store, options, today: dt.date):
        super().__init__(None, store, options, clock=lambda: midnight(today) + 5 * 3600)
        self.tz = UTC


class AddonRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_reports_are_compressed(self):
        reports = self.tmp / "reports"
        reports.mkdir()
        old = summary(D0, [act(D0, 7, 2000, 150)], curve_rows=10)
        (reports / "2027-01-01.json").write_text(json.dumps(old))
        (reports / "notes.json").write_text("{}")
        store = ReportStore(self.tmp)
        self.assertEqual(store.days(), ["2027-01-01"])
        self.assertFalse((reports / "2027-01-01.json").exists())
        self.assertTrue((reports / "notes.json").exists())
        with gzip.open(reports / "2027-01-01.json.gz") as f:
            self.assertEqual(json.load(f), old)
        self.assertEqual(store.load("2027-01-01"), old)
        self.assertNotIn("curve", store.load_summary("2027-01-01"))
        # the API still answers plain JSON
        analyzer = StoreAnalyzer(store, {}, D0 + dt.timedelta(days=1))
        status, body = route(analyzer, store, "GET", "/api/report/2027-01-01")
        self.assertEqual((status, body["day"], len(body["curve"])), (200, "2027-01-01", 10))

    def test_simulated_year_storage_is_bounded(self):
        store = ReportStore(self.tmp)
        rnd = random.Random(17)
        sizes = {}
        for i, d in enumerate(days(D0, 365)):
            acts = [act(d, rnd.uniform(6, 22), rnd.choice([500, 900, 1400, 2000]) * rnd.uniform(0.95, 1.05),
                        rnd.choice((90, 900, 4000))) for _ in range(30)]  # fmt: skip
            store_day(store, d, acts)
            analyzer = StoreAnalyzer(store, {}, d + dt.timedelta(days=1))
            analyzer.maintain()
            sizes[i] = store.size_bytes()
        today = D0 + dt.timedelta(days=365)
        self.assertEqual(len(store.days()), 90)  # today - 90 days .. yesterday
        self.assertEqual(store.days()[0], (today - dt.timedelta(days=90)).isoformat())
        n_summaries = len(store.summary_days())
        self.assertLessEqual(n_summaries, 200)
        self.assertGreaterEqual(n_summaries, 199)
        # the storage stops growing once the retention is reached
        self.assertLess(sizes[364], sizes[250] * 1.05)
        self.assertLess(sizes[364], 8 * 1024 * 1024)
        reg = store.load_registry()
        self.assertEqual(reg["days"], n_summaries)
        self.assertLess(store.registry_path.stat().st_size, 200 * 1024)

    def test_registry_rebuilt_when_missing_or_corrupt(self):
        store = ReportStore(self.tmp)
        for d in days(D0, 10):
            store_day(store, d, [act(d, 7, 2000, 150)], curve_rows=0)
        analyzer = StoreAnalyzer(store, {}, D0 + dt.timedelta(days=10))
        analyzer.maintain()
        first = store.load_registry()
        self.assertEqual([s["id"] for s in first["signatures"]], ["short-2000w"])
        store.registry_path.write_text("{broken")
        with self.assertLogs("service", "WARNING") as logs:
            analyzer.maintain()
        self.assertTrue(any("rebuilding" in m for m in logs.output))
        self.assertEqual(store.load_registry()["signatures"][0]["count"], 10)
        store.registry_path.unlink()
        # the next pass (nothing to analyse) rebuilds it without calling HA
        store.set_state(last_maintenance=(D0 + dt.timedelta(days=10)).isoformat())
        analyzer.options = {"backfill_days": 3}
        self.assertEqual(analyzer.catch_up(), {"analysed": [], "failed": []})
        self.assertEqual(store.load_registry()["signatures"][0]["count"], 10)

    def test_options(self):
        store = ReportStore(self.tmp)
        today = D0 + dt.timedelta(days=40)
        for d in days(D0, 40):
            store_day(store, d, [act(d, 7, 2000, 150)], curve_rows=0)
        analyzer = StoreAnalyzer(store, {"report_retention_days": 10, "half_life_days": 2}, today)
        self.assertEqual(analyzer.registry_config.half_life_days, 2)
        analyzer.maintain()
        self.assertEqual(len(store.days()), 10)
        # 2-day half-life: summaries kept 13 days (weight above 1 %)
        self.assertEqual(len(store.summary_days()), 13)
        bad = StoreAnalyzer(store, {"half_life_days": "x", "peak_window_days": 0}, today)
        self.assertEqual(bad.registry_config.half_life_days, 30)
        self.assertEqual(bad.registry_config.peak_window_days, 30)

    def test_learned_switched_power_is_used(self):
        from test_addon import FakeHA

        store = ReportStore(self.tmp)
        store.save_registry(
            {
                "version": 1,
                "signatures": [],
                "switched_loads": [{"entity_id": "switch.water_heater", "power_w": 2400}],
            }
        )
        options = {"switched_loads": [{"entity_id": "switch.water_heater"}, {"entity_id": "switch.other"}]}
        analyzer = Analyzer(FakeHA(0), store, options)
        loads = {s.entity_id: s.power_w for s in analyzer.prepare().derived.config.switched_loads}
        self.assertEqual(loads, {"switch.water_heater": 2400.0, "switch.other": 0.0})

    def test_api(self):
        store = ReportStore(self.tmp)
        for d in days(D0, 120):
            store_day(store, d, [act(d, 7, 2000, 150)], curve_rows=60)
        analyzer = StoreAnalyzer(store, {}, D0 + dt.timedelta(days=120))
        status, body = route(analyzer, store, "GET", "/api/signatures")
        self.assertEqual((status, body["signatures"], body["peak"]), (200, [], None))
        analyzer.maintain()
        status, body = route(analyzer, store, "GET", "/api/signatures")
        self.assertEqual(status, 200)
        self.assertEqual(body["signatures"][0]["id"], "short-2000w")
        self.assertEqual(body["signatures"][0]["count"], 120)
        status, health = route(analyzer, store, "GET", "/api/health")
        self.assertEqual((health["days"], health["signatures"]), (90, 1))
        self.assertGreater(health["storage_bytes"], 0)
        status, body = route(analyzer, store, "GET", "/api/days")
        self.assertEqual(len(body["days"]), 90)
        self.assertEqual(body["summary_days"][0], "2027-01-01")
        self.assertEqual(len(body["summary_days"]), 30)
        # a recent day: full report with registry ids
        status, report = route(analyzer, store, "GET", "/api/report/2027-04-30")
        self.assertEqual((status, len(report["curve"])), (200, 60))
        self.assertEqual(report["activations"][0]["registry_id"], "short-2000w")
        # an old day: the summary
        status, report = route(analyzer, store, "GET", "/api/report/2027-01-05")
        self.assertEqual((status, report["curve"], report["curve_pruned"]), (200, [], True))
        self.assertEqual(report["activations"][0]["registry_id"], "short-2000w")
        self.assertEqual(route(analyzer, store, "DELETE", "/api/signatures")[0], 405)


# ---------------------------------------------------------------------------
# end to end: nightly analyses of a synthetic house
# ---------------------------------------------------------------------------
class KettleHA:
    """A house with a 2 kW kettle at 07:00 every day, recorded every 30 s."""

    def __init__(self, first: dt.date, n: int, peak_day: dt.date | None = None):
        loads = [Steady(midnight(d) + 7 * 3600, midnight(d) + 7 * 3600 + 240, 2000) for d in days(first, n)]
        if peak_day:  # a 7 kW peak that day
            loads.append(Steady(midnight(peak_day) + 12 * 3600, midnight(peak_day) + 12 * 3600 + 600, 7000))
        self.house = House(base_w=250, loads=loads)
        self.data_start = midnight(first)

    def snapshot(self):
        from test_addon import snapshot

        return snapshot()

    def history(self, entities, start, end):
        a = max(start, self.data_start)
        return self.house.histories(a, end, dt=30) if a < end else {}


class NotablePeakTests(unittest.TestCase):
    def setUp(self):
        t0 = midnight(D0)
        house = House(
            base_w=250, loads=[Steady(t0 + 3600, t0 + 3900, 3000), Steady(t0 + 7200, t0 + 7500, 2700)]
        )
        self.t0, self.hist = t0, house.histories(t0, t0 + 4 * 3600, dt=10)

    def run_analysis(self, **kw):
        from nilm_core import AnalysisConfig, analyze

        cfg = AnalysisConfig(grid_entity="sensor.grid_power", context_s=0, **kw)
        return analyze(self.hist, cfg, self.t0, self.t0 + 4 * 3600, local_hour=UTC_HOUR)

    def test_top_peaks_always_listed(self):
        # an ordinary day: both episodes listed (relative to the day), none notable
        r = self.run_analysis(peak_reference_w=8000)
        self.assertEqual(r["peak_threshold_w"], 6400)
        self.assertEqual(len(r["peaks"]), 2)
        self.assertEqual([p["notable"] for p in r["peaks"]], [False, False])
        # no history: relative to the day itself, the highest one is notable
        r = self.run_analysis()
        self.assertAlmostEqual(r["peak_threshold_w"], 0.8 * r["max_house_w"], delta=1)
        self.assertEqual([p["notable"] for p in r["peaks"]], [True, True])
        # a fixed threshold defines "notable", not which episodes are listed
        r = self.run_analysis(peak_threshold_w=3100, peak_reference_w=8000)
        self.assertEqual(r["peak_threshold_w"], 3100)
        self.assertEqual([p["notable"] for p in r["peaks"]], [True, False])
        r = self.run_analysis(peak_threshold_w=20000)
        self.assertEqual(len(r["peaks"]), 2)
        self.assertFalse(any(p["notable"] for p in r["peaks"]))


class EndToEndTests(unittest.TestCase):
    def test_nightly_runs_kettle_and_peak_window(self):
        peak_day = D0 + dt.timedelta(days=2)
        ha = KettleHA(D0, 12, peak_day=peak_day)
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportStore(tmp)
            ids = []
            # installed on the 4th: backfill of the first 3 days, then one night after another
            for n in range(3, 13):
                today = D0 + dt.timedelta(days=n)
                analyzer = Analyzer(
                    ha, store, {"backfill_days": 3}, clock=lambda t=today: midnight(t) + 5 * 3600
                )
                with self.assertLogs("service", "INFO"):
                    analyzer.catch_up()
                kettle = [s for s in store.load_registry()["signatures"] if abs(s["power_w"] - 2000) < 100]
                self.assertEqual(len(kettle), 1)
                ids.append(kettle[0]["id"])
            self.assertEqual(len(set(ids)), 1)  # same id every night
            self.assertEqual((kettle[0]["count"], kettle[0]["days_seen"]), (12, 12))

            peak = store.load(peak_day.isoformat())
            self.assertGreater(peak["max_house_w"], 7000)
            # backfilled days are analysed newest first: no window yet
            first = store.load(D0.isoformat())
            self.assertEqual(first["peak_threshold_w"], round(0.8 * first["max_house_w"]))
            self.assertEqual([p["notable"] for p in first["peaks"]], [True])  # the kettle is the day's peak
            # the exceptional day: its 7 kW peak is notable
            self.assertTrue(peak["peaks"][0]["notable"])
            self.assertGreater(peak["peaks"][0]["house_w"], 7000)
            # the following nights use the maximum of the window: the kettle is
            # still the day's top episode, but not a notable one
            last = store.load((D0 + dt.timedelta(days=11)).isoformat())
            self.assertEqual(last["peak_threshold_w"], round(0.8 * peak["max_house_w"]))
            self.assertEqual(len(last["peaks"]), 1)
            self.assertFalse(last["peaks"][0]["notable"])
            self.assertLess(last["peaks"][0]["house_w"], last["peak_threshold_w"])
            self.assertEqual(last["peaks"][0]["house_w"], last["max_house_w"])
            reg = store.load_registry()
            self.assertEqual(reg["peak"]["reference_w"], peak["max_house_w"])
            self.assertEqual(reg["peak"]["threshold_w"], round(0.8 * peak["max_house_w"]))


if __name__ == "__main__":
    unittest.main()
