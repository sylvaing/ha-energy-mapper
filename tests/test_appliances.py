"""Tests of the user's appliances in the add-on (issue #11): validation of the
declarations pushed by the integration, labels in the registry, expected
signatures, switched loads, sub-meters and exclusions in the analysis, and
the HTTP endpoints."""

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
from appliances import AppliancesError, analysis_options, apply_to_registry, validate  # noqa: E402
from energy_config import derive, tracking_gaps  # noqa: E402
from nilm_core.registry import expected_signature, nearest_signature, update  # noqa: E402
from service import Analyzer, derive_options  # noqa: E402
from settings import SettingsError  # noqa: E402
from settings import validate as validate_settings  # noqa: E402
from storage import ReportStore  # noqa: E402
from test_energy_config import energy, meter_with_channels, power  # noqa: E402
from test_registry import D0, act, by_id, midnight, nightly, store_day, summary  # noqa: E402

UTC = dt.UTC


def kettle(name="Kettle", key="signature:short-2000w", **extra):
    return {
        "key": key,
        "name": name,
        "sources": [{"type": "signature", "id": "short-2000w", **extra}],
    }


def heater(name="Water heater", on_states=None):
    source = {"type": "switched_load", "entity_id": "switch.water_heater"}
    if on_states is not None:
        source["on_states"] = on_states
    return {
        "key": "load:switch.water_heater",
        "name": name,
        "statistic_id": "energy_mapper:load_switch_water_heater_energy",
        "sources": [source],
    }


def registry(*sigs):
    return {
        "version": 1,
        "updated_at": 1.0,
        "power_tol": 0.12,
        "signatures": [
            {"id": sid, "kind": kind, "band": band, "power_w": p, "label": label, "weight": 1.0}
            for sid, kind, band, p, label in sigs
        ],
        "switched_loads": [],
    }


class ValidateTests(unittest.TestCase):
    def test_normalises(self):
        data = {
            "appliances": [
                heater(on_states=[" off ", "off"]),
                {
                    "key": "appliance:dryer",
                    "name": " Dryer ",
                    "sources": [
                        {
                            "type": "signature",
                            "id": "long-2400w",
                            "kind": "steady",
                            "band": "long",
                            "power_w": 2400,
                        },
                        {"type": "signature", "id": "cyc-600w"},
                        {"type": "submeter", "entity_id": "sensor.dryer_plug_power"},
                    ],
                },
            ]
        }
        out = validate(data)
        self.assertEqual(out[0]["sources"][0]["on_states"], ["off"])
        self.assertEqual(out[1]["name"], "Dryer")
        self.assertEqual(
            out[1]["sources"][0],
            {"type": "signature", "id": "long-2400w", "kind": "steady", "band": "long", "power_w": 2400.0},
        )
        self.assertEqual(out[1]["sources"][1], {"type": "signature", "id": "cyc-600w"})
        self.assertEqual(validate({"appliances": []}), [])

    def test_rejects(self):
        bad = [
            None,
            [],
            {"appliances": {}},
            {"appliances": [], "extra": 1},
            {"appliances": [{"key": "a", "name": "A", "sources": [], "x": 1}]},
            {"appliances": [{"key": "a", "name": "", "sources": []}]},
            {"appliances": [{"key": "a", "name": "A", "sources": [{"type": "relay"}]}]},
            {
                "appliances": [
                    {"key": "a", "name": "A", "sources": [{"type": "submeter", "entity_id": "switch.x"}]}
                ]
            },
            {
                "appliances": [
                    {"key": "a", "name": "A", "sources": [{"type": "switched_load", "entity_id": "X"}]}
                ]
            },
            {"appliances": [{"key": "a", "name": "A", "sources": [{"type": "signature", "id": "../x"}]}]},
            {"appliances": [kettle(kind="steady", band="session", power_w=2000)]},
            {"appliances": [kettle(kind="steady", band="short", power_w=0)]},
            {"appliances": [kettle(), kettle(key="other")]},  # one signature, two appliances
            {"appliances": [kettle(), kettle(name="Other")]},  # same key twice
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(AppliancesError):
                validate(data)


class RegistryLabelTests(unittest.TestCase):
    def test_labels_follow_the_declarations(self):
        reg = registry(
            ("short-2000w", "steady", "short", 2000, None),
            ("long-900w", "steady", "long", 900, "Old label"),
        )
        self.assertTrue(apply_to_registry(reg, [kettle()], 5.0))
        self.assertEqual(
            (by_id(reg, "short-2000w")["label"], by_id(reg, "short-2000w")["appliance"]),
            ("Kettle", "signature:short-2000w"),
        )
        # a label no appliance declares any more is cleared (appliance deleted)
        self.assertIsNone(by_id(reg, "long-900w")["label"])
        self.assertFalse(apply_to_registry(reg, [kettle()], 5.0))
        apply_to_registry(reg, [kettle("Electric kettle")], 5.0)
        self.assertEqual(by_id(reg, "short-2000w")["label"], "Electric kettle")

    def test_missing_declared_signature_is_recreated(self):
        reg = registry()
        declared = kettle(kind="steady", band="short", power_w=2000)
        self.assertTrue(apply_to_registry(reg, [declared], 5.0))
        sig = by_id(reg, "short-2000w")
        self.assertEqual(
            (sig["label"], sig["power_w"], sig["weight"], sig["expected"]), ("Kettle", 2000, 0.0, True)
        )
        # without a group and power it cannot be recreated
        reg = registry()
        self.assertFalse(apply_to_registry(reg, [kettle()], 5.0))
        self.assertEqual(reg["signatures"], [])

    def test_nearest_and_expected(self):
        reg = registry(
            ("short-2000w", "steady", "short", 2000, None), ("cyc-1400w", "cycling", "session", 1400, None)
        )
        self.assertEqual(nearest_signature(reg, "steady", "short", 2150)["id"], "short-2000w")
        self.assertIsNone(nearest_signature(reg, "steady", "medium", 2000))
        self.assertIsNone(nearest_signature(reg, "steady", "short", 2500))
        self.assertEqual(nearest_signature(reg, "cycling", "short", 1450)["id"], "cyc-1400w")
        sig = expected_signature(reg, "steady", "short", 2040, "Kettle", 5.0)
        self.assertEqual(sig["id"], "short-2000w-2")  # the id is taken
        self.assertEqual(expected_signature(reg, "cycling", "x", 600, "Hob", 5.0)["id"], "cyc-600w")

    def test_expected_signature_takes_the_first_use(self):
        """An appliance declared before it is seen ("Kettle, 2000 W") is
        recognised from its first use, and kept while it is not seen."""
        reg = {"version": 1, "updated_at": None, "power_tol": 0.12, "signatures": [], "switched_loads": []}
        sig = expected_signature(reg, "steady", "short", 2000, "Kettle", midnight(D0))
        reg["signatures"].append(sig)
        # nothing seen for a year: still there
        later = D0 + dt.timedelta(days=365)
        kept = nightly({}, later, reg)
        self.assertEqual(by_id(kept, sig["id"])["label"], "Kettle")
        # first uses at 2080 W: the cluster takes the expected id and label
        d = D0 + dt.timedelta(days=3)
        sums = {d.isoformat(): summary(d, [act(d, 8, 2080, 150), act(d, 18, 2060, 140)])}
        seen = nightly(sums, d, reg)
        got = by_id(seen, sig["id"])
        self.assertEqual((got["label"], got["count"], got["power_w"]), ("Kettle", 2, 2060))
        self.assertNotIn("expected", got)
        # an unlabelled expected signature is forgotten at the next update
        reg["signatures"][0]["label"] = None
        self.assertEqual(update(reg, {}, midnight(later), None)["signatures"], [])


class AnalysisOptionsTests(unittest.TestCase):
    def test_declared_switched_loads_and_submeters(self):
        options = derive_options(
            {
                "switched_loads": [
                    {"entity_id": "switch.water_heater", "name": "Boiler", "on_states": ["off"]}
                ],
                "excluded_switched_loads": ["switch.pump", ""],
                "appliances": [
                    heater(),
                    {
                        "key": "load:switch.radiator",
                        "name": "Radiator",
                        "sources": [
                            {"type": "switched_load", "entity_id": "switch.radiator", "on_states": ["off"]}
                        ],
                    },
                    {
                        "key": "signature:long-500w",
                        "name": "Dryer",
                        "sources": [{"type": "submeter", "entity_id": "sensor.dryer_power"}],
                    },
                ],
            }
        )
        self.assertEqual(
            options["switched_loads"],
            [
                # the settings' on-states are kept, the name is the appliance's
                {"entity_id": "switch.water_heater", "name": "Water heater", "on_states": ["off"]},
                {"entity_id": "switch.radiator", "name": "Radiator", "on_states": ["off"]},
            ],
        )
        self.assertEqual(options["extra_submeters"], [("sensor.dryer_power", "Dryer")])
        self.assertEqual(options["excluded_switched_loads"], ["switch.pump"])
        # declared on-states win
        out = analysis_options({"switched_loads": []}, [heater(on_states=["heat"])])
        self.assertEqual(out["switched_loads"][0]["on_states"], ["heat"])
        self.assertEqual(analysis_options({}, []), {})

    def test_derive_with_exclusions_and_extra_submeters(self):
        h = meter_with_channels()
        h.add(energy("sensor.fridge_plug_energy"), "plug")
        h.add(power("sensor.fridge_plug_power"), "plug")
        h.add(energy("sensor.pump_energy", source_entity="switch.pump"), "est", "powercalc")
        h.add(power("sensor.pump_power", "300", source_entity="switch.pump"), "est", "powercalc")
        h.add(energy("sensor.water_heater_energy", source_entity="switch.water_heater"), "est2", "powercalc")
        h.devices = [
            {"stat_consumption": "sensor.fridge_plug_energy", "name": "Fridge"},
            {"stat_consumption": "sensor.pump_energy", "name": "Pump"},
            {"stat_consumption": "sensor.water_heater_energy", "name": "Water heater"},
        ]
        d = derive(h.prefs(), h.states, h.registry)
        self.assertEqual(
            {s.entity_id for s in d.config.switched_loads}, {"switch.pump", "switch.water_heater"}
        )
        options = derive_options(
            {
                "excluded_switched_loads": ["switch.pump"],
                "appliances": [
                    {
                        "key": "signature:long-500w",
                        "name": "Dryer",
                        "sources": [{"type": "submeter", "entity_id": "sensor.dryer_power"}],
                    }
                ],
            }
        )
        d = derive(h.prefs(), h.states, h.registry, options)
        # the excluded relay is no longer measured: its estimate is subtracted instead
        self.assertEqual([s.entity_id for s in d.config.switched_loads], ["switch.water_heater"])
        roles = {t.name: t.role for t in d.tracked}
        self.assertEqual(roles["Pump"], "estimated")
        self.assertEqual(
            [s.entity_id for s in d.config.submeters],
            ["sensor.fridge_plug_power", "sensor.pump_power", "sensor.dryer_power"],
        )
        # a manual switched load wins over its exclusion; settings' sub-meters + extra ones
        options = derive_options(
            {
                "excluded_switched_loads": ["switch.pump"],
                "switched_loads": [{"entity_id": "switch.pump", "name": "Pump"}],
                "submeters": ["sensor.dryer_power"],
                "appliances": [
                    {
                        "key": "signature:long-500w",
                        "name": "Dryer",
                        "sources": [{"type": "submeter", "entity_id": "sensor.dryer_power"}],
                    }
                ],
            }
        )
        d = derive(h.prefs(), h.states, h.registry, options)
        self.assertIn("switch.pump", [s.entity_id for s in d.config.switched_loads])
        self.assertEqual([s.entity_id for s in d.config.submeters], ["sensor.dryer_power"])

    def test_tracking_gap_follows_a_renamed_switched_load(self):
        h = meter_with_channels()
        h.add(energy("sensor.water_heater_energy", source_entity="switch.water_heater"), "est", "powercalc")
        h.devices = [{"stat_consumption": "sensor.water_heater_energy", "name": "Water heater"}]
        d = derive(h.prefs(), h.states, h.registry)
        report = {
            "start": 0,
            "end": 86400,
            "energy_kwh": {"known": {"Boiler": 4.6}},
            "known_loads": [{"entity_id": "switch.water_heater", "name": "Boiler"}],
        }
        hist = {"sensor.water_heater_energy": [(10, "6.200"), (86000, "6.210")]}
        self.assertEqual(len(tracking_gaps(report, d.tracked, hist)), 1)

    def test_settings_key(self):
        self.assertEqual(
            validate_settings({"excluded_switched_loads": ["switch.pump", "switch.pump", "light.x"]}),
            {"excluded_switched_loads": ["switch.pump", "light.x"]},
        )
        self.assertEqual(validate_settings({"excluded_switched_loads": []}), {})
        for bad in (["Pump"], "switch.pump", [1]):
            with self.subTest(bad=bad), self.assertRaises(SettingsError):
                validate_settings({"excluded_switched_loads": bad})


class StoreAnalyzer(Analyzer):
    def __init__(self, store, today: dt.date):
        super().__init__(None, store, {}, clock=lambda: midnight(today) + 5 * 3600)
        self.tz = UTC


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReportStore(self._tmp.name)
        self.today = D0 + dt.timedelta(days=3)
        self.analyzer = StoreAnalyzer(self.store, self.today)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, method, target, body=None):
        raw = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body or b""
        return route(self.analyzer, self.store, method, target, raw)

    def kettle_days(self):
        for i in range(3):
            d = D0 + dt.timedelta(days=i)
            store_day(self.store, d, [act(d, 8, 2000, 150)], curve_rows=10)
        self.analyzer.maintain()
        return self.store.load_registry()["signatures"][0]["id"]

    def test_put_and_get(self):
        self.assertEqual(self.call("GET", "/api/appliances"), (200, {"appliances": []}))
        sid = self.kettle_days()
        body = {
            "appliances": [
                {"key": f"signature:{sid}", "name": "Kettle", "sources": [{"type": "signature", "id": sid}]}
            ]
        }
        status, answer = self.call("PUT", "/api/appliances", body)
        self.assertEqual((status, answer), (200, body))
        # the label is in the registry at once, and after the next update
        self.assertEqual(by_id(self.store.load_registry(), sid)["label"], "Kettle")
        self.analyzer.maintain()
        self.assertEqual(by_id(self.store.load_registry(), sid)["label"], "Kettle")
        self.assertEqual(
            self.call("GET", "/api/signatures")[1]["signatures"][0]["appliance"], f"signature:{sid}"
        )
        # kept across restarts
        restarted = StoreAnalyzer(self.store, self.today)
        self.assertEqual(route(restarted, self.store, "GET", "/api/appliances")[1], body)
        # deleted in Home Assistant: the label goes
        self.assertEqual(self.call("PUT", "/api/appliances", {"appliances": []})[0], 200)
        self.assertIsNone(by_id(self.store.load_registry(), sid)["label"])

    def test_invalid(self):
        status, answer = self.call("PUT", "/api/appliances", {"appliances": [{"key": "x"}]})
        self.assertEqual((status, answer["error"]), (400, "invalid_appliances"))
        self.assertEqual(self.call("PUT", "/api/appliances", b"{")[1]["error"], "invalid_json")
        self.assertEqual(self.call("POST", "/api/appliances")[0], 405)
        self.assertEqual(self.call("GET", "/api/appliances")[1], {"appliances": []})

    def test_expected_signature(self):
        status, answer = self.call(
            "POST", "/api/signatures", {"kind": "steady", "band": "short", "power_w": 2000, "label": "Kettle"}
        )
        self.assertEqual((status, answer["created"], answer["signature"]["id"]), (200, True, "short-2000w"))
        sid = answer["signature"]["id"]
        self.assertEqual(by_id(self.store.load_registry(), sid)["label"], "Kettle")
        # declared by the integration, then the kettle is used
        self.call(
            "PUT",
            "/api/appliances",
            {"appliances": [kettle(kind="steady", band="short", power_w=2000)]},
        )
        self.kettle_days()
        reg = self.store.load_registry()
        self.assertEqual([(s["id"], s["label"], s["count"]) for s in reg["signatures"]], [(sid, "Kettle", 3)])
        # the same power again: that appliance's signature, refused
        status, answer = self.call(
            "POST",
            "/api/signatures",
            {"kind": "steady", "band": "short", "power_w": 2050, "label": "Kettle 2"},
        )
        self.assertEqual((status, answer["error"], answer["id"]), (409, "conflict", sid))
        # a signature already seen but not named: it is returned as is
        self.call("PUT", "/api/appliances", {"appliances": []})
        status, answer = self.call(
            "POST", "/api/signatures", {"kind": "steady", "band": "short", "power_w": 1950, "label": "Kettle"}
        )
        self.assertEqual((status, answer["created"], answer["signature"]["id"]), (200, False, sid))
        for bad in (
            {"kind": "steady", "band": "session", "power_w": 2000, "label": "x"},
            {"kind": "steady", "band": "short", "power_w": 10, "label": "x"},
            {"kind": "steady", "band": "short", "power_w": 2000, "label": ""},
            [],
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("POST", "/api/signatures", bad)[1]["error"], "invalid_signature")
        # cycling: the band is implied
        status, answer = self.call(
            "POST", "/api/signatures", {"kind": "cycling", "power_w": 1400, "label": "Hob"}
        )
        self.assertEqual((status, answer["signature"]["id"]), (200, "cyc-1400w"))

    def test_lost_registry_keeps_the_labels(self):
        sid = self.kettle_days()
        self.call(
            "PUT",
            "/api/appliances",
            {"appliances": [kettle(kind="steady", band="short", power_w=2000) | {"key": "k"}]},
        )
        self.store.registry_path.write_text("{corrupt")
        self.analyzer.maintain()  # rebuilt from the day summaries
        self.assertEqual(sid, "short-2000w")
        self.assertEqual(by_id(self.store.load_registry(), sid)["label"], "Kettle")


if __name__ == "__main__":
    unittest.main()
