"""Tests of the Energy dashboard -> analysis configuration mapping.

Fixtures are hand-written and generic: they mimic the shape of the websocket
API answers, not any real installation.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))

from energy_config import _Index, derive, power_for, tracking_gaps  # noqa: E402


def sensor(entity_id, state="1", device_class=None, unit=None, **attrs):
    a = dict(attrs)
    if device_class:
        a["device_class"] = device_class
    if unit:
        a["unit_of_measurement"] = unit
    return {"entity_id": entity_id, "state": state, "attributes": a}


def power(entity_id, state="100", unit="W", **attrs):
    return sensor(entity_id, state, "power", unit, **attrs)


def energy(entity_id, state="12.5", **attrs):
    return sensor(entity_id, state, "energy", "kWh", **attrs)


def reg(entity_id, device_id=None, platform="test"):
    return {"entity_id": entity_id, "device_id": device_id, "platform": platform}


class Installation:
    """Builds states + registry + prefs for one test case."""

    def __init__(self):
        self.states, self.registry, self.sources, self.devices = [], [], [], []

    def add(self, state, device=None, platform="test"):
        self.states.append(state)
        self.registry.append(reg(state["entity_id"], device, platform))
        return self

    def prefs(self):
        return {"energy_sources": self.sources, "device_consumption": self.devices}


def meter_with_channels():
    """A two-channel meter: channel 1 on the grid, channel 2 unused."""
    h = Installation()
    h.add(energy("sensor.main_meter_channel_1_energy"), "meter")
    h.add(energy("sensor.main_meter_channel_1_energy_returned"), "meter")
    h.add(power("sensor.main_meter_channel_1_power"), "meter")
    h.add(sensor("sensor.main_meter_channel_1_power_factor", "0.9", "power_factor"), "meter")
    h.add(energy("sensor.main_meter_channel_2_energy", "0"), "meter2")
    h.add(power("sensor.main_meter_channel_2_power", "0"), "meter2")
    h.sources.append(
        {
            "type": "grid",
            "stat_energy_from": "sensor.main_meter_channel_1_energy",
            "stat_energy_to": "sensor.main_meter_channel_1_energy_returned",
        }
    )
    h.sources.append({"type": "grid", "stat_energy_from": "sensor.main_meter_channel_2_energy"})
    return h


class DeriveTests(unittest.TestCase):
    def test_no_energy_config(self):
        d = derive(None, [], [])
        self.assertIsNone(d.config)
        self.assertEqual(d.findings[0].kind, "no_grid")

    def test_manual_grid_without_energy_config(self):
        d = derive(None, [power("sensor.house_power")], [], {"grid_entity": "sensor.house_power"})
        self.assertEqual(d.config.grid_entity, "sensor.house_power")

    def test_grid_only_picks_the_right_channel_and_ignores_unused_one(self):
        h = meter_with_channels()
        d = derive(h.prefs(), h.states, h.registry)
        self.assertEqual(d.config.grid_entity, "sensor.main_meter_channel_1_power")
        self.assertIsNone(d.config.grid_export_entity)  # same signed sensor for both directions
        self.assertEqual(d.config.extra_grid_entities, [])
        self.assertIn(
            ("grid_ignored", "sensor.main_meter_channel_2_energy"),
            [(f.kind, f.entity_id) for f in d.findings],
        )

    def test_grid_and_solar(self):
        h = meter_with_channels()
        h.add(energy("sensor.inverter_total_production"), "inverter")
        h.add(power("sensor.inverter_power"), "inverter")
        h.add(power("sensor.inverter_pv1_power"), "inverter")
        h.sources.append({"type": "solar", "stat_energy_from": "sensor.inverter_total_production"})
        d = derive(h.prefs(), h.states, h.registry)
        self.assertEqual(d.config.pv_entity, "sensor.inverter_power")

    def test_grid_solar_battery(self):
        h = meter_with_channels()
        h.add(energy("sensor.battery_discharge_energy"), "bat")
        h.add(energy("sensor.battery_charge_energy"), "bat")
        h.add(power("sensor.battery_power", "-500"), "bat")
        h.sources.append(
            {
                "type": "battery",
                "stat_energy_from": "sensor.battery_discharge_energy",
                "stat_energy_to": "sensor.battery_charge_energy",
            }
        )
        d = derive(h.prefs(), h.states, h.registry)
        self.assertEqual(d.config.battery_entity, "sensor.battery_power")
        self.assertIn("battery_sign", [f.kind for f in d.findings])

    def test_separate_import_export_meters_in_kw(self):
        h = Installation()
        h.add(energy("sensor.p1_energy_consumed"), "p1")
        h.add(energy("sensor.p1_energy_produced"), "p1")
        h.add(power("sensor.p1_power_consumed", "0.4", unit="kW"), "p1")
        h.add(power("sensor.p1_power_produced", "0.0", unit="kW"), "p1")
        h.sources.append(
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.p1_energy_consumed"}],
                "flow_to": [{"stat_energy_to": "sensor.p1_energy_produced"}],
            }
        )
        d = derive(h.prefs(), h.states, h.registry)
        self.assertEqual(d.config.grid_entity, "sensor.p1_power_consumed")
        self.assertEqual(d.config.grid_export_entity, "sensor.p1_power_produced")
        self.assertEqual(d.config.scales["sensor.p1_power_consumed"], 1000.0)

    def test_three_phases_are_summed(self):
        h = Installation()
        for ph in (1, 2, 3):
            h.add(energy(f"sensor.phase_{ph}_energy"), f"ph{ph}")
            h.add(power(f"sensor.phase_{ph}_power"), f"ph{ph}")
            h.sources.append({"type": "grid", "stat_energy_from": f"sensor.phase_{ph}_energy"})
        d = derive(h.prefs(), h.states, h.registry)
        self.assertEqual(d.config.grid_entity, "sensor.phase_1_power")
        self.assertEqual(d.config.extra_grid_entities, ["sensor.phase_2_power", "sensor.phase_3_power"])

    def test_devices_roles(self):
        h = meter_with_channels()
        # measured smart plug
        h.add(energy("sensor.fridge_plug_energy"), "plug")
        h.add(power("sensor.fridge_plug_power"), "plug")
        # water heater estimated from its relay (estimation integration)
        h.add(energy("sensor.water_heater_energy", source_entity="switch.water_heater"), "est", "powercalc")
        h.add(
            power("sensor.water_heater_power", "0.5", source_entity="switch.water_heater"), "est", "powercalc"
        )
        # light estimated from its light entity
        h.add(energy("sensor.kitchen_light_energy", source_entity="light.kitchen"), "est2", "powercalc")
        h.add(power("sensor.kitchen_light_power", "40", source_entity="light.kitchen"), "est2", "powercalc")
        # dead plug
        h.add(energy("sensor.freezer_energy", "unavailable"), "dead")
        # energy-only counter
        h.add(energy("sensor.heat_counter_energy"), None)
        # nested: the desk lamp is inside the office plug
        h.add(energy("sensor.office_plug_energy"), "office")
        h.add(power("sensor.office_plug_power"), "office")
        h.add(energy("sensor.desk_lamp_energy"), "lamp")
        h.add(power("sensor.desk_lamp_power"), "lamp")
        h.devices = [
            {"stat_consumption": "sensor.fridge_plug_energy", "name": "Fridge"},
            {"stat_consumption": "sensor.water_heater_energy", "name": "Water heater"},
            {"stat_consumption": "sensor.kitchen_light_energy"},
            {"stat_consumption": "sensor.freezer_energy", "name": "Freezer"},
            {"stat_consumption": "sensor.heat_counter_energy", "name": "Heat"},
            {"stat_consumption": "sensor.office_plug_energy", "name": "Office"},
            {
                "stat_consumption": "sensor.desk_lamp_energy",
                "name": "Desk lamp",
                "included_in_stat": "sensor.office_plug_energy",
            },
        ]
        d = derive(h.prefs(), h.states, h.registry)
        roles = {t.name: t.role for t in d.tracked}
        self.assertEqual(roles["Fridge"], "submeter")
        self.assertEqual(roles["Water heater"], "switched")
        self.assertEqual(roles["Freezer"], "unavailable")
        self.assertEqual(roles["Heat"], "energy_only")
        self.assertEqual(roles["Office"], "submeter")
        self.assertEqual(roles["Desk lamp"], "nested")
        light = [t for t in d.tracked if t.energy_entity == "sensor.kitchen_light_energy"][0]
        self.assertEqual(light.role, "estimated")
        subs = {s.entity_id for s in d.config.submeters}
        self.assertEqual(
            subs, {"sensor.fridge_plug_power", "sensor.kitchen_light_power", "sensor.office_plug_power"}
        )
        self.assertEqual([s.entity_id for s in d.config.switched_loads], ["switch.water_heater"])

    def test_manual_options_win(self):
        h = meter_with_channels()
        d = derive(
            h.prefs(),
            h.states,
            h.registry,
            {
                "grid_entity": "sensor.other_meter_power",
                "grid_invert": True,
                "submeters": ["sensor.pump_power"],
                "switched_loads": [
                    {"entity_id": "switch.radiator", "name": "Radiator", "on_states": ["off"]}
                ],
                "step_threshold_w": 200,
            },
        )
        c = d.config
        self.assertEqual(c.grid_entity, "sensor.other_meter_power")
        self.assertTrue(c.grid_invert)
        self.assertEqual([s.entity_id for s in c.submeters], ["sensor.pump_power"])
        self.assertEqual(c.switched_loads[0].on_states, ("off",))
        self.assertEqual(c.step_threshold_w, 200.0)

    def test_power_for_ignores_power_factor(self):
        h = meter_with_channels()
        idx = _Index(h.states, h.registry)
        self.assertEqual(
            power_for("sensor.main_meter_channel_1_energy", idx), "sensor.main_meter_channel_1_power"
        )


class GapTests(unittest.TestCase):
    def test_underestimated_switched_device(self):
        h = meter_with_channels()
        h.add(energy("sensor.water_heater_energy", source_entity="switch.water_heater"), "est", "powercalc")
        h.devices = [{"stat_consumption": "sensor.water_heater_energy", "name": "Water heater"}]
        d = derive(h.prefs(), h.states, h.registry)
        report = {"start": 0, "end": 86400, "energy_kwh": {"known": {"Water heater": 4.6}}}
        hist = {"sensor.water_heater_energy": [(10, "6.200"), (40000, "6.205"), (86000, "6.210")]}
        gaps = tracking_gaps(report, d.tracked, hist)
        self.assertEqual(len(gaps), 1)
        self.assertIn("measured 4.60 kWh", gaps[0].detail)

    def test_consistent_device_is_not_reported(self):
        h = meter_with_channels()
        h.add(energy("sensor.water_heater_energy", source_entity="switch.water_heater"), "est", "powercalc")
        h.devices = [{"stat_consumption": "sensor.water_heater_energy", "name": "Water heater"}]
        d = derive(h.prefs(), h.states, h.registry)
        report = {"start": 0, "end": 86400, "energy_kwh": {"known": {"Water heater": 4.6}}}
        hist = {"sensor.water_heater_energy": [(10, "6.0"), (86000, "10.4")]}
        self.assertEqual(tracking_gaps(report, d.tracked, hist), [])


if __name__ == "__main__":
    unittest.main()
