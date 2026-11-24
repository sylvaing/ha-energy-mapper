"""The live view's core (#17): samples, steps, events and candidates one
reading at a time, and their equivalence with the nightly analysis."""

from __future__ import annotations

import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import synth_cycles  # noqa: E402
from nilm_core import (  # noqa: E402
    AnalysisConfig,
    SubmeterConfig,
    SwitchedLoadConfig,
    build_samples,
    detect_steps,
    find_cycling_sessions,
    pair_steps,
    parse_numeric,
)
from nilm_core.live import (  # noqa: E402
    CandidateRanker,
    LiveDetector,
    LiveEvent,
    LivePairer,
    SwitchChange,
    confidence,
    night_interval,
    replay,
)
from nilm_core.registry import Matcher  # noqa: E402
from nilm_core.report import find_steps  # noqa: E402
from nilm_core.signal import SampleBuilder, scaled, sum_series  # noqa: E402
from nilm_core.steps import CANCELLED, CONFIRMED, PROVISIONAL, LiveStep, StepStream  # noqa: E402
from synth import DAY0, HOUR, Cycling, House, Steady, solar_drift_day, switch_history  # noqa: E402

UTC_HOUR = lambda ts: int((ts - DAY0) // HOUR) % 24  # noqa: E731
GRID = "sensor.grid_power"
PV = "sensor.pv_power"


def night_samples(hist, cfg):
    """The samples of the night, as report.analyze builds them."""

    def series(entity, invert=False):
        if not entity:
            return None
        return scaled(parse_numeric(hist.get(entity, [])), cfg.scales.get(entity, 1.0), invert)

    return build_samples(
        grid=sum_series(
            [series(cfg.grid_entity, cfg.grid_invert)]
            + [series(e, cfg.grid_invert) for e in cfg.extra_grid_entities]
        ),
        pv=sum_series([series(cfg.pv_entity)] + [series(e) for e in cfg.extra_pv_entities])
        if cfg.pv_entity
        else None,
        submeters=[series(s.entity_id) for s in cfg.submeters],
        estimated=[s.estimated for s in cfg.submeters],
        grid_export=series(cfg.grid_export_entity),
        battery=series(cfg.battery_entity, cfg.battery_invert),
        house=series(cfg.house_entity),
    )


def night_steps(hist, cfg):
    """The steps of the night, as report.analyze finds them (with the night
    threshold when it applies)."""
    return find_steps(night_samples(hist, cfg), cfg)


def stream_samples(hist, cfg):
    """The samples of a SampleBuilder fed reading by reading in time order."""
    b = SampleBuilder.from_config(cfg)
    readings = sorted(
        ((float(ts), state, e) for e in b.entities for ts, state in hist.get(e, [])), key=lambda r: r[0]
    )
    out, prev = [], None
    for ts, state, e in readings:
        if prev is not None and ts > prev:
            out += b.pop(ts)
        b.push(e, ts, state)
        prev = ts
    return out + b.flush()


def key(steps):
    return [(s.ts, round(s.delta, 6), round(s.before, 6), round(s.after, 6)) for s in steps]


def jitter(hist, seed=3, sigma=1.5):
    """Readings that do not arrive on a regular clock."""
    rnd = random.Random(seed)
    return {e: sorted((ts + abs(rnd.gauss(0, sigma)), v) for ts, v in rows) for e, rows in hist.items()}


def with_gap(hist, start, end):
    """The recorder lost every reading between start and end."""
    return {e: [(ts, v) for ts, v in rows if not start <= ts < end] for e, rows in hist.items()}


def flat(points, entity=GRID):
    """A hand-made history: ``points`` are (ts, watts)."""
    return {entity: [(t, f"{w:.1f}") for t, w in points]}


def level(t0, t1, dt, fn, noise=0.0, seed=1):
    rnd = random.Random(seed)
    out, t = [], t0
    while t < t1:
        out.append((t, fn(t) + (rnd.gauss(0, noise) if noise else 0.0)))
        t += dt
    return out


# ---------------------------------------------------------------------------
class SampleBuilderTest(unittest.TestCase):
    def assertSameSamples(self, hist, cfg):
        night = night_samples(hist, cfg)
        live = stream_samples(hist, cfg)
        self.assertEqual(len(night), len(live))
        self.assertEqual(night, live)

    def test_grid_and_pv_with_dropouts(self):
        house = House(base_w=250, loads=[Steady(DAY0 + 9 * HOUR, DAY0 + 10 * HOUR, 2000)], pv_peak_w=3000)
        hist = house.histories(
            DAY0, DAY0 + 24 * HOUR, dt=10, pv_dropouts=((DAY0 + 12 * HOUR, DAY0 + 12.2 * HOUR),)
        )
        self.assertSameSamples(hist, AnalysisConfig(grid_entity=GRID, pv_entity=PV))

    def test_split_import_export_in_kw(self):
        house = House(loads=[Steady(DAY0 + 13 * HOUR, DAY0 + 14 * HOUR, 1500)], pv_peak_w=4000)
        hist = house.histories(DAY0, DAY0 + 24 * HOUR, dt=10, split_import_export=True)
        cfg = AnalysisConfig(grid_entity=GRID, grid_export_entity=GRID + "_export", pv_entity=PV)
        self.assertSameSamples(hist, cfg)
        hist_kw = house.histories(DAY0, DAY0 + 24 * HOUR, dt=10, kw=True)
        cfg_kw = AnalysisConfig(grid_entity=GRID, pv_entity=PV, scales={GRID: 1000.0})
        self.assertSameSamples(hist_kw, cfg_kw)

    def test_several_meters_battery_submeters(self):
        # two phases reported at different times, two inverters, a battery
        # (inverted sign), a measured and an estimated sub-meter that drop out
        rnd = random.Random(5)
        t0, t1 = DAY0, DAY0 + 6 * HOUR
        ph1 = [(t, f"{200 + rnd.gauss(0, 10):.1f}") for t, _ in level(t0, t1, 10, lambda t: 0)]
        ph2 = [(t + 3, f"{(900 if (t // 600) % 2 else 50) + rnd.gauss(0, 10):.1f}") for t, _ in ph1]
        inv1 = [(t + 1, "unavailable" if 2 * HOUR < t - t0 < 2.1 * HOUR else "800") for t, _ in ph1[::3]]
        inv2 = [(t + 2, "400") for t, _ in ph1[::5]]
        bat = [(t + 4, "-300" if (t // 1800) % 2 else "200") for t, _ in ph1[::6]]
        plug = [(t + 5, "unavailable" if 4 * HOUR < t - t0 < 4.2 * HOUR else "120") for t, _ in ph1[::2]]
        est = [(t + 6, "60") for t, _ in ph1[::30]]
        hist = {
            "sensor.l1": ph1,
            "sensor.l2": ph2,
            "sensor.inv1": inv1,
            "sensor.inv2": inv2,
            "sensor.bat": bat,
            "sensor.plug": plug,
            "sensor.est": est,
        }
        cfg = AnalysisConfig(
            grid_entity="sensor.l1",
            extra_grid_entities=["sensor.l2"],
            pv_entity="sensor.inv1",
            extra_pv_entities=["sensor.inv2"],
            battery_entity="sensor.bat",
            battery_invert=True,
            submeters=[SubmeterConfig("sensor.plug", "Plug"), SubmeterConfig("sensor.est", "Est", True)],
        )
        self.assertSameSamples(hist, cfg)

    def test_house_meter(self):
        """With a house meter (#97), the live samples are the night's too:
        its readings drive the timeline, the other meters are held."""
        rnd = random.Random(7)
        t0, t1 = DAY0, DAY0 + 6 * HOUR
        house = [
            (t, f"{(2500 if (t // 900) % 2 else 400) + rnd.gauss(0, 10):.1f}")
            for t, _ in level(t0, t1, 7, lambda t: 0)
        ]
        grid = [(t + 3, f"{rnd.gauss(100, 50):.1f}") for t, _ in level(t0, t1, 10, lambda t: 0)]
        export = [(t + 4, "unavailable" if HOUR < t - t0 < 1.1 * HOUR else "30") for t, _ in grid[::4]]
        pv = [(t + 1, "800") for t, _ in grid[::3]]
        bat = [(t + 2, "-300" if (t // 1800) % 2 else "200") for t, _ in grid[::6]]
        plug = [(t + 5, "120") for t, _ in grid[::2]]
        hist = {
            "sensor.house": house,
            GRID: grid,
            GRID + "_export": export,
            PV: pv,
            "sensor.bat": bat,
            "sensor.plug": plug,
        }
        cfg = AnalysisConfig(
            grid_entity=GRID,
            grid_export_entity=GRID + "_export",
            pv_entity=PV,
            battery_entity="sensor.bat",
            house_entity="sensor.house",
            submeters=[SubmeterConfig("sensor.plug", "Plug")],
        )
        self.assertSameSamples(hist, cfg)
        samples = night_samples(hist, cfg)
        self.assertEqual([s.ts for s in samples], [t for t, _ in house])
        self.assertEqual([s.house for s in samples], [float(v) for _, v in house])

    def test_prunes_old_readings(self):
        b = SampleBuilder(grid=[GRID], pv=[PV])
        b.push(PV, DAY0, "1000")
        b.push(PV, DAY0 + 10, "unavailable")
        for i in range(3000):
            b.push(GRID, DAY0 + i * 10, "500")
            b.pop(DAY0 + i * 10)
        self.assertLess(len(b.grid.out.points), 200)
        # the last valid PV value is kept for the hold rule (and now too old)
        self.assertEqual(b.pv.out.last_valid_before(DAY0 + 30000), (DAY0, 1000.0))
        self.assertIsNone(b.flush()[-1].pv)


# ---------------------------------------------------------------------------
class EquivalenceTest(unittest.TestCase):
    """Replayed as a stream, a day gives exactly the steps of the night."""

    def assertEquivalent(self, hist, cfg, **kwargs):
        night = night_steps(hist, cfg)
        live = replay(hist, cfg, local_hour=UTC_HOUR, **kwargs)
        self.assertGreater(len(night), 0)
        self.assertEqual(key(night), key(live.steps))
        return live

    def test_synthetic_days(self):
        # noise, overlapping loads, multi-phase appliances, motor bursts
        for index in range(4):
            with self.subTest(day=index):
                house, day0, _ = synth_cycles.make_day(index)
                hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10)
                self.assertEquivalent(hist, AnalysisConfig(grid_entity=GRID))

    def test_solar_with_dropouts(self):
        for index in (1, 2):
            with self.subTest(day=index):
                house, day0, _ = synth_cycles.make_day(index)
                house.pv_peak_w = 3500
                drop = ((day0 + 11 * HOUR, day0 + 11 * HOUR + 120), (day0 + 15 * HOUR, day0 + 15.5 * HOUR))
                hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10, pv_dropouts=drop)
                self.assertEquivalent(hist, AnalysisConfig(grid_entity=GRID, pv_entity=PV))

    def test_gaps_and_irregular_readings(self):
        house, day0, _ = synth_cycles.make_day(3)
        hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10)
        hist = with_gap(jitter(hist), day0 + 7 * HOUR, day0 + 7.5 * HOUR)
        hist = with_gap(hist, day0 + 13 * HOUR, day0 + 13 * HOUR + 45)
        self.assertEquivalent(hist, AnalysisConfig(grid_entity=GRID))

    def test_fast_meter_and_split_meters(self):
        loads = [
            Steady(DAY0 + 1 * HOUR, DAY0 + 1.1 * HOUR, 2000),
            Steady(DAY0 + 1.05 * HOUR, DAY0 + 2 * HOUR, 900),
            Cycling(DAY0 + 2.5 * HOUR, DAY0 + 2.8 * HOUR, 1400, 40, 30),
        ]
        house = House(base_w=300, loads=loads, pv_peak_w=0)
        hist = house.histories(DAY0, DAY0 + 3 * HOUR, dt=2)
        self.assertEquivalent(hist, AnalysisConfig(grid_entity=GRID))
        hist = house.histories(DAY0, DAY0 + 3 * HOUR, dt=10, split_import_export=True)
        self.assertEquivalent(hist, AnalysisConfig(grid_entity=GRID, grid_export_entity=GRID + "_export"))

    def test_estimated_interval_on_a_regular_meter(self):
        # live estimates the interval from the last readings: on a regular
        # meter it is the night's, so the steps are the same
        house, day0, _ = synth_cycles.make_day(0)
        hist = house.histories(day0, day0 + 24 * HOUR, dt=10)
        self.assertEquivalent(hist, AnalysisConfig(grid_entity=GRID), interval_s=None)

    def test_night_threshold(self):
        # #77: smaller steps where PV produces nothing, the same rule live
        night = AnalysisConfig(grid_entity=GRID, pv_entity=PV, night_step_threshold_w=150)
        variants = {
            "drift": solar_drift_day(),
            "irregular": with_gap(jitter(solar_drift_day(seed=11)), DAY0 + 19 * HOUR, DAY0 + 19.2 * HOUR),
        }
        for name, hist in variants.items():
            with self.subTest(day=name):
                live = self.assertEquivalent(hist, night)
                small = [s for s in live.steps if abs(s.delta) < night.step_threshold_w]
                self.assertEqual(len(small), 2)
        with self.subTest(day="estimated interval"):
            self.assertEquivalent(solar_drift_day(seed=5), night, interval_s=None)
        for index in (1, 2):
            with self.subTest(day=index):
                # noise, overlapping loads, multi-phase appliances, at night too
                house, day0, _ = synth_cycles.make_day(index)
                house.pv_peak_w = 3500
                hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10)
                self.assertEquivalent(hist, night)

    def test_night_interval(self):
        hist = flat(level(DAY0, DAY0 + 3600, 7, lambda t: 300))
        self.assertAlmostEqual(night_interval(hist, AnalysisConfig(grid_entity=GRID)), 7.0)


# ---------------------------------------------------------------------------
class ProvisionalTest(unittest.TestCase):
    def run_stream(self, points, **kwargs):
        s = StepStream(**kwargs)
        log = []  # (reading time, id, state, ts, delta)
        for t, v in points:
            for st in s.push(t, v):
                log.append((t, st.id, st.state, st.ts, st.delta, st.small))
        for st in s.flush():
            log.append((None, st.id, st.state, st.ts, st.delta, st.small))
        return s, log

    def test_provisional_then_confirmed(self):
        t_on = DAY0 + 600
        pts = level(DAY0, DAY0 + 1800, 10, lambda t: 2250 if t >= t_on else 250, noise=10)
        s, log = self.run_stream(pts, interval_s=10)
        self.assertEqual([x[2] for x in log], [PROVISIONAL, CONFIRMED])
        prov, conf = log
        self.assertEqual(prov[1], conf[1])  # the same step, confirmed
        self.assertLessEqual(prov[0] - t_on, 20)  # after 3 readings
        self.assertEqual(conf[3], t_on)
        self.assertAlmostEqual(conf[4], 2000, delta=40)
        # confirmed once the window after it and the merge delay are over
        self.assertGreaterEqual(conf[0] - t_on, s.window_s)
        self.assertLessEqual(conf[0] - t_on, s.window_s + s.merge_s + 10)
        self.assertEqual((s.confirmed, s.cancelled), (1, 0))

    def test_spike_is_cancelled(self):
        # a 2-second inrush on a 1 s meter: provisional, then cancelled
        t_on = DAY0 + 300
        pts = level(DAY0, DAY0 + 600, 1, lambda t: 2500 if t_on <= t < t_on + 2 else 250)
        s, log = self.run_stream(pts, interval_s=1)
        self.assertEqual([x[2] for x in log], [PROVISIONAL, CANCELLED])
        self.assertEqual(log[0][1], log[1][1])
        self.assertEqual(detect_steps(pts), [])

    def test_small_step(self):
        # 200 W: between 60 % and 100 % of the threshold, never confirmed
        pts = level(DAY0, DAY0 + 900, 10, lambda t: 450 if t >= DAY0 + 300 else 250)
        _, log = self.run_stream(pts, interval_s=10)
        self.assertEqual([x[2] for x in log], [PROVISIONAL, CANCELLED])
        self.assertTrue(log[0][5])

    def test_end_of_stream(self):
        # a step just before the end: confirmed by flush, as the night does
        pts = level(DAY0, DAY0 + 300, 10, lambda t: 1500 if t >= DAY0 + 280 else 250)
        _, log = self.run_stream(pts, interval_s=10)
        self.assertEqual(log[-1][0], None)  # given by flush
        self.assertEqual(log[-1][2:4], (CONFIRMED, DAY0 + 280))
        self.assertEqual([(DAY0 + 280, 1250.0)], [(s.ts, s.delta) for s in detect_steps(pts)])

    def test_old_reading_dropped(self):
        s = StepStream()
        s.push(DAY0 + 10, 100)
        self.assertEqual(s.push(DAY0 + 5, 900), [])
        self.assertEqual(s.dropped, 1)


# ---------------------------------------------------------------------------
class PairingTest(unittest.TestCase):
    def test_same_pairs_as_the_night(self):
        loads = [
            Steady(DAY0 + 1 * HOUR, DAY0 + 4 * HOUR, 3000),  # water heater
            Steady(DAY0 + 2 * HOUR, DAY0 + 2 * HOUR + 180, 2000),  # kettle while it runs
            Steady(DAY0 + 6 * HOUR, DAY0 + 6.6 * HOUR, 2600),  # oven
            Steady(DAY0 + 6.2 * HOUR, DAY0 + 6.25 * HOUR, 1200),  # microwave during it
            Steady(DAY0 + 9 * HOUR, DAY0 + 9 * HOUR + 400, 800),
        ]
        house = House(base_w=250, loads=loads)
        hist = house.histories(DAY0, DAY0 + 12 * HOUR, dt=10)
        cfg = AnalysisConfig(grid_entity=GRID)
        night = pair_steps(night_steps(hist, cfg))
        live = replay(hist, cfg, local_hour=UTC_HOUR)
        closed = [e for e in live.events if e.state == CONFIRMED and e.duration_s is not None]
        self.assertEqual(len(night), 5)
        self.assertEqual(
            [(a.start, a.end, a.power_w) for a in night],
            [(e.t_on, e.t_off, round(e.power_w, 1)) for e in sorted(closed, key=lambda e: e.t_on)],
        )
        self.assertFalse(any(e.running for e in live.events))

    def test_off_step_without_its_switch_on(self):
        # the view starts while something is already on
        pts = level(DAY0, DAY0 + 900, 10, lambda t: 1250 if t < DAY0 + 400 else 250)
        live = replay(flat(pts), AnalysisConfig(grid_entity=GRID), local_hour=UTC_HOUR)
        (ev,) = live.events
        self.assertLess(ev.delta_w, 0)
        self.assertEqual(ev.state, CONFIRMED)
        self.assertFalse(ev.running)

    def test_session(self):
        # a hob regulating: one line, a session of 9 cycles
        house = House(base_w=250, loads=[Cycling(DAY0 + 600, DAY0 + 600 + 9 * 70, 1400, 40, 30)])
        hist = house.histories(DAY0, DAY0 + 1800, dt=5)
        cfg = AnalysisConfig(grid_entity=GRID)
        live = replay(hist, cfg, local_hour=UTC_HOUR)
        events = [e for e in live.events if e.state == CONFIRMED]
        self.assertEqual(len(events), 1)
        info = events[0].session()
        night = find_cycling_sessions(night_steps(hist, cfg))
        self.assertEqual(len(night), 1)
        self.assertEqual(info["count"], night[0].cycles)
        self.assertTrue(info["confirmed"])
        self.assertAlmostEqual(info["on_s"], 40, delta=6)
        self.assertAlmostEqual(events[0].power_w, 1400, delta=60)
        self.assertEqual(events[0].to_dict()["session"]["count"], 9)

    def test_two_kettles_are_not_a_session(self):
        # each one is on for longer than the session gap
        loads = [Steady(DAY0 + 600, DAY0 + 780, 2000), Steady(DAY0 + 840, DAY0 + 1020, 2000)]
        hist = House(base_w=250, loads=loads).histories(DAY0, DAY0 + 1800, dt=10)
        live = replay(hist, AnalysisConfig(grid_entity=GRID), local_hour=UTC_HOUR)
        self.assertEqual(len([e for e in live.events if e.state == CONFIRMED]), 2)

    def test_provisional_event_cancelled(self):
        p = LivePairer()
        st = LiveStep(1, DAY0, 900, 250, 1150)
        (ev,) = p.feed(st)
        self.assertEqual((ev.state, ev.running), (PROVISIONAL, True))
        st.state = CANCELLED
        self.assertEqual(p.feed(st)[0].state, CANCELLED)
        p.prune(DAY0 + 1)
        self.assertEqual(p.events, {})

    def test_small_or_negative_provisional_opens_nothing(self):
        p = LivePairer()
        self.assertEqual(p.feed(LiveStep(1, DAY0, 200, 250, 450, small=True)), [])
        self.assertEqual(p.feed(LiveStep(2, DAY0, -900, 1150, 250)), [])


# ---------------------------------------------------------------------------
def sig(sid, kind, band, power, label=None, hours_at=None, per_week=0.0, **extra):
    hours = [0.0] * 24
    if hours_at is not None:
        hours[hours_at] = 5.0
    return {
        "id": sid,
        "kind": kind,
        "band": band,
        "power_w": power,
        "label": label,
        "hours": hours,
        "per_week": per_week,
        **extra,
    }


REGISTRY = {
    "power_tol": 0.12,
    "signatures": [
        sig("short-2000w", "steady", "short", 2000, "Kettle", hours_at=7, per_week=10, appliance="k1"),
        sig("short-2100w", "steady", "short", 2100),
        sig("long-3000w", "steady", "long", 3000, "Water heater", per_week=7),
        sig("medium-2600w", "steady", "medium", 2600, "Oven", per_week=2),
        sig("cyc-1400w", "cycling", "session", 1400, "Hob"),
        sig("short-1200w", "steady", "short", 1200, review={"state": "ignored"}),
    ],
}


def event(t_on, delta, **kw):
    return LiveEvent(id=1, t_on=t_on, delta_w=delta, state=CONFIRMED, steps=[(t_on, delta)], **kw)


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.r = CandidateRanker(REGISTRY, local_hour=UTC_HOUR)

    def ids(self, cands):
        return [c.get("signature_id") or c.get("entity_id") for c in cands]

    def test_matcher_near(self):
        m = Matcher(REGISTRY)
        near = m.near("steady", "short", 2040)
        self.assertEqual([s for s, _ in near], ["short-2000w", "short-2100w"])
        self.assertAlmostEqual(near[0][1], 40 / 240)
        for p in (1500, 1800, 2050, 2300, 2500):
            first = [s for s, d in m.near("steady", "short", p) if d <= 1]
            self.assertEqual(first[0] if first else None, m.match({"start": 0, "end": 60, "power_w": p}))
        self.assertEqual(m.near("steady", "short", 9000), [])

    def test_running_step_ranks_by_power_and_habit(self):
        ev = event(DAY0 + 7 * HOUR, 2010)
        cands = self.r.rank(ev, ev.t_on + 5)
        self.assertEqual(self.ids(cands)[:2], ["short-2000w", "short-2100w"])
        top = cands[0]
        self.assertEqual((top["label"], top["appliance"], top["confidence"]), ("Kettle", "k1", "likely"))
        self.assertGreater(top["score"], 0.95)
        self.assertTrue(top["reason"]["usual_hour"])
        self.assertLessEqual(len(cands), 3)
        self.assertTrue(all(0 <= c["score"] <= 1 for c in cands))

    def test_ignored_signature_never_proposed(self):
        cands = self.r.rank(event(DAY0, 1200), DAY0 + 5)
        self.assertNotIn("short-1200w", self.ids(cands))

    def test_duration_rules_out_bands(self):
        ev = event(DAY0, 2900)
        self.assertEqual(self.ids(self.r.rank(ev, DAY0 + 30))[0], "long-3000w")
        ev = event(DAY0, 2050)
        self.assertIn("short-2000w", self.ids(self.r.rank(ev, DAY0 + 60)))
        self.assertNotIn("short-2000w", self.ids(self.r.rank(ev, DAY0 + 11 * 60)))  # > 10 min
        # closed after 25 min: only the medium band (and multi-phase)
        ev = event(DAY0, 2550, t_off=DAY0 + 1500, off_w=2550)
        ev.steps.append((DAY0 + 1500, -2550))
        self.assertEqual(self.ids(self.r.rank(ev, DAY0 + 1500)), ["medium-2600w"])

    def test_session_matches_cycling(self):
        ev = event(DAY0, 1380)
        ev.steps += [(DAY0 + 40, -1400), (DAY0 + 70, 1420), (DAY0 + 110, -1390)]
        ev.t_off, ev.off_w = DAY0 + 110, 1390
        self.assertEqual(self.ids(self.r.rank(ev, DAY0 + 110)), ["cyc-1400w"])

    def test_unknown(self):
        self.assertEqual(self.r.rank(event(DAY0, 5200), DAY0 + 5), [])
        self.assertEqual(event(DAY0, 5200).to_dict()["confidence"], "unknown")
        levels = [confidence(x) for x in (0.9, 0.6, 0.45, 0.3, 0.1, None)]
        self.assertEqual(levels, ["likely"] * 2 + ["maybe"] * 2 + ["unknown"] * 2)

    def test_pv_moved_is_never_likely(self):
        ev = event(DAY0 + 7 * HOUR, 2000, pv_moved=True)
        cands = self.r.rank(ev, ev.t_on + 5)
        self.assertLess(max(c["score"] for c in cands), 0.6)
        self.assertTrue(cands[0]["reason"]["pv_moved"])

    def test_switch_that_just_changed(self):
        ev = event(DAY0, 2950)
        sw = [
            SwitchChange("switch.heater", DAY0 - 4, True, name="Water heater", power_w=3000),
            SwitchChange("switch.other", DAY0 - 300, True, name="Too early"),
            SwitchChange("switch.off", DAY0, False, name="Wrong way"),
        ]
        cands = self.r.rank(ev, DAY0 + 5, sw)
        self.assertEqual(self.ids(cands)[0], "switch.heater")
        self.assertEqual((cands[0]["score"], cands[0]["reason"]["seconds"]), (0.95, -4))
        self.assertNotIn("switch.other", self.ids(cands))
        self.assertNotIn("switch.off", self.ids(cands))
        far = self.r.rank(event(DAY0, 800), DAY0 + 5, [sw[0]])
        self.assertEqual(far[0]["score"], 0.5)

    def test_right_appliance_first(self):
        # the design's target on steady signatures: first in >= 80 % of the
        # uses, in the top 3 in >= 95 %
        rnd = random.Random(11)
        # name: (power, shortest and longest use in seconds)
        kinds = {
            "Kettle": (2000, 120, 240),
            "Oven": (2600, 1500, 3000),
            "Heater": (3000, 7200, 10800),
            "Microwave": (1200, 60, 300),
            "Iron": (1000, 600, 1200),
            "Toaster": (850, 120, 240),
        }
        registry = {
            "power_tol": 0.12,
            "signatures": [
                sig(n, "steady", "short" if hi <= 600 else "medium" if hi <= 3600 else "long", p, n)
                for n, (p, lo, hi) in kinds.items()
            ],
        }
        loads, truth, t = [], [], DAY0 + 600
        for _ in range(60):
            name = rnd.choice(list(kinds))
            p, lo, hi = kinds[name]
            d = rnd.uniform(lo, hi)
            power = p * rnd.uniform(0.95, 1.05)
            loads.append(Steady(t, t + d, power))
            truth.append((t, name))
            t += d + rnd.uniform(120, 900)
        hist = House(base_w=250, loads=loads, noise_w=15).histories(DAY0, t + 600, dt=10)
        live = replay(hist, AnalysisConfig(grid_entity=GRID), registry=registry, local_hour=UTC_HOUR)
        by_start = {round(e.t_on / 10): e for e in live.events if e.delta_w > 0}
        first = top3 = 0
        for t_on, name in truth:
            near = (round(t_on / 10) + d for d in (0, 1, -1))
            ev = next((by_start[k] for k in near if k in by_start), None)
            labels = [c["label"] for c in ev.candidates] if ev else []
            first += bool(labels) and labels[0] == name
            top3 += name in labels
        self.assertGreaterEqual(first / len(truth), 0.8)
        self.assertGreaterEqual(top3 / len(truth), 0.95)


# ---------------------------------------------------------------------------
class DetectorTest(unittest.TestCase):
    def test_switched_load_is_a_candidate(self):
        loads = [Steady(DAY0 + 600, DAY0 + 4000, 3000)]
        hist = House(base_w=250, loads=loads).histories(DAY0, DAY0 + 5000, dt=10)
        hist["switch.heater"] = [(DAY0, "off")] + switch_history([(DAY0 + 595, DAY0 + 4003)])
        cfg = AnalysisConfig(
            grid_entity=GRID, switched_loads=[SwitchedLoadConfig("switch.heater", "Water heater")]
        )
        live = replay(hist, cfg, registry=REGISTRY, local_hour=UTC_HOUR)
        (ev,) = [e for e in live.events if e.state == CONFIRMED]
        self.assertEqual(ev.candidates[0]["source"], "switch")
        self.assertEqual(ev.candidates[0]["label"], "Water heater")
        self.assertEqual(ev.duration_s, 3400)

    def test_pv_moved(self):
        # a cloud: PV drops 1500 W, the grid meter reports it 20 s later, so
        # the house load (grid + PV) shows a fake dip then a fake rise
        t_cloud = DAY0 + 600
        grid = level(DAY0, DAY0 + 1500, 10, lambda t: -1000 if t < t_cloud + 20 else 500)
        pv = level(DAY0 + 5, DAY0 + 1500, 10, lambda t: 1500 if t < t_cloud else 0)
        hist = {GRID: [(t, f"{w:.0f}") for t, w in grid], PV: [(t, f"{w:.0f}") for t, w in pv]}
        cfg = AnalysisConfig(grid_entity=GRID, pv_entity=PV)
        live = replay(hist, cfg, registry=REGISTRY, local_hour=UTC_HOUR)
        confirmed = [u for u in live.updates if u.state == CONFIRMED]
        self.assertTrue(confirmed)
        self.assertTrue(all(u.pv_moved for u in confirmed))
        self.assertTrue(all(e.pv_moved for e in live.events if e.state == CONFIRMED))

    def test_live_use(self):
        # pushed as live would: readings as they come, built 5 s behind
        cfg = AnalysisConfig(grid_entity=GRID)
        det = LiveDetector(cfg, registry=REGISTRY, local_hour=UTC_HOUR)
        self.assertEqual(det.entities, [GRID])
        t_on = DAY0 + 7 * HOUR + 300
        seen = []
        for t, w in level(DAY0 + 7 * HOUR, DAY0 + 7 * HOUR + 900, 10, lambda t: 2250 if t >= t_on else 250):
            det.push(GRID, t, f"{w:.0f}")
            u = det.advance(t + 10 - 5)
            seen += [(t, e.id, e.state, e.to_dict()["confidence"]) for e in u.events]
        self.assertEqual(seen[0][2:], (PROVISIONAL, "likely"))
        self.assertEqual(seen[-1][2], CONFIRMED)
        self.assertLessEqual(seen[0][0] - t_on, 30)
        (ev,) = det.events.values()
        self.assertTrue(ev.running)
        self.assertEqual(ev.candidates[0]["signature_id"], "short-2000w")
        # 11 minutes later, a kettle is ruled out
        changed = det.rerank(t_on + 11 * 60)
        self.assertEqual(changed, [ev])
        self.assertNotIn("short-2000w", [c["signature_id"] for c in ev.candidates])
        det.set_registry(None)
        self.assertEqual(det.rerank(t_on + 12 * 60)[0].candidates, [])
        d = ev.to_dict()
        self.assertEqual((d["delta_w"], d["running"], d["duration_s"]), (2000, True, None))
        self.assertIsNone(d["session"])

    def test_replay_speed(self):
        # a full day of a 10 s meter in well under a second or two
        import time

        house, day0, _ = synth_cycles.make_day(1)
        hist = house.histories(day0 - 3 * HOUR, day0 + 24 * HOUR, dt=10)
        t = time.perf_counter()
        live = replay(hist, AnalysisConfig(grid_entity=GRID), registry=REGISTRY, local_hour=UTC_HOUR)
        self.assertLess(time.perf_counter() - t, 10)
        self.assertEqual(live.samples, len(hist[GRID]))


if __name__ == "__main__":
    unittest.main()
