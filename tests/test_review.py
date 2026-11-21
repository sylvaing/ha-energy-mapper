"""Tests of the review of the signatures (#10): states, decisions carried by
the registry, bounded storage of ignored signatures, settings and the HTTP
endpoints. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import route  # noqa: E402
from nilm_core import registry as registry_module  # noqa: E402
from nilm_core.registry import update  # noqa: E402
from review import Criteria, ReviewError, decide, listing, state  # noqa: E402
from settings import SettingsError  # noqa: E402
from settings import validate as validate_settings  # noqa: E402
from storage import ReportStore  # noqa: E402
from test_appliances import StoreAnalyzer  # noqa: E402
from test_registry import D0, act, by_id, midnight, nightly, store_day, summary  # noqa: E402

DAY = 86400.0
C = Criteria()


def sig(sid="short-2000w", count=6, days_seen=3, **extra):
    return {"id": sid, "kind": "steady", "band": "short", "power_w": 2000, "label": None, "weight": 4.0} | {
        "count": count,
        "days_seen": days_seen,
        **extra,
    }


def kettle_sums(n_days: int, per_day: int = 2, power: float = 2000):
    out = {}
    for i in range(n_days):
        d = D0 + dt.timedelta(days=i)
        out[d.isoformat()] = summary(d, [act(d, 7 + 3 * k, power, 150) for k in range(per_day)])
    return out


class StateTests(unittest.TestCase):
    def test_maturity(self):
        self.assertEqual(state(sig(count=6, days_seen=3), 0, C, ()), "proposed")
        self.assertEqual(state(sig(count=4, days_seen=3), 0, C, ()), "immature")
        self.assertEqual(state(sig(count=9, days_seen=2), 0, C, ()), "immature")
        lenient = Criteria(min_uses=2, min_days=1)
        self.assertEqual(state(sig(count=2, days_seen=1), 0, lenient, ()), "proposed")

    def test_named_is_validated(self):
        self.assertEqual(state(sig(label="Kettle"), 0, C, ()), "validated")
        self.assertEqual(state(sig(appliance="signature:short-2000w"), 0, C, ()), "validated")

    def test_wait_comes_back_with_more_uses_or_later(self):
        s = sig(count=6)
        decide(s, "wait", 1000.0, C)
        self.assertEqual(s["review"]["until_count"], 12)
        self.assertEqual(s["review"]["until"], 1000.0 + 14 * DAY)
        self.assertEqual(state(s, 1000.0 + DAY, C, ()), "waiting")
        s["count"] = 11
        self.assertEqual(state(s, 1000.0 + DAY, C, ()), "waiting")
        s["count"] = 12  # twice as many uses
        self.assertEqual(state(s, 1000.0 + DAY, C, ()), "proposed")
        s["count"] = 6
        self.assertEqual(state(s, 1000.0 + 14 * DAY, C, ()), "proposed")  # 14 days later
        # few uses: at least one more is needed
        few = sig(count=1, days_seen=1)
        decide(few, "wait", 0.0, Criteria(min_uses=2, min_days=1, wait_factor=1.2))
        self.assertEqual(few["review"]["until_count"], 2)

    def test_ignore_restore_validate(self):
        s = sig()
        decide(s, "ignore", 5.0, C)
        self.assertEqual(s["review"], {"state": "ignored", "at": 5.0})
        self.assertEqual(state(s, 1e12, C, ()), "ignored")
        decide(s, "restore", 6.0, C)
        self.assertNotIn("review", s)
        self.assertEqual(state(s, 6.0, C, ()), "proposed")
        decide(s, "validate", 7.0, C, "load:switch.boiler")
        self.assertEqual(state(s, 7.0, C, ["load:switch.boiler"]), "validated")
        # the appliance was deleted in Home Assistant: not proposed again
        self.assertEqual(state(s, 7.0, C, []), "ignored")
        with self.assertRaises(ReviewError):
            decide(s, "validate", 7.0, C)
        with self.assertRaises(ReviewError):
            decide(s, "maybe", 7.0, C)

    def test_listing_order_and_counts(self):
        reg = {
            "signatures": [
                sig("a", weighted_energy_kwh=1.0),
                sig("b", weighted_energy_kwh=5.0),
                sig("c", review={"state": "ignored", "at": 1.0}),
                sig(
                    "d", review={"state": "waiting", "at": 1.0, "count": 6, "until_count": 12, "until": 99.0}
                ),
                sig("e", count=1),
                sig("f", label="Kettle"),
            ]
        }
        got = listing(reg, 10.0, C, ())
        self.assertEqual(
            [(s["id"], s["review_state"]) for s in got["signatures"]],
            [
                ("b", "proposed"),
                ("a", "proposed"),
                ("d", "waiting"),
                ("c", "ignored"),
            ],
        )
        self.assertEqual(got["counts"], {"proposed": 2, "waiting": 1, "ignored": 1, "validated": 1})
        self.assertEqual(got["criteria"]["min_uses"], 5)
        self.assertEqual(listing(None, 0, C, ())["signatures"], [])


class RegistryCarryTests(unittest.TestCase):
    def test_decision_follows_the_signature(self):
        sums = kettle_sums(4)
        reg = nightly(sums, D0 + dt.timedelta(days=3), None)
        sid = reg["signatures"][0]["id"]
        decide(by_id(reg, sid), "wait", 1.0, C)
        more = kettle_sums(6)
        reg2 = nightly(more, D0 + dt.timedelta(days=5), reg)
        self.assertEqual(by_id(reg2, sid)["review"]["state"], "waiting")
        # unrelated signatures get no review key
        self.assertNotIn("review", nightly(more, D0 + dt.timedelta(days=5), None)["signatures"][0])

    def test_ignored_signature_survives_fading_out(self):
        reg = nightly(kettle_sums(4), D0 + dt.timedelta(days=3), None)
        sid = reg["signatures"][0]["id"]
        plain = nightly({}, D0 + dt.timedelta(days=400), reg)
        self.assertIsNone(by_id(plain, sid))  # forgotten
        decide(by_id(reg, sid), "ignore", 1.0, C)
        kept = nightly({}, D0 + dt.timedelta(days=400), reg)
        self.assertEqual(by_id(kept, sid)["review"]["state"], "ignored")
        self.assertLess(by_id(kept, sid)["weight"], 0.01)
        # the kettle comes back a year later: same id, still ignored
        later = D0 + dt.timedelta(days=401)
        back = nightly({later.isoformat(): summary(later, [act(later, 8, 2030, 150)])}, later, kept)
        self.assertEqual(by_id(back, sid)["review"]["state"], "ignored")
        self.assertEqual(by_id(back, sid)["count"], 1)

    def test_faded_ignored_signatures_are_bounded(self):
        cap = registry_module.MAX_FADED_IGNORED
        sigs = [
            sig(f"short-{1000 + 100 * i}w", review={"state": "ignored", "at": float(i)})
            | {"power_w": 1000 + 100 * i}
            for i in range(cap + 5)
        ]
        prev = {
            "version": 1,
            "updated_at": midnight(D0),
            "power_tol": 0.12,
            "signatures": sigs,
            "switched_loads": [],
        }
        reg = update(prev, {}, midnight(D0 + dt.timedelta(days=400)), None)
        kept = {s["id"] for s in reg["signatures"]}
        self.assertEqual(len(kept), cap)
        self.assertNotIn("short-1000w", kept)  # the oldest decisions go first
        self.assertIn(f"short-{1000 + 100 * (cap + 4)}w", kept)


class SettingsTests(unittest.TestCase):
    def test_review_keys(self):
        got = validate_settings({"review_min_uses": 8, "review_wait_factor": 3, "review_wait_days": 30})
        self.assertEqual(got, {"review_min_uses": 8, "review_wait_factor": 3.0, "review_wait_days": 30})
        for bad in ({"review_min_uses": 1}, {"review_wait_factor": 1.0}, {"review_min_days": 2.5}):
            with self.subTest(bad=bad), self.assertRaises(SettingsError):
                validate_settings(bad)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReportStore(self._tmp.name)
        self.today = D0 + dt.timedelta(days=4)
        self.analyzer = StoreAnalyzer(self.store, self.today)
        for i in range(4):
            d = D0 + dt.timedelta(days=i)
            store_day(self.store, d, [act(d, 8, 2000, 150), act(d, 19, 2000, 150)])
        self.analyzer.maintain()
        self.sid = self.store.load_registry()["signatures"][0]["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, method, target, body=None):
        raw = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body or b""
        return route(self.analyzer, self.store, method, target, raw)

    def states(self):
        return {s["id"]: s["review_state"] for s in self.call("GET", "/api/review")[1]["signatures"]}

    def test_review_flow(self):
        status, answer = self.call("GET", "/api/review")
        self.assertEqual(status, 200)
        self.assertEqual(answer["counts"]["proposed"], 1)
        self.assertEqual(answer["signatures"][0]["examples"][0]["day"], "2027-01-04")
        self.assertEqual(self.states(), {self.sid: "proposed"})

        status, answer = self.call("POST", "/api/review", {"id": self.sid, "action": "wait"})
        self.assertEqual((status, answer["signature"]["review_state"]), (200, "waiting"))
        self.assertEqual(self.states(), {self.sid: "waiting"})
        # kept by the nightly update and across restarts
        self.analyzer.maintain()
        restarted = StoreAnalyzer(self.store, self.today)
        self.assertEqual(route(restarted, self.store, "GET", "/api/review")[1]["counts"]["waiting"], 1)

        self.assertEqual(self.call("POST", "/api/review", {"id": self.sid, "action": "ignore"})[0], 200)
        self.assertEqual(self.states(), {self.sid: "ignored"})
        self.assertEqual(self.call("POST", "/api/review", {"id": self.sid, "action": "restore"})[0], 200)
        self.assertEqual(self.states(), {self.sid: "proposed"})

        # validated by linking it to a switch: the appliance must be declared
        body = {"id": self.sid, "action": "validate", "appliance": "load:switch.kettle"}
        status, answer = self.call("POST", "/api/review", body)
        self.assertEqual((status, answer["signature"]["review_state"]), (200, "ignored"))
        appliance = {
            "key": "load:switch.kettle",
            "name": "Kettle",
            "sources": [{"type": "switched_load", "entity_id": "switch.kettle"}],
        }
        self.call("PUT", "/api/appliances", {"appliances": [appliance]})
        self.assertEqual(self.states(), {})
        self.assertEqual(self.call("GET", "/api/review")[1]["counts"]["validated"], 1)

    def test_criteria_from_the_settings(self):
        self.analyzer.set_settings({"review_min_uses": 20})
        self.assertEqual(self.states(), {})
        self.assertEqual(self.call("GET", "/api/review")[1]["criteria"]["min_uses"], 20)

    def test_decision_during_the_nightly_update_is_kept(self):
        stored = self.store.load_registry()
        decide(by_id(stored, self.sid), "ignore", 1.0, C)
        original = self.store.load_registry

        def load_then_decide():
            # the nightly update reads the registry, then a decision is saved
            out = original()
            self.store.load_registry = original
            self.store.save_registry(stored)
            return out

        self.store.load_registry = load_then_decide
        self.analyzer.maintain()
        self.store.load_registry = original
        self.assertEqual(by_id(self.store.load_registry(), self.sid)["review"]["state"], "ignored")

    def test_invalid(self):
        for bad in (
            {"id": self.sid},
            {"id": self.sid, "action": "later"},
            {"id": "", "action": "wait"},
            {"id": self.sid, "action": "wait", "extra": 1},
            {"id": self.sid, "action": "validate"},
            {"id": self.sid, "action": "validate", "appliance": " "},
            [],
        ):
            with self.subTest(bad=bad):
                status, answer = self.call("POST", "/api/review", bad)
                self.assertEqual((status, answer["error"]), (400, "invalid_review"))
        self.assertEqual(self.call("POST", "/api/review", b"{")[1]["error"], "invalid_json")
        status, answer = self.call("POST", "/api/review", {"id": "nope", "action": "wait"})
        self.assertEqual((status, answer["error"]), (404, "not_found"))
        self.assertEqual(self.call("PUT", "/api/review")[0], 405)


if __name__ == "__main__":
    unittest.main()
