"""Tests of the entity suggestions (#41): scoring of the state changes of
Home Assistant entities against the steps of a signature, candidate
selection, stored changes, the nightly computation and ``GET /api/review``.
Synthetic data only: the entities are made up."""

from __future__ import annotations

import datetime as dt
import pathlib
import random
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import route  # noqa: E402
from ha_client import chunks  # noqa: E402
from nilm_core.correlation import (  # noqa: E402
    CorrelationConfig,
    day_changes,
    score_entity,
    signature_edges,
    suggest,
    window_s,
)
from service import Analyzer  # noqa: E402
from settings import SettingsError  # noqa: E402
from settings import validate as validate_settings  # noqa: E402
from storage import ReportStore  # noqa: E402
from suggest import annotate_listing, candidates, changes_file, day_changes_of, unrecorded  # noqa: E402
from test_registry import D0, act, midnight, summary  # noqa: E402

DAY = 86400.0
N_DAYS = 6
# a 2 kW appliance used twice a day, 25 minutes
USES = [(7.0, 1500.0), (19.5, 1500.0)]


def days(n=N_DAYS):
    return [D0 + dt.timedelta(days=i) for i in range(n)]


def fan_summaries(n=N_DAYS, samples=2880):
    """Day summaries with the uses of a 2 kW appliance (sampled every 30 s)."""
    out = {}
    for d in days(n):
        s = summary(d, [act(d, h, 2000, dur) for h, dur in USES])
        s["quality"] = {"samples": samples}
        out[d.isoformat()] = s
    return out


def registry_of(sums):
    from nilm_core.registry import update

    reg = update(None, sums, midnight(D0 + dt.timedelta(days=N_DAYS)))
    sid = reg["signatures"][0]["id"]
    return reg, sid


def uses(d):
    return [(midnight(d) + h * 3600, midnight(d) + h * 3600 + dur) for h, dur in USES]


def follows(d, on="on", off="off", lag=20.0):
    """States of an entity that follows the appliance (a few seconds early)."""
    pts = [(midnight(d), off)]
    for a, b in uses(d):
        pts += [(a - lag, on), (b - lag, off)]
    return pts


def chatty(d, every=180.0):
    return [(midnight(d) + i * every, "on" if i % 2 else "off") for i in range(int(DAY / every))]


def unrelated(d):
    rnd = random.Random(d.toordinal())
    pts = [(midnight(d), "off")]
    for _ in range(4):
        t = midnight(d) + rnd.uniform(9, 17) * 3600
        pts += [(t, "on"), (t + 600, "off")]
    return sorted(pts)


def changes_for(entity_states):
    """{day: {entity: changes}} from {entity: fn(day) -> states}."""
    out = {}
    for d in days():
        start, end = midnight(d), midnight(d) + DAY
        out[d.isoformat()] = {e: day_changes(fn(d), start, end, 100)[0] for e, fn in entity_states.items()}
    return out


class ChangesTests(unittest.TestCase):
    def test_changes_skip_the_initial_state_and_unavailable(self):
        pts = [(0, "off"), (10, "on"), (20, "unavailable"), (30, "on"), (40, "off"), (200, "on")]
        self.assertEqual(day_changes(pts, 0, 100, 10), ([(10, "on"), (40, "off")], 2))

    def test_too_many_changes(self):
        pts = [(i, "on" if i % 2 else "off") for i in range(50)]
        self.assertEqual(day_changes(pts, 0, 100, 10), (None, 49))

    def test_window_follows_the_sampling(self):
        cfg = CorrelationConfig()
        s = {"start": 0, "end": DAY, "quality": {"samples": 1440}}  # 60 s
        self.assertEqual(window_s(s, cfg), 120)
        self.assertEqual(window_s({**s, "quality": {"samples": 86400}}, cfg), 60)
        self.assertEqual(window_s({**s, "quality": {"samples": 24}}, cfg), 300)
        self.assertEqual(window_s({"start": 0, "end": DAY}, cfg), 60)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.sums = fan_summaries()
        self.reg, self.sid = registry_of(self.sums)

    def test_edges_of_a_signature(self):
        edges = signature_edges(self.sums, self.reg, [self.sid], CorrelationConfig())
        self.assertEqual(len(edges[self.sid]), N_DAYS)
        first = edges[self.sid][D0.isoformat()]
        self.assertEqual([e.on for e in first], [True, False, True, False])

    def test_the_entity_that_follows_wins(self):
        changes = changes_for(
            {
                "fan.attic": follows,
                "switch.pilot_wire": lambda d: follows(d, on="off", off="on"),
                "binary_sensor.motion": chatty,
                "light.desk": unrelated,
            }
        )
        got = suggest(self.sums, self.reg, [self.sid], changes)
        found = {c["entity_id"]: c for c in got[self.sid]}
        self.assertEqual(set(found), {"fan.attic", "switch.pilot_wire"})
        fan = found["fan.attic"]
        self.assertEqual((fan["score"], fan["on_share"], fan["off_share"]), (1.0, 1.0, 1.0))
        self.assertEqual((fan["matches"], fan["steps"], fan["days"]), (4 * N_DAYS, 4 * N_DAYS, N_DAYS))
        self.assertEqual((fan["on_states"], fan["inverted"]), (["on"], False))
        pilot = found["switch.pilot_wire"]
        self.assertEqual((pilot["on_states"], pilot["inverted"]), (["off"], True))

    def test_a_chatty_entity_is_rejected_by_the_reverse_share(self):
        changes = changes_for({"binary_sensor.motion": lambda d: chatty(d, every=900)})
        windows = {d: 60.0 for d in changes}
        edges = signature_edges(self.sums, self.reg, [self.sid], CorrelationConfig())[self.sid]
        cand = score_entity(edges, {d: c["binary_sensor.motion"] for d, c in changes.items()}, windows)
        self.assertLess(cand["explained"], 0.3)
        self.assertEqual(suggest(self.sums, self.reg, [self.sid], changes), {})

    def test_multi_state_entity(self):
        def washer(d):
            pts = [(midnight(d), "idle")]
            for a, b in uses(d):
                pts += [(a - 10, "running"), (b + 30, "finished")]
            return pts

        got = suggest(self.sums, self.reg, [self.sid], changes_for({"sensor.washer_state": washer}))
        self.assertEqual(got[self.sid][0]["on_states"], ["running"])
        self.assertFalse(got[self.sid][0]["inverted"])

    def test_thresholds(self):
        # matches most steps but misses too many
        def flaky(d):
            pts = follows(d)
            return pts[:3] if d.toordinal() % 2 else pts

        got = suggest(self.sums, self.reg, [self.sid], changes_for({"fan.flaky": flaky}))
        self.assertEqual(got, {})
        lenient = CorrelationConfig(min_score=0.6)
        got = suggest(self.sums, self.reg, [self.sid], changes_for({"fan.flaky": flaky}), lenient)
        self.assertEqual(got[self.sid][0]["entity_id"], "fan.flaky")
        self.assertEqual(got[self.sid][0]["score"], 0.75)
        # two days of history are not enough
        two = {d: c for d, c in changes_for({"fan.attic": follows}).items() if d <= "2027-01-02"}
        self.assertEqual(suggest(self.sums, self.reg, [self.sid], two), {})
        # unknown signatures: nothing
        self.assertEqual(suggest(self.sums, self.reg, ["nope"], changes_for({"fan.attic": follows})), {})

    def test_days_without_history_are_not_compared(self):
        changes = changes_for({"fan.attic": follows})
        changes[D0.isoformat()]["fan.attic"] = None  # too many changes that day
        del changes[(D0 + dt.timedelta(days=1)).isoformat()]  # no stored changes
        got = suggest(self.sums, self.reg, [self.sid], changes)[self.sid][0]
        self.assertEqual((got["days"], got["steps"], got["score"]), (N_DAYS - 2, 4 * (N_DAYS - 2), 1.0))


class CandidateTests(unittest.TestCase):
    def test_candidates(self):
        states = [
            {"entity_id": e, "attributes": attrs}
            for e, attrs in (
                ("light.desk", {}),
                ("switch.relay", {}),
                ("fan.attic", {}),
                ("climate.living", {}),
                ("water_heater.tank", {}),
                ("media_player.tv", {}),
                ("binary_sensor.door", {"device_class": "door"}),
                ("sensor.washer_state", {"device_class": "enum"}),
                ("sensor.outdoor_temperature", {"device_class": "temperature"}),
                ("sensor.washer_power", {"device_class": "power"}),
                ("switch.diagnostic", {}),
                ("sensor.energy_mapper_state", {"device_class": "enum"}),
                ("person.someone", {}),
                ("switch.water_heater", {}),
            )
        ]
        registry = [
            {"entity_id": "switch.diagnostic", "entity_category": "diagnostic"},
            {"entity_id": "sensor.energy_mapper_state", "platform": "energy_mapper"},
        ]
        got = candidates(states, registry, {"switch.water_heater"})
        self.assertEqual(
            got,
            [
                "binary_sensor.door",
                "climate.living",
                "fan.attic",
                "light.desk",
                "media_player.tv",
                "sensor.washer_state",
                "switch.relay",
                "water_heater.tank",
            ],
        )

    def test_changes_file_and_unrecorded(self):
        d = D0
        start, end = midnight(d), midnight(d) + DAY
        hist = {"fan.attic": follows(d), "binary_sensor.motion": chatty(d, every=60)}
        f = changes_file(
            d.isoformat(),
            hist,
            ["fan.attic", "binary_sensor.motion", "media_player.tv"],
            start,
            end,
            CorrelationConfig(),
        )
        self.assertEqual(len(f["entities"]["fan.attic"]), 4)
        self.assertEqual(set(f["chatty"]), {"binary_sensor.motion"})
        self.assertEqual(f["missing"], ["media_player.tv"])
        purged = changes_file("2027-01-02", {}, ["fan.attic"], start + DAY, end + DAY, CorrelationConfig())
        files = {d.isoformat(): f, "2027-01-02": purged}
        per_day = day_changes_of(files)
        self.assertEqual(list(per_day), [d.isoformat()])  # a purged day is not compared
        self.assertIsNone(per_day[d.isoformat()]["binary_sensor.motion"])
        self.assertEqual(
            unrecorded(files, ["fan.attic", "binary_sensor.motion", "media_player.tv"]),
            {"entities": 1, "domains": ["media_player"]},
        )
        self.assertEqual(unrecorded({"2027-01-02": purged}, ["fan.attic"]), {"entities": 0, "domains": []})

    def test_chunks(self):
        ids = [f"light.lamp_{i:03d}" for i in range(300)]
        parts = chunks(ids + ids[:5], 400)
        self.assertEqual(sum(parts, []), ids)
        self.assertTrue(all(len(",".join(p)) <= 400 for p in parts))
        self.assertEqual(chunks(["a" * 50], 10), [["a" * 50]])

    def test_listing(self):
        stored = {
            "computed_at": 5.0,
            "days": 6,
            "candidates": 3,
            "min_score": 0.8,
            "unrecorded": {"entities": 1, "domains": ["media_player"]},
            "signatures": {
                "a": [{"entity_id": "fan.attic", "score": 1.0}, {"entity_id": "switch.relay", "score": 0.9}]
            },
        }
        listing = {"signatures": [{"id": "a"}, {"id": "b"}]}
        got = annotate_listing(listing, stored, {"fan.attic"})
        self.assertEqual(got["signatures"][0]["suggestions"], [{"entity_id": "switch.relay", "score": 0.9}])
        self.assertNotIn("suggestions", got["signatures"][1])
        self.assertEqual(got["suggest"]["unrecorded_domains"], ["media_player"])
        self.assertEqual(annotate_listing({"signatures": []}, None, ())["suggest"]["computed_at"], None)

    def test_setting(self):
        self.assertEqual(validate_settings({"review_suggest_score": 90}), {"review_suggest_score": 90})
        for bad in (40, 101, 85.5):
            with self.assertRaises(SettingsError):
                validate_settings({"review_suggest_score": bad})


class FanHA:
    """Home Assistant with a grid meter and a few candidate entities."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def snapshot(self):
        from test_addon import snapshot

        snap = snapshot()
        for e in ("fan.attic", "light.desk", "switch.relay", "media_player.tv"):
            snap["states"].append({"entity_id": e, "state": "off", "attributes": {"friendly_name": e}})
        return snap

    def history(self, entities, start, end):
        self.calls.append(list(entities))
        d = dt.datetime.fromtimestamp(start, dt.UTC).date()
        out = {"fan.attic": follows(d), "light.desk": unrelated(d), "switch.relay": [(start, "off")]}
        return {e: v for e, v in out.items() if e in entities}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReportStore(self._tmp.name)
        for day, s in fan_summaries().items():
            self.store.save(day, s)
        self.today = D0 + dt.timedelta(days=N_DAYS)
        self.ha = FanHA()
        self.analyzer = Analyzer(self.ha, self.store, {}, clock=lambda: midnight(self.today) + 5 * 3600)
        self.analyzer.tz = dt.UTC
        self.analyzer.maintain()
        self.sid = self.store.load_registry()["signatures"][0]["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_nightly_suggestions_and_review(self):
        ctx = self.analyzer.prepare()
        self.assertEqual(ctx.candidates, ["fan.attic", "light.desk", "media_player.tv", "switch.relay"])
        data = self.analyzer.suggest(ctx)
        self.assertEqual(len(self.ha.calls), N_DAYS)  # one request per day
        self.assertEqual(sorted(self.store.changes_days()), [d.isoformat() for d in days()])
        self.assertEqual(data["unrecorded"], {"entities": 1, "domains": ["media_player"]})
        self.assertEqual([c["entity_id"] for c in data["signatures"][self.sid]], ["fan.attic"])
        # a second run reuses the stored changes
        self.analyzer.suggest(self.analyzer.prepare())
        self.assertEqual(len(self.ha.calls), N_DAYS)
        # and so does a run without Home Assistant
        self.assertEqual(self.analyzer.suggest(None)["signatures"], data["signatures"])

        status, listing = route(self.analyzer, self.store, "GET", "/api/review")
        self.assertEqual(status, 200)
        sig = next(s for s in listing["signatures"] if s["id"] == self.sid)
        self.assertEqual(sig["suggestions"][0]["entity_id"], "fan.attic")
        self.assertEqual(sig["suggestions"][0]["score"], 1.0)
        self.assertEqual(listing["suggest"]["unrecorded_domains"], ["media_player"])

        # linked meanwhile: no longer suggested, nor a candidate
        self.analyzer.set_appliances(
            [{"key": "k", "name": "Fan", "sources": [{"type": "switched_load", "entity_id": "fan.attic"}]}]
        )
        status, listing = route(self.analyzer, self.store, "GET", "/api/review")
        sig = next(s for s in listing["signatures"] if s["id"] == self.sid)
        self.assertNotIn("suggestions", sig)
        self.assertNotIn("fan.attic", self.analyzer.prepare().candidates)

    def test_validated_signatures_are_not_compared(self):
        self.analyzer.decide(self.sid, "ignore")
        data = self.analyzer.suggest(self.analyzer.prepare())
        self.assertEqual(data["signatures"], {})

    def test_threshold_setting(self):
        self.analyzer.set_settings({"review_suggest_score": 95})
        self.assertEqual(self.analyzer.correlation_config.min_score, 0.95)

    def test_changes_are_pruned_with_the_window(self):
        self.analyzer.suggest(self.analyzer.prepare())
        self.store.save_changes("2026-10-01", {"entities": {}})
        removed = self.store.prune(self.today, 90, 200, 31)
        self.assertEqual(removed["changes"], ["2026-10-01"])
        self.assertEqual(len(self.store.changes_days()), N_DAYS)


if __name__ == "__main__":
    unittest.main()
