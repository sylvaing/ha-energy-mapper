"""A meter of the house itself (#97): the house value comes from it, and
meters polled at different instants (grid, battery, solar) make no false
step any more."""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))

from energy_config import derive  # noqa: E402
from nilm_core import AnalysisConfig, analyze  # noqa: E402

DAY0 = 1_700_000_000.0  # any midnight will do: the analysis covers [DAY0, DAY0 + 1 day)
HOUR = 3600.0
HOUSE, GRID, PV, BAT = "sensor.house_power", "sensor.grid_power", "sensor.pv_power", "sensor.battery_power"


def readings(t0, t1, dt, fn, offset=0.0):
    out, t = [], t0 + offset
    while t < t1:
        out.append((t, f"{fn(t):.1f}"))
        t += dt
    return out


def battery_swap_day(lag_s=40.0):
    """A night when the battery takes over the house at 22:00, while the
    house draws a steady 600 W (then 2.6 kW, a kettle-sized load, from
    23:00 to 23:10). The battery reports the change at once, the grid
    ``lag_s`` later: grid + battery overshoots by the battery's power
    meanwhile. The house meter sees only the house."""
    t0, t1 = DAY0 - 3 * HOUR, DAY0 + 24 * HOUR
    swap = DAY0 + 22 * HOUR
    load_on, load_off = DAY0 + 23 * HOUR, DAY0 + 23 * HOUR + 600

    def house(t):
        return 600.0 + (2000.0 if load_on <= t < load_off else 0.0)

    def battery(t):  # + discharge: 3 kW feeds the house and exports the rest
        return 3000.0 if t >= swap else 0.0

    def grid(t):  # the grid meter is late by lag_s
        return house(t) - (3000.0 if t >= swap + lag_s else 0.0)

    return {
        HOUSE: readings(t0, t1, 5, house, offset=1),
        GRID: readings(t0, t1, 5, grid, offset=2),
        BAT: readings(t0, t1, 5, battery, offset=3),
        PV: readings(t0, t1, 30, lambda t: 0.0, offset=4),
    }


def uses_at(report):
    """The minutes of the day the uses start at (readings come a few seconds after the hour)."""
    return sorted(int((a["start"] - DAY0) // 60) for a in report["activations"])


class HouseMeterTest(unittest.TestCase):
    def test_battery_swap_makes_no_step_with_a_house_meter(self):
        hist = battery_swap_day()
        base = AnalysisConfig(grid_entity=GRID, pv_entity=PV, battery_entity=BAT)
        without = analyze(hist, base, DAY0, DAY0 + 24 * HOUR)
        # grid + battery: the swap is a 3 kW use of 40 s
        self.assertIn(22 * 60, uses_at(without))

        cfg = AnalysisConfig(grid_entity=GRID, pv_entity=PV, battery_entity=BAT, house_entity=HOUSE)
        report = analyze(hist, cfg, DAY0, DAY0 + 24 * HOUR)
        # only the real load is left
        self.assertEqual(uses_at(report), [23 * 60])
        self.assertEqual(round(report["activations"][0]["power_w"] / 100), 20)
        # the house is the meter's, its peak the real load
        self.assertEqual(report["max_house_w"], 2600)

    def test_totals_still_read_grid_solar_battery(self):
        hist = battery_swap_day()
        cfg = AnalysisConfig(grid_entity=GRID, pv_entity=PV, battery_entity=BAT, house_entity=HOUSE)
        report = analyze(hist, cfg, DAY0, DAY0 + 24 * HOUR)
        energy = report["energy_kwh"]
        self.assertAlmostEqual(energy["house"], 0.6 * 24 + 2.0 / 6, delta=0.05)
        # 2 h of export at 2.4 kW (the kettle's 10 min less), read from the grid meter
        self.assertGreater(energy["grid_export"], 4.0)

    def test_no_solar_doubt_with_a_house_meter(self):
        """A late solar reading makes no step in the house meter: nothing is
        set aside (#86 only applies to grid + solar)."""
        hist = battery_swap_day()
        cfg = AnalysisConfig(grid_entity=GRID, pv_entity=PV, battery_entity=BAT, house_entity=HOUSE)
        report = analyze(hist, cfg, DAY0, DAY0 + 24 * HOUR)
        self.assertEqual(report["quality"]["solar_held"], 0)


def _state(entity_id, value="500"):
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {"device_class": "power", "unit_of_measurement": "W"},
    }


class DeriveHouseTest(unittest.TestCase):
    def setUp(self):
        self.states = [_state(GRID), _state(HOUSE)]
        self.registry = [
            {"entity_id": e["entity_id"], "device_id": None, "platform": "test"} for e in self.states
        ]

    def test_house_entity_option(self):
        d = derive(None, self.states, self.registry, {"grid_entity": GRID, "house_entity": HOUSE})
        self.assertEqual(d.config.house_entity, HOUSE)
        self.assertEqual(d.to_dict()["house_entity"], HOUSE)

    def test_missing_house_entity_falls_back(self):
        d = derive(None, self.states, self.registry, {"grid_entity": GRID, "house_entity": "sensor.gone"})
        self.assertIsNone(d.config.house_entity)
        self.assertIn(("unavailable", "sensor.gone"), [(f.kind, f.entity_id) for f in d.findings])

    def test_house_entity_in_kw(self):
        states = [
            *self.states,
            {
                **_state("sensor.house_kw", "0.5"),
                "attributes": {"device_class": "power", "unit_of_measurement": "kW"},
            },
        ]
        d = derive(None, states, self.registry, {"grid_entity": GRID, "house_entity": "sensor.house_kw"})
        self.assertEqual(d.config.scales.get("sensor.house_kw"), 1000.0)


if __name__ == "__main__":
    unittest.main()
