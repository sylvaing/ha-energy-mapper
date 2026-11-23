"""Tests of the labelled examples (#17): their declaration in the add-on's
appliances, finding them again in the analysed days (found, linked,
pending with a reason, contradicting), the end-to-end refinement of the
registry, and the HTTP endpoints. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import route  # noqa: E402
from appliances import MAX_EXAMPLES, AppliancesError, apply_to_registry, validate  # noqa: E402
from examples import (  # noqa: E402
    DAY_MISSING,
    DAY_NOT_ANALYSED,
    FOUND,
    MEASURED,
    NO_ACTIVATION,
    NOT_IN_REGISTRY,
    OTHER,
    PENDING,
    SMALL_UNTRACKED,
    TOO_SMALL,
    find_activation,
    status,
)
from nilm_core import AnalysisConfig, analyze  # noqa: E402
from nilm_core.registry import update  # noqa: E402
from storage import summary_of  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady  # noqa: E402
from test_addon import TODAY, FakeHA, make_analyzer, midnight  # noqa: E402

DAY = 24 * HOUR
GRID = "sensor.grid_power"
UTC_HOUR = lambda ts: int(ts // 3600) % 24  # noqa: E731


def example(t_on, t_off=None, power_w=2000.0, source="live", **kw):
    return {"t_on": t_on, "t_off": t_off, "power_w": power_w, "source": source, "created": t_on, **kw}


def summary(day0, *activations):
    return {
        "start": day0,
        "end": day0 + DAY,
        "activations": [{"start": a, "end": b, "power_w": p} for a, b, p in activations],
        "cycles": [],
    }


def sig(sid, power, band="short", **kw):
    return {"id": sid, "kind": "steady", "band": band, "power_w": power, "count": 9, "label": None, **kw}


REGISTRY = {
    "power_tol": 0.12,
    "signatures": [sig("short-2000w", 2000), sig("short-1000w", 1000), sig("medium-2400w", 2400, "medium")],
}
D0 = DAY0
SUMMARIES = {
    "2027-01-01": summary(D0, (D0 + 3600, D0 + 3780, 2010), (D0 + 7200, D0 + 7320, 1000)),
    "2027-01-02": summary(D0 + DAY, (D0 + DAY + 3600, D0 + DAY + 3800, 1990)),
}


def kettle(sources=(), examples=()):
    return {"key": "appliance:kettle", "name": "Kettle", "sources": list(sources), "examples": list(examples)}


SOURCE_2000 = {"type": "signature", "id": "short-2000w"}


# ---------------------------------------------------------------------------
# declaration
# ---------------------------------------------------------------------------
class DeclarationTests(unittest.TestCase):
    def test_validate(self):
        ex = example(D0 + 3600, D0 + 3780, signature_id="short-2000w", live={"id": 4, "session": 17})
        (a,) = validate({"appliances": [kettle(examples=[ex])]})
        self.assertEqual(a["examples"], [{**ex, "t_on": float(ex["t_on"]), "t_off": float(ex["t_off"])}])
        # an open example; no examples: no key
        (a,) = validate({"appliances": [kettle(examples=[example(D0)])]})
        self.assertIsNone(a["examples"][0]["t_off"])
        (a,) = validate({"appliances": [kettle()]})
        self.assertNotIn("examples", a)

    def test_rejects(self):
        bad = [
            example(D0, D0 - 1),  # stops before it starts
            example(D0, D0 + 2 * DAY),  # longer than a day
            example(D0, source="guess"),
            example(D0, power_w=0),
            example(D0, power_w=True),
            example(D0, other=1),
            example(D0, signature_id="Not An Id"),
            example(D0, live={"id": 1}),
            {"t_off": None, "power_w": 1, "source": "live", "created": 0},
        ]
        for ex in bad:
            with self.subTest(ex=ex), self.assertRaises(AppliancesError):
                validate({"appliances": [kettle(examples=[ex])]})
        with self.assertRaises(AppliancesError):
            validate({"appliances": [kettle(examples=[example(D0 + i) for i in range(MAX_EXAMPLES + 1)])]})


# ---------------------------------------------------------------------------
# finding the examples again
# ---------------------------------------------------------------------------
class FindTests(unittest.TestCase):
    def test_closed_example_overlap(self):
        day = SUMMARIES["2027-01-01"]
        self.assertEqual(find_activation(example(D0 + 3610, D0 + 3770), day)["power_w"], 2010)
        # overlapping less than half of the shorter one: not it
        self.assertIsNone(find_activation(example(D0 + 3700, D0 + 3900), day))

    def test_open_example_by_its_start(self):
        day = SUMMARIES["2027-01-01"]
        self.assertEqual(find_activation(example(D0 + 3620), day)["power_w"], 2010)
        self.assertIsNone(find_activation(example(D0 + 3650), day))  # 50 s away

    def test_found_with_the_appliance_signature(self):
        a = kettle([SOURCE_2000], [example(D0 + 3600, D0 + 3780), example(D0 + DAY + 3605)])
        got = status(a, SUMMARIES, REGISTRY)
        self.assertEqual((got["total"], got["found"], got["pending"]), (2, 2, 0))
        self.assertEqual([e["status"] for e in got["examples"]], [FOUND, FOUND])
        self.assertEqual(got["examples"][1]["activation"]["end"], D0 + DAY + 3800)
        self.assertEqual(got["signature"]["id"], "short-2000w")
        self.assertIsNone(got["link"])
        self.assertEqual(got["disagreement"], [])

    def test_link_an_appliance_without_signature(self):
        a = kettle(examples=[example(D0 + 3600, D0 + 3780), example(D0 + DAY + 3600, D0 + DAY + 3800)])
        got = status(a, SUMMARIES, REGISTRY)
        self.assertEqual(got["link"]["id"], "short-2000w")
        self.assertEqual(got["link"]["count"], 9)
        self.assertEqual(got["replace"], [])
        self.assertEqual(got["found"], 2)

    def test_an_expected_signature_is_replaced(self):
        expected = sig(
            "short-1900w-2", 1500, count=0, expected=True, label="Kettle", appliance="appliance:kettle"
        )
        registry = {**REGISTRY, "signatures": [*REGISTRY["signatures"], expected]}
        a = kettle([{"type": "signature", "id": "short-1900w-2"}], [example(D0 + 3600, D0 + 3780)])
        got = status(a, SUMMARIES, registry)
        self.assertEqual(got["link"]["id"], "short-2000w")
        self.assertEqual(got["replace"], ["short-1900w-2"])
        self.assertEqual(got["found"], 1)

    def test_a_signature_of_another_appliance_is_a_conflict(self):
        registry = {
            **REGISTRY,
            "signatures": [sig("short-2000w", 2000, label="Toaster", appliance="appliance:toaster")],
        }
        got = status(kettle(examples=[example(D0 + 3600, D0 + 3780)]), SUMMARIES, registry)
        self.assertIsNone(got["link"])
        self.assertEqual(got["conflict"], {"id": "short-2000w", "appliance": "appliance:toaster"})
        self.assertEqual(got["examples"][0]["status"], OTHER)
        self.assertEqual(got["examples"][0]["owner"], "appliance:toaster")

    def test_contradicting_examples(self):
        a = kettle(
            [SOURCE_2000],
            [example(D0 + 3600, D0 + 3780), example(D0 + 7200, D0 + 7320, power_w=1000)],
        )
        got = status(a, SUMMARIES, REGISTRY)
        self.assertEqual((got["found"], got["other"]), (1, 1))
        self.assertEqual(got["disagreement"], ["short-1000w"])
        self.assertIsNone(got["link"])  # it has a signature of its own already

    def test_majority_wins_the_link(self):
        a = kettle(
            examples=[
                example(D0 + 7200, D0 + 7320, power_w=1000),
                example(D0 + 3600, D0 + 3780),
                example(D0 + DAY + 3600, D0 + DAY + 3800),
            ]
        )
        got = status(a, SUMMARIES, REGISTRY)
        self.assertEqual(got["link"]["id"], "short-2000w")
        self.assertEqual((got["found"], got["other"]), (2, 1))

    def test_pending_reasons(self):
        registry = {"power_tol": 0.12, "signatures": [sig("short-2000w", 2000)]}
        cases = [
            (example(D0 + 2 * DAY + 60, D0 + 2 * DAY + 200), DAY_NOT_ANALYSED),  # not analysed yet
            (example(D0 - DAY + 60, D0 - DAY + 200), DAY_MISSING),  # before the stored days
            (example(D0 + 5 * HOUR, D0 + 5 * HOUR + 120), NO_ACTIVATION),
            (example(D0 + 5 * HOUR, D0 + 5 * HOUR + 120, power_w=150), TOO_SMALL),
            (example(D0 + 7200, D0 + 7320, power_w=1000), NOT_IN_REGISTRY),
        ]
        for ex, reason in cases:
            with self.subTest(reason=reason):
                (item,) = status(kettle(examples=[ex]), SUMMARIES, registry)["examples"]
                self.assertEqual((item["status"], item["reason"]), (PENDING, reason))

    def test_under_the_threshold_the_measured_noise_decides(self):
        """#100: no activation and a power under the step threshold."""
        ex = example(D0 + 5 * HOUR, D0 + 5 * HOUR + 120, power_w=70)

        def reason(visible_w, e=ex, threshold=300.0):
            got = status(kettle(examples=[e]), SUMMARIES, REGISTRY, threshold, visible_w=visible_w)
            (item,) = got["examples"]
            self.assertEqual((item["status"], item["small_use"], got["pending"]), (PENDING, None, 1))
            return item["reason"]

        # at or above the noise: the curve shows it, it is not tracked yet
        self.assertEqual(reason(40.0), SMALL_UNTRACKED)
        self.assertEqual(reason(70.0), SMALL_UNTRACKED)
        # under the noise: the curve cannot show it
        self.assertEqual(reason(120.0), TOO_SMALL)
        # the noise is not measured yet: the former verdict
        self.assertEqual(reason(None), TOO_SMALL)
        # at or above the step threshold the noise says nothing
        big = example(D0 + 5 * HOUR, D0 + 5 * HOUR + 120, power_w=300)
        self.assertEqual(reason(40.0, big), NO_ACTIVATION)
        self.assertEqual(reason(600.0, big), NO_ACTIVATION)
        self.assertEqual(reason(40.0, ex, 60.0), NO_ACTIVATION)

    def test_a_small_use_of_the_day_is_the_example(self):
        """#100: the day's small uses, by the overlap rule of the activations."""
        t0 = D0 + 5 * HOUR
        day = {
            **SUMMARIES["2027-01-01"],
            "small_uses": [
                {"start": t0 + 4, "end": t0 + 118, "power_w": 68.4, "on_w": 71.0, "off_w": 66.0},
                {"start": t0 + 3600, "end": t0 + 3660, "power_w": 55.0, "on_w": 55.0, "off_w": 55.0},
            ],
        }
        summaries = {**SUMMARIES, "2027-01-01": day}

        def item(e, visible_w=None):
            return status(kettle(examples=[e]), summaries, REGISTRY, visible_w=visible_w)["examples"][0]

        found = {"day": "2027-01-01", "start": t0 + 4, "end": t0 + 118, "power_w": 68}
        # seen by the curve whatever the noise of the last days says
        for visible in (None, 40.0, 120.0):
            got = item(example(t0, t0 + 120, power_w=70), visible)
            self.assertEqual(
                (got["status"], got["reason"], got["small_use"]), (PENDING, SMALL_UNTRACKED, found)
            )
            self.assertIsNone(got["activation"])
            self.assertIsNone(got["signature_id"])
        # an open example: the small use that starts with it
        got = item(example(t0, power_w=70))
        self.assertEqual((got["reason"], got["small_use"]), (SMALL_UNTRACKED, found))
        # no overlap: the noise decides
        apart = example(t0 + 1800, t0 + 1900, power_w=70)
        self.assertEqual((item(apart)["reason"], item(apart)["small_use"]), (TOO_SMALL, None))
        self.assertEqual(item(apart, 40.0)["reason"], SMALL_UNTRACKED)
        # an example the analysis tracks is not looked for in the small uses
        got = item(example(t0, t0 + 120, power_w=2000))
        self.assertEqual((got["reason"], got["small_use"]), (NO_ACTIVATION, None))
        # an activation comes first
        act = {**day, "small_uses": [{"start": D0 + 3600, "end": D0 + 3780, "power_w": 70.0}]}
        got = status(
            kettle(examples=[example(D0 + 3600, D0 + 3780, power_w=70)]),
            {**SUMMARIES, "2027-01-01": act},
            REGISTRY,
        )
        self.assertEqual(
            (got["examples"][0]["signature_id"], got["examples"][0]["small_use"]), ("short-2000w", None)
        )

    def test_measured_appliance_is_not_evaluated(self):
        a = kettle([{"type": "switched_load", "entity_id": "switch.kettle"}], [example(D0 + 3600, D0 + 3780)])
        got = status(a, SUMMARIES, REGISTRY)
        self.assertTrue(got["measured"])
        self.assertEqual(got["examples"][0]["reason"], MEASURED)
        self.assertIsNone(got["link"])


# ---------------------------------------------------------------------------
# end to end: examples on synthetic days refine the registry
# ---------------------------------------------------------------------------
def synthetic_days(days=6, seed=5):
    """Days with a toaster (1.2 kW, ~3 min, once a day) among distractors;
    returns the summaries and the toaster's true uses."""
    rnd = random.Random(seed)
    summaries, uses = {}, []
    for i in range(days):
        d0 = DAY0 + i * DAY
        t = d0 + rnd.uniform(7, 9) * HOUR
        toaster = Steady(t, t + rnd.uniform(150, 210), rnd.uniform(1180, 1220))
        uses.append((toaster.start, toaster.end))
        loads = [
            Cycling(d0, d0 + DAY, 110, 15 * 60, 30 * 60),  # fridge
            Steady(d0 + 1.5 * HOUR, d0 + 4 * HOUR, 3000),  # water heater
            Steady(d0 + 19 * HOUR, d0 + 19 * HOUR + 240, 2000),  # kettle
            toaster,
        ]
        house = House(base_w=250, loads=loads, noise_w=10, seed=seed * 100 + i)
        report = analyze(
            house.histories(d0 - 3 * HOUR, d0 + DAY),
            AnalysisConfig(grid_entity=GRID),
            d0,
            d0 + DAY,
            local_hour=UTC_HOUR,
        )
        summaries[(dt.date(2027, 1, 1) + dt.timedelta(days=i)).isoformat()] = summary_of(report)
    return summaries, uses


class EndToEndTests(unittest.TestCase):
    def test_examples_find_link_and_refine(self):
        summaries, uses = synthetic_days()
        end = DAY0 + 6 * DAY
        registry = update(None, summaries, end, local_hour=UTC_HOUR)
        # three labelled uses (as the live view sees them: a few seconds off),
        # one open (the tab was closed before the stop)
        labelled = [
            example(uses[1][0] + 4, uses[1][1] + 3, power_w=1200),
            example(uses[3][0] - 3, uses[3][1] - 2, power_w=1190, source="guided"),
            example(uses[4][0] + 5, power_w=1210),
        ]
        toaster = {"key": "appliance:toaster", "name": "Toaster", "sources": [], "examples": labelled}
        got = status(toaster, summaries, registry)
        self.assertEqual(got["found"], 3, got["examples"])
        link = got["link"]
        self.assertIsNotNone(link)
        self.assertEqual((link["kind"], link["band"]), ("steady", "short"))
        self.assertAlmostEqual(link["power_w"], 1200, delta=40)
        self.assertGreaterEqual(link["count"], 6)  # every past use is attached
        # the open one gets the activation's end
        self.assertAlmostEqual(got["examples"][2]["activation"]["end"], uses[4][1], delta=30)

        # linked by the integration: the signature becomes a source, the
        # registry gives it the appliance's label, the examples are found
        toaster["sources"] = [{"type": "signature", "id": link["id"]}]
        self.assertTrue(apply_to_registry(registry, [toaster], end))
        sig = next(s for s in registry["signatures"] if s["id"] == link["id"])
        self.assertEqual((sig["label"], sig["appliance"]), ("Toaster", "appliance:toaster"))
        again = status(toaster, summaries, registry)
        self.assertEqual((again["found"], again["total"]), (3, 3))
        self.assertIsNone(again["link"])
        self.assertEqual(again["signature"]["label"], "Toaster")
        # the next night keeps the label on the signature (its identity)
        nxt = update(registry, summaries, end + DAY, local_hour=UTC_HOUR)
        self.assertTrue(any(s.get("label") == "Toaster" for s in nxt["signatures"]))


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
class FakeLive:
    def __init__(self):
        self.calls = []

    def event(self, event_id, session):
        self.calls.append((event_id, session))
        if session == 9:
            return "session_ended", None
        if event_id == 3:
            return "ok", {"session": 7, "event": {"id": 3, "t_on": 1.0}}
        return "not_found", None


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ha = FakeHA(data_start=midnight(TODAY) - 3 * 86400)
        self.analyzer, self.store = make_analyzer(self._tmp.name, self.ha)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, method, target, live=None):
        return route(self.analyzer, self.store, method, target, b"", live)

    def test_examples_endpoint(self):
        self.assertEqual(self.call("GET", "/api/examples"), (200, {"last_day": None, "appliances": []}))
        report = self.analyzer.analyze_day(TODAY - dt.timedelta(days=1))
        act = report["activations"][0]
        ex = example(act["start"] + 2, act["end"] - 2, power_w=act["power_w"])
        body = json.dumps({"appliances": [kettle(examples=[ex]), {**kettle(), "key": "other"}]}).encode()
        self.assertEqual(route(self.analyzer, self.store, "PUT", "/api/appliances", body)[0], 200)
        status_code, answer = self.call("GET", "/api/examples")
        self.assertEqual(status_code, 200)
        self.assertEqual(answer["last_day"], (TODAY - dt.timedelta(days=1)).isoformat())
        (item,) = answer["appliances"]  # only the appliances with examples
        self.assertEqual((item["key"], item["found"], item["total"]), ("appliance:kettle", 1, 1))
        self.assertIsNotNone(item["link"])
        self.assertEqual(self.call("POST", "/api/examples")[0], 405)

    def test_examples_endpoint_reads_the_measured_noise(self):
        """#100: the endpoint decides "too small" from the stored reports."""
        report = self.analyzer.analyze_day(TODAY - dt.timedelta(days=1))
        visible = report["quality"]["meters"]["visible_w"]
        self.assertEqual(self.analyzer.visible_w(self.store.summaries_all()), visible)
        self.assertLess(visible, self.analyzer.smallest_step_w)
        # a quiet minute of the day: no activation there
        t0 = next(
            t
            for t in range(int(report["start"]) + 600, int(report["end"]), 600)
            if all(a["end"] < t - 300 or a["start"] > t + 360 for a in report["activations"])
        )
        seen = example(t0, t0 + 60, power_w=visible + 5)
        under = example(t0 + 1, t0 + 60, power_w=visible - 5)
        body = json.dumps({"appliances": [kettle(examples=[seen, under])]}).encode()
        self.assertEqual(route(self.analyzer, self.store, "PUT", "/api/appliances", body)[0], 200)
        (item,) = self.call("GET", "/api/examples")[1]["appliances"]
        self.assertEqual([e["reason"] for e in item["examples"]], [SMALL_UNTRACKED, TOO_SMALL])
        self.assertEqual((item["found"], item["pending"]), (0, 2))

    def test_live_event_endpoint(self):
        live = FakeLive()
        self.assertEqual(
            self.call("GET", "/api/live/event?id=3&session=7", live),
            (200, {"session": 7, "event": {"id": 3, "t_on": 1.0}}),
        )
        status_code, body = self.call("GET", "/api/live/event?id=4&session=7", live)
        self.assertEqual((status_code, body["error"]), (404, "not_found"))
        status_code, body = self.call("GET", "/api/live/event?id=3&session=9", live)
        self.assertEqual((status_code, body["error"]), (409, "session_ended"))
        self.assertEqual(self.call("GET", "/api/live/event?id=3", live)[0], 200)
        self.assertEqual(live.calls[-1], (3, None))
        self.assertEqual(self.call("GET", "/api/live/event?id=x", live)[0], 400)
        self.assertEqual(self.call("GET", "/api/live/event?id=3")[1]["error"], "session_ended")  # disabled


class LiveOptionsTests(unittest.TestCase):
    def test_examples_change_no_live_option(self):
        """Examples are not part of the live configuration: labelling does
        not restart the live session (``live_options``)."""
        with tempfile.TemporaryDirectory() as tmp:
            analyzer, _ = make_analyzer(tmp, FakeHA(data_start=0))
            analyzer.set_appliances(validate({"appliances": [kettle()]}))
            before = analyzer.live_options()
            analyzer.set_appliances(validate({"appliances": [kettle(examples=[example(D0)])]}))
            self.assertEqual(analyzer.live_options(), before)


if __name__ == "__main__":
    unittest.main()
