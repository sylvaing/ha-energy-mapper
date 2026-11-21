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
from statistic_ids import (  # noqa: E402
    KIND_LOAD,
    KIND_SIGNATURE,
    is_own,
    kind_of,
    known_statistics,
    slugify,
    statistic_id,
)


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
        estimated = {s.entity_id for s in d.config.submeters if s.estimated}
        self.assertEqual(estimated, {"sensor.kitchen_light_power"})
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


# Energy Mapper's own statistics in the Energy dashboard (issue #36)
HEATER = "energy_mapper:load_switch_water_heater_energy"
RADIATOR = "energy_mapper:load_switch_radiator_relay_energy"
KETTLE = "energy_mapper:signature_short_2000w_energy"


def learned_registry():
    """An add-on registry: two learned switched loads (the radiator's
    pilot wire heats while its relay is off) and a labelled signature."""
    return {
        "switched_loads": [
            {
                "entity_id": "switch.water_heater",
                "name": "Water heater",
                "power_w": 2000,
                "on_states": ["on"],
            },
            {
                "entity_id": "switch.radiator_relay",
                "name": "Radiator",
                "power_w": 1000,
                "inverted": True,
                "on_states": ["off"],
            },
        ],
        "signatures": [
            {"id": "short-2000w", "label": "Kettle", "power_w": 2000},
            {"id": "long-500w", "label": None, "power_w": 500},
        ],
    }


def swapped_installation():
    """Both relay-driven estimates were replaced by Energy Mapper statistics."""
    h = meter_with_channels()
    h.add(sensor("switch.water_heater", "off"), "heater")
    h.add(sensor("switch.radiator_relay", "on"), "radiator")
    h.add(energy("sensor.fridge_plug_energy"), "plug")
    h.add(power("sensor.fridge_plug_power"), "plug")
    h.devices = [
        {"stat_consumption": "sensor.fridge_plug_energy", "name": "Fridge"},
        {"stat_consumption": HEATER},
        {"stat_consumption": RADIATOR, "name": "Living room radiator"},
        {"stat_consumption": KETTLE},
    ]
    return h


class StatisticIdTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(statistic_id(KIND_LOAD, "switch.water_heater"), HEATER)
        self.assertEqual(statistic_id(KIND_SIGNATURE, "short-2000w"), KETTLE)
        self.assertEqual(
            statistic_id(KIND_SIGNATURE, "cyc-150w-2"), "energy_mapper:signature_cyc_150w_2_energy"
        )
        self.assertEqual(slugify("Input_Boolean.Pump__1"), "input_boolean_pump_1")
        self.assertEqual(slugify("--"), "unknown")
        self.assertEqual(kind_of(HEATER), KIND_LOAD)
        self.assertEqual(kind_of(KETTLE), KIND_SIGNATURE)
        self.assertIsNone(kind_of("energy_mapper:other"))
        self.assertIsNone(kind_of("sensor.water_heater_energy"))
        self.assertTrue(is_own("energy_mapper:other"))
        self.assertFalse(is_own(None))

    def test_known_statistics_keeps_forgotten_appliances(self):
        mapping = known_statistics(learned_registry())
        self.assertEqual(set(mapping), {HEATER, RADIATOR, KETTLE})  # unlabelled signature left out
        self.assertEqual(mapping[RADIATOR]["source_id"], "switch.radiator_relay")
        self.assertEqual(mapping[RADIATOR]["on_states"], ["off"])
        self.assertEqual(
            mapping[KETTLE], {"kind": KIND_SIGNATURE, "source_id": "short-2000w", "name": "Kettle"}
        )
        # the registry forgot the radiator (idle all summer): its entry stays
        later = known_statistics({"switched_loads": learned_registry()["switched_loads"][:1]}, mapping)
        self.assertEqual(later[RADIATOR], mapping[RADIATOR])
        self.assertEqual(known_statistics(None), {})


class OwnStatisticTests(unittest.TestCase):
    def test_swapped_devices_stay_switched_loads(self):
        h = swapped_installation()
        d = derive(h.prefs(), h.states, h.registry, statistics=known_statistics(learned_registry()))
        kinds = [f.kind for f in d.findings]
        self.assertNotIn("unavailable", kinds)
        self.assertNotIn("unknown_statistic", kinds)
        self.assertNotIn("no_power_sensor", kinds)
        loads = {s.entity_id: s for s in d.config.switched_loads}
        self.assertEqual(set(loads), {"switch.water_heater", "switch.radiator_relay"})
        self.assertEqual(loads["switch.water_heater"].on_states, ("on",))
        self.assertEqual(loads["switch.water_heater"].name, "Water heater")  # from the registry
        self.assertEqual(loads["switch.radiator_relay"].on_states, ("off",))  # learned polarity kept
        self.assertEqual(loads["switch.radiator_relay"].name, "Living room radiator")  # dashboard name wins
        # never subtracted: only the real sub-meter is
        self.assertEqual([s.entity_id for s in d.config.submeters], ["sensor.fridge_plug_power"])
        roles = {t.energy_entity: (t.role, t.source_entity) for t in d.tracked}
        self.assertEqual(roles[HEATER], ("energy_mapper", "switch.water_heater"))
        self.assertEqual(roles[RADIATOR], ("energy_mapper", "switch.radiator_relay"))
        self.assertEqual(roles[KETTLE], ("energy_mapper", None))
        # not compared with the measured energy (it is the measured energy)
        report = {"start": 0, "end": 86400, "energy_kwh": {"known": {"Water heater": 4.6}}}
        self.assertEqual(tracking_gaps(report, d.tracked, {HEATER: [(10, "0"), (80000, "40")]}), [])

    def test_without_stored_mapping_the_entity_is_found_from_the_id(self):
        h = swapped_installation()
        h.devices = h.devices[:3]
        d = derive(h.prefs(), h.states, h.registry)
        loads = {s.entity_id: s.on_states for s in d.config.switched_loads}
        # default polarity: the engine's automatic polarity finds the inversion
        self.assertEqual(loads, {"switch.water_heater": ("on",), "switch.radiator_relay": ("on",)})
        self.assertEqual([f.kind for f in d.findings if f.kind == "unavailable"], [])

    def test_estimate_and_statistic_of_the_same_relay(self):
        h = swapped_installation()
        h.add(energy("sensor.radiator_energy", source_entity="switch.radiator_relay"), "est", "powercalc")
        h.devices.append({"stat_consumption": "sensor.radiator_energy", "name": "Radiator estimate"})
        d = derive(h.prefs(), h.states, h.registry, statistics=known_statistics(learned_registry()))
        radiators = [s for s in d.config.switched_loads if s.entity_id == "switch.radiator_relay"]
        self.assertEqual(len(radiators), 1)  # measured once
        self.assertEqual(radiators[0].on_states, ("off",))

    def test_nested_devices(self):
        h = swapped_installation()
        h.add(energy("sensor.heater_pump_energy"), "pump")
        h.add(power("sensor.heater_pump_power"), "pump")
        # a plug counted inside the Energy Mapper statistic, and the radiator
        # statistic counted inside a sub-meter
        h.devices.append(
            {"stat_consumption": "sensor.heater_pump_energy", "name": "Pump", "included_in_stat": HEATER}
        )
        h.devices[2]["included_in_stat"] = "sensor.fridge_plug_energy"
        d = derive(h.prefs(), h.states, h.registry, statistics=known_statistics(learned_registry()))
        roles = {t.name: t.role for t in d.tracked}
        self.assertEqual(roles["Pump"], "nested")
        self.assertEqual(roles["Living room radiator"], "nested")
        self.assertEqual([s.entity_id for s in d.config.switched_loads], ["switch.water_heater"])
        self.assertEqual([s.entity_id for s in d.config.submeters], ["sensor.fridge_plug_power"])

    def test_stale_statistics(self):
        h = swapped_installation()
        h.devices = [
            {"stat_consumption": "energy_mapper:load_switch_removed_relay_energy", "name": "Old relay"},
            {"stat_consumption": "energy_mapper:signature_long_900w_energy"},
            {"stat_consumption": "energy_mapper:something_else"},
        ]
        mapping = known_statistics(learned_registry())
        mapping["energy_mapper:load_switch_removed_relay_energy"] = {
            "kind": KIND_LOAD,
            "source_id": "switch.removed_relay",
            "name": "Old relay",
            "on_states": ["on"],
        }
        d = derive(h.prefs(), h.states, h.registry, statistics=mapping)
        stale = [f for f in d.findings if f.kind == "unknown_statistic"]
        self.assertEqual(len(stale), 3)
        self.assertIn(
            "Old relay: Energy Mapper statistic of an appliance it no longer knows", stale[0].detail
        )
        self.assertNotIn("unavailable", [f.kind for f in d.findings])
        self.assertEqual(d.config.switched_loads, [])
        self.assertEqual({t.role for t in d.tracked}, {"energy_mapper"})

    def test_manual_switched_loads_still_win(self):
        h = swapped_installation()
        d = derive(
            h.prefs(),
            h.states,
            h.registry,
            {
                "switched_loads": [
                    {"entity_id": "switch.radiator_relay", "name": "Manual", "on_states": ["on"]}
                ]
            },
            known_statistics(learned_registry()),
        )
        loads = {s.entity_id: (s.name, s.on_states) for s in d.config.switched_loads}
        self.assertEqual(loads["switch.radiator_relay"], ("Manual", ("on",)))
        self.assertEqual(len(d.config.switched_loads), 2)


class DeclaredApplianceTests(unittest.TestCase):
    """Appliances declared by the integration (#11) over #36."""

    def appliances(self):
        return [
            # the heater statistic, now re-linked to a signature (merged away)
            {
                "key": "load:switch.water_heater",
                "name": "Boiler",
                "statistic_id": HEATER,
                "sources": [{"type": "signature", "id": "long-500w"}],
            },
            # the radiator, renamed; its learned polarity is kept
            {
                "key": "load:switch.radiator_relay",
                "name": "Bedroom radiator",
                "statistic_id": RADIATOR,
                "sources": [{"type": "switched_load", "entity_id": "switch.radiator_relay"}],
            },
        ]

    def test_declared_appliances_win_and_are_known(self):
        mapping = known_statistics(learned_registry(), None, self.appliances())
        self.assertEqual(mapping[HEATER]["kind"], KIND_SIGNATURE)
        self.assertEqual(mapping[HEATER]["appliance"], "load:switch.water_heater")
        self.assertEqual(mapping[RADIATOR]["name"], "Bedroom radiator")
        self.assertEqual(mapping[RADIATOR]["on_states"], ["off"])
        h = swapped_installation()
        h.devices[2].pop("name")
        d = derive(h.prefs(), h.states, h.registry, statistics=mapping)
        self.assertNotIn("unknown_statistic", [f.kind for f in d.findings])
        loads = {s.entity_id: (s.name, s.on_states) for s in d.config.switched_loads}
        self.assertEqual(loads, {"switch.radiator_relay": ("Bedroom radiator", ("off",))})

    def test_marker_of_a_deleted_appliance_is_dropped(self):
        mapping = known_statistics(learned_registry(), None, self.appliances())
        later = known_statistics(learned_registry(), mapping, [])
        self.assertNotIn("appliance", later[HEATER])
        self.assertEqual(later[HEATER]["source_id"], "switch.water_heater")  # from the registry

    def test_excluded_switch_is_not_measured_from_its_statistic(self):
        h = swapped_installation()
        d = derive(
            h.prefs(),
            h.states,
            h.registry,
            {"excluded_switched_loads": ["switch.radiator_relay"]},
            known_statistics(learned_registry()),
        )
        self.assertEqual([s.entity_id for s in d.config.switched_loads], ["switch.water_heater"])


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
