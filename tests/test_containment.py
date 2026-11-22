"""Tests of the containment scoring and the attribute candidates (#43).

A load cycling inside the long "heating" periods of a thermostat is not
found by the edge test (its steps fall far from the thermostat's changes)
but runs only while the thermostat heats: containment finds it. Entities
changing a few times a day without any link to the load, or "on" most of
the time, stay rejected. Synthetic data only: the entities are made up."""

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
from ha_client import parse_attribute_history  # noqa: E402
from nilm_core.correlation import (  # noqa: E402
    CorrelationConfig,
    accepted,
    binomial_tail,
    day_states,
    poisson_binomial_tail,
    score_containment,
    score_entity,
    segments,
    signature_edges,
    signature_uses,
    suggest,
    window_s,
)
from nilm_core.registry import update  # noqa: E402
from service import Analyzer  # noqa: E402
from storage import ReportStore  # noqa: E402
from suggest import (  # noqa: E402
    attribute_keys,
    candidates,
    changes_file,
    day_changes_of,
    day_initial_of,
    public_candidates,
    split_key,
)
from test_registry import D0, act, midnight, summary  # noqa: E402

DAY = 86400.0
N_DAYS = 20
THERMOSTAT = "climate.room::hvac_action"


def days(n=N_DAYS):
    return [D0 + dt.timedelta(days=i) for i in range(n)]


def heating_day(d: dt.date, outside: float = 0.0, fixed: bool = False):
    """One day of a radiator on its thermostat: five "heating" periods of 60
    to 110 minutes (the last one without the load), the ~900 W load cycling
    inside the first four (one cycling session each, starting and stopping
    minutes away from the thermostat's changes). The periods follow the
    regulation: each one starts at any time of its slot of 4.8 hours (at
    the same times every day, give or take 20 minutes, when ``fixed``: a
    daily schedule). ``outside``: the share of the sessions moved three
    hours later, outside any heating period. Returns (activations,
    hvac_action states)."""
    rnd = random.Random(d.toordinal())
    m = midnight(d)
    acts, states = [], [(m, "idle")]
    for j in range(5):
        h0 = m + (j * 4.8 + 0.5 + (0.0 if fixed else rnd.uniform(0, 2.2))) * 3600 + rnd.uniform(0, 1200)
        dur = rnd.uniform(60, 110) * 60
        states += [(h0, "heating"), (h0 + dur, "idle")]
        if j == 4:
            continue  # heats without the load (another reason, or the relay is off)
        a0, a1 = h0 + rnd.uniform(3, 10) * 60, h0 + dur - rnd.uniform(3, 10) * 60
        if rnd.random() < outside:
            a0, a1 = a0 + 3 * 3600 + 1800, a1 + 3 * 3600 + 1800
        acts.append(act(d, (a0 - m) / 3600, 900, a1 - a0, cycling=True, duty=0.4))
    return acts, states


def unrelated(d: dt.date, seed: int):
    """An entity changing four times a day, at random: two "on" periods of
    30 minutes to 3 hours."""
    rnd = random.Random(seed * 100_000 + d.toordinal())
    m = midnight(d)
    pts = [(m, "off")]
    for _ in range(2):
        t = m + rnd.uniform(0, 20) * 3600
        pts += [(t, "on"), (t + rnd.uniform(30, 180) * 60, "off")]
    return sorted(pts)


def mostly_on(d: dt.date):
    """An entity "on" except for two short breaks a day."""
    rnd = random.Random(-d.toordinal())
    m = midnight(d)
    pts = [(m, "on")]
    for k in range(2):
        t = m + (2 + 10 * k + rnd.uniform(0, 8)) * 3600
        pts += [(t, "off"), (t + rnd.uniform(20, 60) * 60, "on")]
    return pts


def scenario(outside: float = 0.0, entities=None, fixed: bool = False):
    """(summaries, registry, sid, changes, initial) over ``N_DAYS`` days."""
    sums, series = {}, {}
    for d in days():
        acts, states = heating_day(d, outside, fixed)
        s = summary(d, acts)
        s["quality"] = {"samples": 2880}  # 30 s sampling
        sums[d.isoformat()] = s
        per = {THERMOSTAT: states}
        for e, fn in (entities or {}).items():
            per[e] = fn(d)
        series[d.isoformat()] = per
    reg = update(None, sums, midnight(D0 + dt.timedelta(days=N_DAYS)))
    assert len(reg["signatures"]) == 1, reg["signatures"]
    sid = reg["signatures"][0]["id"]
    changes, initial = {}, {}
    for day, per in series.items():
        m = midnight(dt.date.fromisoformat(day))
        changes[day], initial[day] = {}, {}
        for e, pts in per.items():
            first, ch, _ = day_states(pts, m, m + DAY, 300)
            changes[day][e], initial[day][e] = ch, first
    return sums, reg, sid, changes, initial


def containment_of(sums, reg, sid, changes, initial, entity):
    uses = signature_uses(sums, reg, [sid])[sid]
    segs = {
        d: segments(initial[d].get(entity), changes[d][entity], sums[d]["start"], sums[d]["end"])
        for d in changes
    }
    starts = {d: sums[d]["start"] for d in sums}
    return score_containment(uses, segs, CorrelationConfig(), day_starts=starts)


class ContainmentTests(unittest.TestCase):
    def test_a_load_cycling_inside_heating_periods(self):
        sums, reg, sid, changes, initial = scenario()
        # the edge test misses it: few steps near a change of the thermostat
        cfg = CorrelationConfig()
        edges = signature_edges(sums, reg, [sid], cfg)[sid]
        windows = {d: window_s(sums[d], cfg) for d in sums}
        edge = score_entity(edges, {d: c[THERMOSTAT] for d, c in changes.items()}, windows)
        self.assertTrue(edge is None or not accepted(edge, cfg))
        # containment finds it
        got = suggest(sums, reg, [sid], changes, cfg, initial)[sid]
        self.assertEqual(len(got), 1)
        c = got[0]
        self.assertEqual(
            (c["entity_id"], c["match"], c["on_states"]), (THERMOSTAT, "containment", ["heating"])
        )
        self.assertEqual(c["score"], c["contained"])
        self.assertGreaterEqual(c["contained"], 0.99)
        self.assertLess(c["time_share"], 0.4)
        self.assertEqual((c["matches"], c["sessions"], c["days"]), (4 * N_DAYS, 4 * N_DAYS, N_DAYS))
        self.assertAlmostEqual(c["used"], 0.8, places=2)  # one heating period a day without the load
        self.assertLess(c["chance"], 1e-30)

    def test_unrelated_entities_are_rejected(self):
        entities = {f"light.lamp_{k}": (lambda d, k=k: unrelated(d, k)) for k in range(30)}
        entities["binary_sensor.mostly_on"] = mostly_on
        sums, reg, sid, changes, initial = scenario(entities=entities)
        got = suggest(sums, reg, [sid], changes, CorrelationConfig(), initial)[sid]
        self.assertEqual([c["entity_id"] for c in got], [THERMOSTAT])
        # the best of 30 random entities is far from the chance threshold
        lights = [
            containment_of(sums, reg, sid, changes, initial, e) for e in entities if e.startswith("light.")
        ]
        self.assertGreater(min(c["chance"] for c in lights), 100 * CorrelationConfig().max_chance)
        self.assertLess(max(c["contained"] - c["time_share"] for c in lights), 0.1)
        # on most of the time: it holds most of the load by chance only
        mostly = containment_of(sums, reg, sid, changes, initial, "binary_sensor.mostly_on")
        self.assertGreater(mostly["time_share"], 0.85)
        self.assertGreater(mostly["chance"], 1e-4)

    def test_a_load_also_running_outside(self):
        sums, reg, sid, changes, initial = scenario(outside=0.4)
        self.assertEqual(suggest(sums, reg, [sid], changes, CorrelationConfig(), initial), {})
        c = containment_of(sums, reg, sid, changes, initial, THERMOSTAT)
        self.assertLess(c["contained"], 0.8)
        lenient = CorrelationConfig(min_score=0.5)
        self.assertEqual(suggest(sums, reg, [sid], changes, lenient, initial)[sid][0]["match"], "containment")

    def test_too_few_days(self):
        sums, reg, sid, changes, initial = scenario()
        two = {d: c for d, c in changes.items() if d <= "2027-01-02"}
        self.assertEqual(suggest(sums, reg, [sid], two, CorrelationConfig(), initial), {})

    def test_without_the_initial_states(self):
        # files written before 0.8.0: the time before the first change is unknown
        sums, reg, sid, changes, _ = scenario()
        got = suggest(sums, reg, [sid], changes, CorrelationConfig())[sid][0]
        self.assertEqual((got["entity_id"], got["match"]), (THERMOSTAT, "containment"))

    def test_edge_matches_come_first(self):
        sums, reg, sid, changes, initial = scenario()
        # a relay switching exactly with the load: an edge match
        for day in changes:
            m = midnight(dt.date.fromisoformat(day))
            pts = [(m, "off")]
            for a in sums[day]["activations"]:
                pts += [(a["start"] - 5, "on"), (a["end"] - 5, "off")]
            first, ch, _ = day_states(pts, m, m + DAY, 100)
            changes[day]["switch.relay"], initial[day]["switch.relay"] = ch, first
        got = suggest(sums, reg, [sid], changes, CorrelationConfig(), initial)[sid]
        self.assertEqual(
            [(c["entity_id"], c["match"]) for c in got],
            [("switch.relay", "edge"), (THERMOSTAT, "containment")],
        )

    def test_segments_and_tail(self):
        self.assertEqual(
            segments("off", [(10.0, "on"), (20.0, "off")], 0.0, 30.0),
            [(0.0, 10.0, "off"), (10.0, 20.0, "on"), (20.0, 30.0, "off")],
        )
        self.assertEqual(segments(None, [(10.0, "on")], 0.0, 30.0), [(10.0, 30.0, "on")])
        self.assertEqual(segments(None, [], 0.0, 30.0), [])
        self.assertAlmostEqual(binomial_tail(10, 10, 0.5), 0.5**10)
        self.assertAlmostEqual(binomial_tail(3, 2, 0.5), 0.5)
        self.assertEqual((binomial_tail(5, 0, 0.3), binomial_tail(5, 6, 0.3)), (1.0, 0.0))
        self.assertEqual(day_states([(0, "unavailable"), (5, "on")], 0, 100, 10), (None, [], 0))
        self.assertEqual(day_states([(0, "off"), (5, "on")], 0, 100, 10), ("off", [(5, "on")], 1))


def daytime_house(d: dt.date):
    """One winter day of a house whose loads and schedules follow the clock
    (#82). Signatures: a ~900 W radiator cycling inside its thermostat's
    "heating" periods (four a day, at times set by the regulation, between
    06:00 and 21:00), and a ~350 W load running a few hours in the
    afternoon, linked to nothing. Entities:

    - the radiator's thermostat (``hvac_action``) and its pilot-wire relay,
      ``off`` (allowing heat) from 10 minutes before to 10 minutes after
      each period with the load: both contain the radiator;
    - three thermostats of water radiators, ``heat`` on their comfort
      schedule (about 06:00 to 21:00, 60 % of the day) and their
      ``hvac_action`` ``heating`` most of it: they contain every daytime
      use, without any link;
    - a pool pump filtering from 10:00 to 19:00 every day: it contains the
      afternoon load by the clock only.

    Returns (activations, {entity: states})."""
    rnd = random.Random(7 * d.toordinal())
    m = midnight(d)
    acts = []
    heating = [(m, "idle")]
    relay = [(m, "on")]
    for j in range(4):
        h0 = m + (6 + j * 3.75 + rnd.uniform(0, 1.8)) * 3600
        dur = rnd.uniform(60, 100) * 60
        heating += [(h0, "heating"), (h0 + dur, "idle")]
        relay += [(h0 - 600, "off"), (h0 + dur + 600, "on")]
        a0, a1 = h0 + rnd.uniform(3, 10) * 60, h0 + dur - rnd.uniform(3, 10) * 60
        acts.append(act(d, (a0 - m) / 3600, 900, a1 - a0, cycling=True, duty=0.4))
    a0 = rnd.uniform(11, 15)
    acts.append(act(d, a0, 350, rnd.uniform(1.5, 3) * 3600))
    states = {RADIATOR: heating, RELAY: relay}
    for k in range(3):
        on0, on1 = m + (6 + rnd.uniform(-0.5, 0.2)) * 3600, m + (21 + rnd.uniform(-0.2, 0.5)) * 3600
        states[f"climate.water_{k}"] = [(m, "off"), (on0, "heat"), (on1, "off")]
        pts = [(m, "idle")]
        t = on0
        while t < on1:  # heating, with short idle breaks
            t1 = min(t + rnd.uniform(1, 3) * 3600, on1)
            pts += [(t, "heating"), (t1, "idle")]
            t = t1 + rnd.uniform(5, 20) * 60
        states[f"climate.water_{k}::hvac_action"] = pts
    states[POOL] = [(m, "off"), (m + 10 * 3600, "on"), (m + 19 * 3600, "off")]
    return sorted(acts, key=lambda a: a["start"]), states


RADIATOR = "climate.bathroom::hvac_action"
RELAY = "switch.bathroom_pilot_wire"
POOL = "binary_sensor.pool_pump"


def daytime_scenario():
    """(summaries, registry, {power: sid}, changes, initial) over ``N_DAYS``
    days of ``daytime_house``."""
    sums, series = {}, {}
    for d in days():
        acts, states = daytime_house(d)
        s = summary(d, acts)
        s["quality"] = {"samples": 2880}
        sums[d.isoformat()] = s
        series[d.isoformat()] = states
    reg = update(None, sums, midnight(D0 + dt.timedelta(days=N_DAYS)))
    sids = {round(sig["power_w"], -2): sig["id"] for sig in reg["signatures"]}
    assert set(sids) == {900, 400}, reg["signatures"]
    changes, initial = {}, {}
    for day, per in series.items():
        m = midnight(dt.date.fromisoformat(day))
        changes[day], initial[day] = {}, {}
        for e, pts in per.items():
            first, ch, _ = day_states(pts, m, m + DAY, 300)
            changes[day][e], initial[day][e] = ch, first
    return sums, reg, sids, changes, initial


class SpecificityTests(unittest.TestCase):
    """#82: inclusion is not enough; the entity must be specific to the load."""

    @classmethod
    def setUpClass(cls):
        cls.sums, cls.reg, cls.sids, cls.changes, cls.initial = daytime_scenario()
        cls.found = suggest(
            cls.sums, cls.reg, cls.sids.values(), cls.changes, CorrelationConfig(), cls.initial
        )

    def of(self, power, entity):
        return containment_of(self.sums, self.reg, self.sids[power], self.changes, self.initial, entity)

    def test_the_radiator_is_found_tightest_first(self):
        got = self.found[self.sids[900]]
        # its thermostat's periods fit the load more tightly than the relay's
        # (allowing heat 10 minutes around them): ranked by lift
        self.assertEqual(
            [(c["entity_id"], c["match"]) for c in got], [(RADIATOR, "containment"), (RELAY, "containment")]
        )
        self.assertGreater(got[0]["lift"], got[1]["lift"])
        self.assertEqual((got[1]["on_states"], got[1]["inverted"]), (["off"], True))
        for c in got:
            self.assertGreaterEqual(c["contained"], 0.95)
            self.assertGreater(c["lift"], 2.5)
            self.assertLess(c["shifted_chance"], 1e-6)

    def test_thermostats_on_all_day_are_not_suggested(self):
        # every use of the radiator falls inside them, and the old test
        # (binomial on their time share) passed: their lift is too low
        for e in ("climate.water_0", "climate.water_1::hvac_action"):
            c = self.of(900, e)
            self.assertGreaterEqual(c["contained"], 0.9, e)
            self.assertGreater(c["time_share"], 0.5, e)
            self.assertLess(c["lift"], 2.0, e)
            self.assertLessEqual(c["chance"], 1e-4, e)
        names = {c["entity_id"] for found in self.found.values() for c in found}
        self.assertFalse({e for e in names if e.startswith("climate.water")})

    def test_a_daily_schedule_contains_by_the_clock_only(self):
        # the afternoon load always runs during the pool's filtration: lift
        # and binomial chance pass, the same hours of the other days do not
        c = self.of(400, POOL)
        self.assertGreaterEqual(c["contained"], 0.99)
        self.assertGreaterEqual(c["lift"], 2.0)
        self.assertLessEqual(c["chance"], 1e-4)
        self.assertGreater(c["baseline"], 0.9)
        self.assertGreater(c["shifted_chance"], 0.1)
        self.assertNotIn(self.sids[400], self.found)

    def test_a_fixed_heating_schedule_cannot_be_told_from_a_coincidence(self):
        # the known limit: periods at the same times every day
        sums, reg, sid, changes, initial = scenario(fixed=True)
        self.assertEqual(suggest(sums, reg, [sid], changes, CorrelationConfig(), initial), {})
        c = containment_of(sums, reg, sid, changes, initial, THERMOSTAT)
        self.assertGreaterEqual(c["contained"], 0.99)
        self.assertGreater(c["shifted_chance"], 1e-4)

    def test_poisson_binomial_tail(self):
        self.assertAlmostEqual(poisson_binomial_tail([0.5] * 10, 10), 0.5**10)
        self.assertAlmostEqual(poisson_binomial_tail([0.3] * 8, 5), binomial_tail(8, 5, 0.3))
        self.assertAlmostEqual(poisson_binomial_tail([1.0, 0.0, 0.5], 2), 0.5)
        self.assertEqual((poisson_binomial_tail([0.2], 0), poisson_binomial_tail([0.2], 2)), (1.0, 0.0))


class AttributeTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(
            attribute_keys(["climate.a", "light.b", "humidifier.c"]),
            ["climate.a::hvac_action", "humidifier.c::action"],
        )
        self.assertEqual(split_key(THERMOSTAT), ("climate.room", "hvac_action"))
        self.assertEqual(split_key("light.b"), ("light.b", None))
        got = public_candidates({"s": [{"entity_id": THERMOSTAT, "score": 1.0}, {"entity_id": "light.b"}]})
        self.assertEqual(got["s"][0], {"entity_id": "climate.room", "attribute": "hvac_action", "score": 1.0})
        self.assertEqual(got["s"][1], {"entity_id": "light.b"})

    def test_attribute_candidates(self):
        states = [
            {"entity_id": e, "attributes": {}} for e in ("climate.a", "humidifier.b", "light.c", "climate.d")
        ]
        registry = [{"entity_id": "climate.d", "entity_category": "diagnostic"}]
        self.assertEqual(candidates(states, registry, (), attributes=True), ["climate.a", "humidifier.b"])
        self.assertIn("humidifier.b", candidates(states, registry, ()))

    def test_parse_attribute_history(self):
        payload = [
            [
                {
                    "entity_id": "climate.a",
                    "state": "heat",
                    "attributes": {"hvac_action": "idle"},
                    "last_changed": "2027-01-01T00:00:00+00:00",
                    "last_updated": "2026-12-31T23:00:00+00:00",
                },
                {
                    "entity_id": "climate.a",
                    "state": "heat",
                    "attributes": {"hvac_action": "heating"},
                    "last_changed": "2027-01-01T00:00:00+00:00",
                    "last_updated": "2027-01-01T01:00:00+00:00",
                },
                {
                    "entity_id": "climate.a",
                    "state": "unavailable",
                    "attributes": {},
                    "last_updated": 1798768800.0,
                },
            ],
            [{"entity_id": "light.x", "state": "on", "last_updated": "2027-01-01T01:00:00+00:00"}],
        ]
        start = midnight(D0)
        got = parse_attribute_history(payload, {"climate.a": ("hvac_action",)}, window_start=start)
        self.assertEqual(
            got,
            {
                "climate.a::hvac_action": [
                    (start, "idle"),
                    (start + 3600, "heating"),
                    (1798768800.0, "unknown"),
                ]
            },
        )

    def test_changes_file_version_2(self):
        d = D0
        m = midnight(d)
        hist = {"light.a": [(m, "off"), (m + 60, "on")], "light.b": [(m + 30, "on")]}
        attrs = {
            THERMOSTAT: [(m, "idle")] + [(m + 100 * i, "heating" if i % 2 else "idle") for i in range(1, 250)]
        }
        f = changes_file(
            d.isoformat(), hist, ["light.a", "light.b", "light.c"], m, m + DAY, CorrelationConfig(), attrs
        )
        self.assertEqual(f["version"], 2)
        self.assertEqual(f["initial"], {"light.a": "off", THERMOSTAT: "idle"})
        self.assertEqual(len(f["entities"][THERMOSTAT]), 249)  # more than 100: kept for an attribute
        self.assertEqual(f["missing"], ["light.c"])
        self.assertEqual(day_initial_of({d.isoformat(): f})[d.isoformat()]["light.a"], "off")
        self.assertIn(THERMOSTAT, day_changes_of({d.isoformat(): f})[d.isoformat()])
        self.assertEqual(day_initial_of({"x": {"entities": {}}}), {"x": {}})


class ThermostatHA:
    """Home Assistant with a grid meter, a relay appliance and a thermostat
    whose hvac_action contains the uses of the signature."""

    def __init__(self, sums):
        self.sums = sums
        self.calls: list[list[str]] = []
        self.attribute_calls: list[dict] = []
        self.purged = False

    def snapshot(self):
        from test_addon import snapshot

        snap = snapshot()
        for e in ("climate.room", "switch.radiator", "light.desk"):
            snap["states"].append({"entity_id": e, "state": "heat", "attributes": {"friendly_name": e}})
        snap["registry"] += [
            {"entity_id": "climate.room", "device_id": "dev-radiator"},
            {"entity_id": "switch.radiator", "device_id": "dev-radiator"},
            {"entity_id": "light.desk", "device_id": "dev-lamp"},
        ]
        return snap

    def history(self, entities, start, end):
        self.calls.append(list(entities))
        if self.purged:
            return {}
        d = dt.datetime.fromtimestamp(start, dt.UTC).date()
        out = {"light.desk": unrelated(d, 1), "switch.radiator": [(start, "on")]}
        return {e: v for e, v in out.items() if e in entities}

    def history_attributes(self, wanted, start, end):
        self.attribute_calls.append(dict(wanted))
        if self.purged:
            return {}
        d = dt.datetime.fromtimestamp(start, dt.UTC).date()
        return {THERMOSTAT: heating_day(d)[1]} if "climate.room" in wanted else {}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReportStore(self._tmp.name)
        sums = {}
        for d in days():
            s = summary(d, heating_day(d)[0])
            s["quality"] = {"samples": 2880}
            sums[d.isoformat()] = s
            self.store.save(d.isoformat(), s)
        self.today = D0 + dt.timedelta(days=N_DAYS)
        self.ha = ThermostatHA(sums)
        self.analyzer = Analyzer(self.ha, self.store, {}, clock=lambda: midnight(self.today) + 5 * 3600)
        self.analyzer.tz = dt.UTC
        self.analyzer.maintain()
        self.sid = self.store.load_registry()["signatures"][0]["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_attribute_suggestion_and_merge_target(self):
        ctx = self.analyzer.prepare()
        self.assertEqual(ctx.attribute_entities, ["climate.room"])
        self.assertIn("climate.room", ctx.candidates)
        self.assertEqual(ctx.devices["climate.room"], "dev-radiator")
        data = self.analyzer.suggest(ctx)
        self.assertEqual(len(self.ha.attribute_calls), N_DAYS)  # one request with attributes per day
        self.assertEqual(self.ha.attribute_calls[0], {"climate.room": ("hvac_action",)})
        self.assertEqual(data["attribute_candidates"], 1)
        found = data["signatures"][self.sid]
        self.assertEqual(
            [(c["entity_id"], c.get("attribute"), c["match"]) for c in found],
            [("climate.room", "hvac_action", "containment")],
        )
        # the relay of the radiator is an appliance: the suggestion names it
        self.analyzer.set_appliances(
            [
                {
                    "key": "load:switch.radiator",
                    "name": "Radiator",
                    "sources": [{"type": "switched_load", "entity_id": "switch.radiator"}],
                }
            ]
        )
        status, listing = route(self.analyzer, self.store, "GET", "/api/review")
        self.assertEqual(status, 200)
        sig = next(s for s in listing["signatures"] if s["id"] == self.sid)
        self.assertEqual(
            sig["suggestions"][0]["appliance"],
            {"key": "load:switch.radiator", "name": "Radiator", "via": "device"},
        )
        self.assertEqual(listing["suggest"]["attribute_candidates"], 1)
        # the switched load of an appliance is a candidate now
        self.assertIn("switch.radiator", self.analyzer.prepare().candidates)
        # a run without Home Assistant gives the same, with the stored devices
        self.assertEqual(self.analyzer.suggest(None)["devices"]["climate.room"], "dev-radiator")

    def test_old_changes_files_are_read_again_once(self):
        ctx = self.analyzer.prepare()
        for d in days():
            self.store.save_changes(d.isoformat(), {"day": d.isoformat(), "entities": {"light.desk": []}})
        self.analyzer.suggest(ctx)
        self.assertEqual(len(self.ha.calls), N_DAYS)
        self.assertEqual(self.store.load_changes(D0.isoformat())["version"], 2)
        self.analyzer.suggest(ctx)
        self.assertEqual(len(self.ha.calls), N_DAYS)  # not again

    def test_a_purged_day_keeps_its_old_file(self):
        ctx = self.analyzer.prepare()
        old = {"day": D0.isoformat(), "entities": {"light.desk": [[1.0, "on"]]}, "chatty": {}}
        self.store.save_changes(D0.isoformat(), old)
        self.ha.purged = True
        self.analyzer.suggest(ctx)
        self.assertEqual(self.store.load_changes(D0.isoformat()), old)


if __name__ == "__main__":
    unittest.main()
