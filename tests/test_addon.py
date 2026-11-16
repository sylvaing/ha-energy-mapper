"""Tests of the add-on service: history conversion, calendar, storage,
scheduler passes and the HTTP API, with a fake Home Assistant (no network)."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from api import make_server, route  # noqa: E402
from const import VERSION  # noqa: E402
from ha_client import HAClient, HAError, endpoint_from_env, parse_history  # noqa: E402
from service import (  # noqa: E402
    AnalysisError,
    Analyzer,
    Scheduler,
    day_bounds,
    days_to_analyze,
    derive_options,
    next_run,
    parse_hhmm,
)
from settings import SettingsError, defaults, validate  # noqa: E402
from storage import ReportStore, parse_day  # noqa: E402
from synth import House, Steady  # noqa: E402
from ws_client import OP_TEXT, encode_frame, read_frame  # noqa: E402

UTC = dt.UTC
TODAY = dt.date(2027, 3, 10)
NOW = dt.datetime.combine(TODAY, dt.time(5, 0), tzinfo=UTC).timestamp()


def midnight(day: dt.date) -> float:
    return dt.datetime.combine(day, dt.time(0), tzinfo=UTC).timestamp()


# ---------------------------------------------------------------------------
# fake Home Assistant
# ---------------------------------------------------------------------------
def snapshot(time_zone="UTC"):
    states = [
        {
            "entity_id": "sensor.grid_energy",
            "state": "1234.5",
            "attributes": {"device_class": "energy", "unit_of_measurement": "kWh"},
        },
        {
            "entity_id": "sensor.grid_power",
            "state": "300",
            "attributes": {"device_class": "power", "unit_of_measurement": "W"},
        },
    ]
    registry = [
        {"entity_id": "sensor.grid_energy", "device_id": "meter", "platform": "test"},
        {"entity_id": "sensor.grid_power", "device_id": "meter", "platform": "test"},
    ]
    prefs = {
        "energy_sources": [{"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid_energy"}]}],
        "device_consumption": [],
    }
    return {"prefs": prefs, "states": states, "registry": registry, "config": {"time_zone": time_zone}}


class FakeHA:
    """Serves a synthetic house: recorded from ``data_start`` (unix ts)."""

    def __init__(self, data_start: float, prefs=True):
        self.data_start = data_start
        self.house = House(
            base_w=250, loads=[Steady(midnight(TODAY) - 20 * 3600, midnight(TODAY) - 18 * 3600, 2000)]
        )
        self.history_calls: list[tuple[list[str], float, float]] = []
        self.snapshots = 0
        self.fail = False
        self.prefs = prefs

    def snapshot(self):
        if self.fail:
            raise HAError("websocket: connection refused")
        self.snapshots += 1
        snap = snapshot()
        if not self.prefs:
            snap["prefs"] = None
        return snap

    def history(self, entities, start, end):
        self.history_calls.append((list(entities), start, end))
        a = max(start, self.data_start)
        if a >= end:
            return {}
        return self.house.histories(a, end, dt=60)


def make_analyzer(tmp, ha, options=None, now=NOW):
    store = ReportStore(tmp)
    return Analyzer(ha, store, options or {"backfill_days": 30}, clock=lambda: now), store


# ---------------------------------------------------------------------------
# history conversion
# ---------------------------------------------------------------------------
class HistoryTests(unittest.TestCase):
    def test_minimal_response(self):
        payload = [
            [
                {"entity_id": "sensor.a", "state": "10", "last_changed": "2027-03-08T23:59:00+00:00"},
                {"state": "12.5", "last_changed": "2027-03-09T00:00:10.500000+00:00"},
                {"state": "unavailable", "last_changed": "2027-03-09T00:01:00+00:00"},
            ],
            [{"entity_id": "switch.b", "state": "on", "last_changed": "2027-03-09T01:00:00+00:00"}],
            [],
        ]
        start = midnight(dt.date(2027, 3, 9))
        hist = parse_history(payload, window_start=start)
        self.assertEqual(set(hist), {"sensor.a", "switch.b"})
        self.assertEqual(hist["sensor.a"][0], (start, "10"))  # clamped to the window
        self.assertEqual(hist["sensor.a"][1], (start + 10.5, "12.5"))
        self.assertEqual(hist["sensor.a"][2][1], "unavailable")
        self.assertEqual(hist["switch.b"], [(start + 3600, "on")])

    def test_numeric_timestamps_and_full_rows(self):
        payload = [
            [
                {"entity_id": "sensor.a", "state": "1", "last_changed": 100.0, "attributes": {"x": 1}},
                {"entity_id": "sensor.a", "state": "2", "last_updated": 50.0},
            ]
        ]
        self.assertEqual(parse_history(payload)["sensor.a"], [(50.0, "2"), (100.0, "1")])

    def test_history_request(self):
        client = HAClient(endpoint_from_env({"HA_URL": "http://ha.local:8123/", "HA_TOKEN": "secret"}))
        seen = {}

        def fake_get(path):
            seen["path"] = path
            return []

        client._get = fake_get
        client.history(["sensor.a", "switch.b"], 0.0, 86400.0)
        path = seen["path"]
        self.assertTrue(path.startswith("/history/period/1970-01-01T00%3A00%3A00%2B00%3A00?"))
        self.assertIn("filter_entity_id=sensor.a%2Cswitch.b", path)
        self.assertIn("end_time=1970-01-02T00%3A00%3A00%2B00%3A00", path)
        self.assertIn("significant_changes_only=0", path)
        self.assertTrue(path.endswith("&minimal_response&no_attributes"))


class EndpointTests(unittest.TestCase):
    def test_standalone(self):
        ep = endpoint_from_env({"HA_URL": "https://ha.example:8123/", "HA_TOKEN": "secret"})
        self.assertEqual(ep.rest, "https://ha.example:8123/api")
        self.assertEqual(ep.websocket, "wss://ha.example:8123/api/websocket")
        self.assertFalse(ep.supervised)
        self.assertNotIn("secret", repr(ep))

    def test_supervisor(self):
        ep = endpoint_from_env({"SUPERVISOR_TOKEN": "secret"})
        self.assertEqual(ep.rest, "http://supervisor/core/api")
        self.assertEqual(ep.websocket, "ws://supervisor/core/websocket")
        self.assertTrue(ep.supervised)

    def test_standalone_wins_and_missing(self):
        ep = endpoint_from_env({"HA_URL": "http://h:8123", "HA_TOKEN": "t", "SUPERVISOR_TOKEN": "s"})
        self.assertFalse(ep.supervised)
        with self.assertRaises(HAError):
            endpoint_from_env({})


# ---------------------------------------------------------------------------
# websocket
# ---------------------------------------------------------------------------
class WebSocketTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        for size in (0, 5, 125, 126, 65535, 65536, 200_000):
            payload = bytes(i % 251 for i in range(size))
            data = encode_frame(OP_TEXT, payload)
            pos = 0

            def recv_exact(n, data=data):
                nonlocal pos
                out = data[pos : pos + n]
                pos += n
                return out

            fin, opcode, got = read_frame(recv_exact)
            self.assertTrue(fin)
            self.assertEqual(opcode, OP_TEXT)
            self.assertEqual(got, payload)
            self.assertEqual(pos, len(data))

    def test_commands_against_fake_server(self):
        """Handshake, auth, ids and a failing command, over a local socket."""
        import base64
        import hashlib

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        received = []

        def serve():
            conn, _ = srv.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", buf).group(1)
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            f = conn.makefile("rb")

            def send(obj):  # server frames are not masked; fragment the big one
                data = json.dumps(obj).encode()
                if len(data) > 1000:
                    half = len(data) // 2
                    conn.sendall(bytes([0x01, 126]) + half.to_bytes(2, "big") + data[:half])
                    conn.sendall(bytes([0x89, 0]))  # a ping in between
                    rest = data[half:]
                    conn.sendall(bytes([0x80, 126]) + len(rest).to_bytes(2, "big") + rest)
                elif len(data) < 126:
                    conn.sendall(bytes([0x81, len(data)]) + data)
                else:
                    conn.sendall(bytes([0x81, 126]) + len(data).to_bytes(2, "big") + data)

            def recv():
                while True:
                    _, opcode, payload = read_frame(f.read)
                    if opcode == OP_TEXT:
                        return json.loads(payload)

            send({"type": "auth_required"})
            received.append(recv())
            send({"type": "auth_ok"})
            for _ in range(2):
                msg = recv()
                received.append(msg)
                send({"type": "event", "id": 99})
                if msg["type"] == "get_states":
                    big = [{"entity_id": f"sensor.s{i}", "state": "1"} for i in range(100)]
                    send({"id": msg["id"], "type": "result", "success": True, "result": big})
                else:
                    send({"id": msg["id"], "type": "result", "success": False, "error": {"code": "x"}})
            f.close()
            conn.close()
            srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        client = HAClient(
            endpoint_from_env({"HA_URL": f"http://127.0.0.1:{port}", "HA_TOKEN": "tok"}), timeout=5
        )
        states, prefs = client.ws_commands([{"type": "get_states"}, {"type": "energy/get_prefs"}])
        t.join(5)
        self.assertEqual(received[0], {"type": "auth", "access_token": "tok"})
        self.assertEqual([m["id"] for m in received[1:]], [1, 2])
        self.assertEqual(len(states), 100)
        self.assertIsNone(prefs)

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        client = HAClient(
            endpoint_from_env({"HA_URL": f"http://127.0.0.1:{port}", "HA_TOKEN": "t"}), timeout=2
        )
        with self.assertRaises(HAError):
            client.snapshot()


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------
class CalendarTests(unittest.TestCase):
    def test_day_bounds_dst(self):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
        a, b = day_bounds(dt.date(2027, 3, 14), tz)  # spring forward
        self.assertEqual(b - a, 23 * 3600)
        a, b = day_bounds(dt.date(2027, 11, 7), tz)  # fall back
        self.assertEqual(b - a, 25 * 3600)
        a, b = day_bounds(dt.date(2027, 6, 1), tz)
        self.assertEqual(b - a, 24 * 3600)
        self.assertEqual(dt.datetime.fromtimestamp(a, tz).hour, 0)

    def test_days_to_analyze(self):
        today = dt.date(2027, 3, 10)
        self.assertEqual(
            days_to_analyze(today, {"2027-03-08"}, 3),
            [dt.date(2027, 3, 9), dt.date(2027, 3, 7)],
        )
        self.assertEqual(days_to_analyze(today, set(), 0), [dt.date(2027, 3, 9)])
        self.assertEqual(days_to_analyze(today, {"2027-03-09"}, 1), [])
        self.assertEqual(len(days_to_analyze(today, set(), 30)), 30)

    def test_next_run(self):
        at = dt.time(4, 30)
        now = dt.datetime(2027, 3, 10, 3, 0, tzinfo=UTC)
        self.assertEqual(next_run(now, at), dt.datetime(2027, 3, 10, 4, 30, tzinfo=UTC))
        now = dt.datetime(2027, 3, 10, 4, 30, tzinfo=UTC)
        self.assertEqual(next_run(now, at), dt.datetime(2027, 3, 11, 4, 30, tzinfo=UTC))

    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("23:05"), dt.time(23, 5))
        self.assertEqual(parse_hhmm(None), dt.time(4, 30))
        with self.assertLogs("service", "WARNING"):
            self.assertEqual(parse_hhmm("late"), dt.time(4, 30))

    def test_parse_day(self):
        self.assertEqual(parse_day("2027-03-09"), dt.date(2027, 3, 9))
        for bad in ("2027-02-30", "../x", "2027-3-9", "", None):
            self.assertIsNone(parse_day(bad))


class OptionsTests(unittest.TestCase):
    def test_empty_values_derive(self):
        self.assertEqual(
            derive_options({"grid_entity": "", "submeters": [], "switched_loads": [], "backfill_days": 30}),
            {"grid_invert": False, "battery_invert": False},
        )

    def test_manual_values(self):
        out = derive_options(
            {
                "grid_entity": " sensor.grid_power ",
                "grid_invert": True,
                "submeters": ["sensor.pump_power", ""],
                "switched_loads": [
                    {"entity_id": "switch.heater"},
                    {"entity_id": "switch.fan", "on_states": ["off"]},
                ],
                "step_threshold_w": 200,
            }
        )
        self.assertEqual(out["grid_entity"], "sensor.grid_power")
        self.assertTrue(out["grid_invert"])
        self.assertEqual(out["submeters"], ["sensor.pump_power"])
        self.assertEqual(
            out["switched_loads"],
            [
                {"entity_id": "switch.heater", "name": "switch.heater", "on_states": ["on"]},
                {"entity_id": "switch.fan", "name": "switch.fan", "on_states": ["off"]},
            ],
        )
        self.assertEqual(out["step_threshold_w"], 200.0)


# ---------------------------------------------------------------------------
# settings pushed by the integration
# ---------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_validate_normalises(self):
        self.assertEqual(validate({}), {})
        out = validate(
            {
                "grid_entity": " sensor.grid_power ",
                "pv_entity": None,
                "battery_entity": "",
                "grid_invert": True,
                "submeters": ["sensor.pump_power", "sensor.pump_power", "sensor.ev_power"],
                "switched_loads": [
                    {"entity_id": "switch.heater", "name": " Heater ", "on_states": ["on", " on "]},
                    {"entity_id": "switch.radiator", "name": "", "on_states": ["off"]},
                    {"entity_id": "input_boolean.kettle", "on_states": []},
                ],
                "step_threshold_w": 250,
                "backfill_days": 10.0,
                "analysis_time": "03:15:00",
            }
        )
        self.assertEqual(
            out,
            {
                "grid_entity": "sensor.grid_power",
                "grid_invert": True,
                "submeters": ["sensor.pump_power", "sensor.ev_power"],
                "switched_loads": [
                    {"entity_id": "switch.heater", "name": "Heater", "on_states": ["on"]},
                    {"entity_id": "switch.radiator", "on_states": ["off"]},
                    {"entity_id": "input_boolean.kettle"},
                ],
                "step_threshold_w": 250.0,
                "backfill_days": 10,
                "analysis_time": "03:15",
            },
        )
        self.assertEqual(validate({"submeters": [], "switched_loads": []}), {})

    def test_validate_rejects(self):
        bad = [
            [],
            {"nope": 1},
            {"grid_entity": "switch.grid"},
            {"grid_entity": "not an entity"},
            {"grid_entity": 3},
            {"grid_invert": "yes"},
            {"step_threshold_w": 10},
            {"step_threshold_w": True},
            {"step_threshold_w": float("nan")},
            {"backfill_days": 1.5},
            {"report_retention_days": 1},
            {"analysis_time": "25:00"},
            {"submeters": "sensor.a"},
            {"submeters": ["switch.a"]},
            {"switched_loads": ["switch.a"]},
            {"switched_loads": [{"name": "x"}]},
            {"switched_loads": [{"entity_id": "switch.a", "power": 3}]},
            {"switched_loads": [{"entity_id": "switch.a", "on_states": [""]}]},
            {"switched_loads": [{"entity_id": "switch.a"}, {"entity_id": "switch.a"}]},
            {"submeters": [f"sensor.s{i}" for i in range(101)]},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(SettingsError):
                validate(data)

    def test_defaults_follow_the_options(self):
        out = defaults({"backfill_days": 7, "step_threshold_w": 200, "analysis_time": ""})
        self.assertEqual(
            (out["backfill_days"], out["step_threshold_w"], out["analysis_time"], out["peak_threshold_w"]),
            (7, 200, "04:30", None),
        )

    def test_settings_win_over_options_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            analyzer, store = make_analyzer(tmp, FakeHA(0), {"backfill_days": 30, "grid_entity": "sensor.a"})
            self.assertEqual(analyzer.options["backfill_days"], 30)
            analyzer.set_settings({"backfill_days": 3, "pv_entity": "sensor.pv"})
            self.assertEqual(analyzer.backfill_days, 3)
            self.assertEqual(
                (analyzer.options["grid_entity"], analyzer.options["pv_entity"]), ("sensor.a", "sensor.pv")
            )
            again, _ = make_analyzer(tmp, FakeHA(0), {"backfill_days": 30})
            self.assertEqual(again.settings, {"backfill_days": 3, "pv_entity": "sensor.pv"})
            again.set_settings({})
            self.assertFalse(store.settings_path.exists())
            self.assertEqual(again.backfill_days, 30)

    def test_corrupt_or_invalid_settings_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportStore(tmp)
            store.settings_path.write_text("{not json")
            self.assertEqual(Analyzer(FakeHA(0), store, {}).settings, {})
            store.settings_path.write_text('{"backfill_days": -1}')
            self.assertEqual(Analyzer(FakeHA(0), store, {}).settings, {})

    def test_settings_drive_the_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            ha = FakeHA(data_start=midnight(TODAY) - 3 * 86400, prefs=False)
            analyzer, _ = make_analyzer(tmp, ha)
            with self.assertRaises(AnalysisError):
                analyzer.analyze_day(TODAY - dt.timedelta(days=1))
            analyzer.set_settings({"grid_entity": "sensor.grid_power"})
            report = analyzer.analyze_day(TODAY - dt.timedelta(days=1))
            self.assertEqual(report["config"]["grid_entity"], "sensor.grid_power")

    def test_scheduler_time_follows_the_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            analyzer, _ = make_analyzer(tmp, FakeHA(0))
            sched = Scheduler(analyzer, "04:30")
            self.assertEqual(sched.at, dt.time(4, 30))
            analyzer.set_settings({"analysis_time": "02:05"})
            self.assertEqual(sched.at, dt.time(2, 5))

    def test_automatic(self):
        with tempfile.TemporaryDirectory() as tmp:
            analyzer, _ = make_analyzer(tmp, FakeHA(0))
            analyzer.set_settings({"grid_entity": "sensor.other_power"})
            auto = analyzer.automatic()  # ignores the settings
            self.assertEqual(auto["grid_entity"], "sensor.grid_power")
            self.assertEqual((auto["pv_entity"], auto["submeters"], auto["switched_loads"]), (None, [], []))
            analyzer, _ = make_analyzer(tmp, FakeHA(0, prefs=False))
            self.assertIsNone(analyzer.automatic()["grid_entity"])


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
class StorageTests(unittest.TestCase):
    def test_save_load_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportStore(tmp)
            store.save("2027-03-09", {"a": 1, "curve": [1, 2]})
            store.save("2027-03-01", {"a": 2})
            (pathlib.Path(tmp) / "reports" / "notes.json.gz").write_bytes(b"")
            (pathlib.Path(tmp) / "reports" / "2027-03-05.json.gz").write_bytes(b"{broken")
            self.assertEqual(store.days(), ["2027-03-01", "2027-03-05", "2027-03-09"])
            self.assertEqual(store.load("2027-03-09"), {"a": 1, "curve": [1, 2]})
            self.assertEqual(store.load_summary("2027-03-09"), {"a": 1})
            self.assertIsNone(store.load("2027-03-05"))
            self.assertIsNone(store.load("../state"))
            self.assertEqual(list((pathlib.Path(tmp) / "reports").glob(".tmp-*")), [])
            with self.assertRaises(ValueError):
                store.save("x", {})

    def test_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportStore(tmp)
            self.assertEqual(store.state(), {})
            store.set_state(last_run=12.0)
            self.assertEqual(ReportStore(tmp).state(), {"last_run": 12.0})


# ---------------------------------------------------------------------------
# analysis passes
# ---------------------------------------------------------------------------
class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_of_a_day(self):
        ha = FakeHA(data_start=midnight(TODAY) - 5 * 86400)
        analyzer, store = make_analyzer(self.tmp, ha)
        day = TODAY - dt.timedelta(days=1)
        report = analyzer.analyze_day(day)
        self.assertEqual(report["day"], "2027-03-09")
        self.assertEqual(report["start"], midnight(day))
        self.assertEqual(report["end"], midnight(TODAY))
        self.assertEqual(report["time_zone"], "UTC")
        self.assertEqual(report["addon_version"], VERSION)
        self.assertEqual(report["config"]["grid_entity"], "sensor.grid_power")
        self.assertIsInstance(report["findings"], list)
        # 250 W base all day + 2 kW for 2 hours
        self.assertAlmostEqual(report["energy_kwh"]["house"], 0.25 * 24 + 4, delta=0.3)
        self.assertEqual(store.load("2027-03-09")["day"], "2027-03-09")
        self.assertEqual(store.state()["last_run"], NOW)
        entities, start, end = ha.history_calls[-1]
        self.assertEqual(entities, ["sensor.grid_power"])
        self.assertEqual(end, midnight(TODAY))
        self.assertEqual(start, midnight(day) - 3 * 3600)  # context before midnight

    def test_today_is_refused(self):
        analyzer, _ = make_analyzer(self.tmp, FakeHA(0))
        with self.assertRaises(AnalysisError) as cm:
            analyzer.analyze_day(TODAY)
        self.assertEqual(cm.exception.code, "invalid_day")

    def test_backfill_stops_where_the_recorder_ends(self):
        # recorded from 03:00 four days ago: that day is partial
        ha = FakeHA(data_start=midnight(TODAY) - 4 * 86400 + 3 * 3600)
        analyzer, store = make_analyzer(self.tmp, ha)
        with self.assertLogs("service", "INFO") as logs:
            result = analyzer.catch_up()
        self.assertEqual(result["analysed"], ["2027-03-09", "2027-03-08", "2027-03-07"])
        self.assertEqual(store.days(), ["2027-03-07", "2027-03-08", "2027-03-09"])
        self.assertTrue(any("starts after midnight" in m for m in logs.output))
        self.assertEqual(len(ha.history_calls), 4)  # stopped after the partial day

        # next night: only the new day is analysed
        later = NOW + 86400
        analyzer.clock = lambda: later
        ha.history_calls.clear()
        self.assertEqual(analyzer.catch_up()["analysed"], ["2027-03-10"])
        self.assertEqual(len(ha.history_calls), 2)  # yesterday, then the partial day again

    def test_backfill_skips_an_outage_and_stops_on_empty_days(self):
        ha = FakeHA(data_start=0)
        real = ha.history

        def history(entities, start, end):
            day = dt.datetime.fromtimestamp(end - 1, UTC).date()
            if day == dt.date(2027, 3, 8) or day <= dt.date(2027, 3, 5):  # an outage, then nothing
                return {}
            return real(entities, start, end)

        ha.history = history
        analyzer, _ = make_analyzer(self.tmp, ha, {"backfill_days": 30})
        with self.assertLogs("service", "INFO"):
            result = analyzer.catch_up()
        self.assertEqual(result["analysed"], ["2027-03-09", "2027-03-07", "2027-03-06"])
        # 03-08 skipped, then the backfill stops at the third empty day (03-03)
        self.assertEqual(result["failed"], ["2027-03-08", "2027-03-05", "2027-03-04"])

    def test_nothing_missing_does_not_call_ha(self):
        ha = FakeHA(0)
        analyzer, store = make_analyzer(self.tmp, ha, {"backfill_days": 1})
        store.save("2027-03-09", {})
        self.assertEqual(analyzer.catch_up(), {"analysed": [], "failed": []})
        self.assertEqual(ha.snapshots, 0)

    def test_no_data(self):
        ha = FakeHA(data_start=NOW + 1)
        analyzer, _ = make_analyzer(self.tmp, ha)
        with self.assertRaises(AnalysisError) as cm:
            analyzer.analyze_day(TODAY - dt.timedelta(days=1))
        self.assertEqual(cm.exception.code, "no_data")

    def test_no_grid(self):
        analyzer, _ = make_analyzer(self.tmp, FakeHA(0, prefs=False))
        with self.assertRaises(AnalysisError) as cm:
            analyzer.catch_up()
        self.assertEqual(cm.exception.code, "no_grid")

    def test_manual_grid_without_energy_dashboard(self):
        analyzer, store = make_analyzer(
            self.tmp, FakeHA(0, prefs=False), {"backfill_days": 1, "grid_entity": "sensor.grid_power"}
        )
        self.assertEqual(analyzer.catch_up()["analysed"], ["2027-03-09"])

    def test_failed_day_does_not_stop_the_pass(self):
        ha = FakeHA(data_start=0)
        real = ha.history

        def flaky(entities, start, end):
            if start < midnight(TODAY - dt.timedelta(days=1)) - 4 * 3600:
                raise ValueError("boom")
            return real(entities, start, end)

        ha.history = flaky
        analyzer, _ = make_analyzer(self.tmp, ha, {"backfill_days": 3})
        with self.assertLogs("service", "ERROR"):
            result = analyzer.catch_up()
        self.assertEqual(result, {"analysed": ["2027-03-09"], "failed": ["2027-03-08", "2027-03-07"]})

    def test_scheduler_retries_when_ha_is_down(self):
        ha = FakeHA(0)
        ha.fail = True
        analyzer, store = make_analyzer(self.tmp, ha, {"backfill_days": 2})
        sched = Scheduler(analyzer, "04:30")
        with self.assertLogs("service", "WARNING"):
            self.assertEqual(sched.run_once(), 60)
            self.assertEqual(sched.run_once(), 300)
        ha.fail = False
        # 05:00 now: next run tomorrow 04:30
        self.assertEqual(sched.run_once(), 23.5 * 3600)
        self.assertEqual(len(store.days()), 2)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ha = FakeHA(data_start=midnight(TODAY) - 3 * 86400)
        self.analyzer, self.store = make_analyzer(self._tmp.name, self.ha)

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, method, target, body=b""):
        return route(self.analyzer, self.store, method, target, body)

    def test_health_and_days(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual(
            (status, body["status"], body["version"], body["last_run"], body["days"], body["signatures"]),
            (200, "ok", VERSION, None, 0, 0),
        )
        self.assertEqual(self.call("GET", "/api/days"), (200, {"days": [], "summary_days": []}))
        self.assertEqual(
            self.call("POST", "/api/analyze?day=2027-03-08"), (200, {"day": "2027-03-08", "ok": True})
        )
        self.assertEqual(
            self.call("POST", "/api/analyze?day=2027-03-09"), (200, {"day": "2027-03-09", "ok": True})
        )
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["days"], body["last_run"]), (200, 2, NOW))
        self.assertEqual(
            self.call("GET", "/api/days"), (200, {"days": ["2027-03-08", "2027-03-09"], "summary_days": []})
        )
        status, body = self.call("GET", "/api/health")
        self.assertGreaterEqual(body["signatures"], 1)  # the registry was updated
        self.assertGreater(body["storage_bytes"], 0)

    def test_report(self):
        self.assertEqual(self.call("GET", "/api/report/2027-03-09")[0], 404)
        self.assertEqual(self.call("GET", "/api/report/..%2Fstate")[0], 400)
        self.call("POST", "/api/analyze?day=2027-03-09")
        status, body = self.call("GET", "/api/report/2027-03-09")
        self.assertEqual(status, 200)
        self.assertEqual(body["day"], "2027-03-09")
        self.assertIn("energy_kwh", body)

    def test_analyze_errors(self):
        status, body = self.call("POST", "/api/analyze")
        self.assertEqual((status, body["ok"], body["error"]), (400, False, "invalid_day"))
        status, body = self.call("POST", "/api/analyze?day=2027-03-10")
        self.assertEqual((status, body["error"]), (400, "invalid_day"))
        status, body = self.call("POST", "/api/analyze?day=2027-01-01")
        self.assertEqual(
            (status, body["day"], body["ok"], body["error"]), (422, "2027-01-01", False, "no_data")
        )
        self.ha.fail = True
        status, body = self.call("POST", "/api/analyze?day=2027-03-09")
        self.assertEqual((status, body["error"]), (502, "ha_error"))

    def test_settings(self):
        status, body = self.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], {})
        self.assertEqual(body["options"], {"backfill_days": 30})
        self.assertEqual(body["defaults"]["backfill_days"], 30)
        self.assertEqual(body["defaults"]["step_threshold_w"], 300.0)
        self.assertEqual(body["automatic"]["grid_entity"], "sensor.grid_power")

        data = json.dumps({"backfill_days": 5, "grid_entity": "sensor.grid_power", "pv_entity": None})
        status, body = self.call("PUT", "/api/settings", data.encode())
        self.assertEqual(
            (status, body), (200, {"settings": {"backfill_days": 5, "grid_entity": "sensor.grid_power"}})
        )
        self.assertEqual(self.analyzer.backfill_days, 5)
        self.assertEqual(self.call("GET", "/api/settings")[1]["settings"]["backfill_days"], 5)

        status, body = self.call("PUT", "/api/settings", b'{"backfill_days": 999}')
        self.assertEqual((status, body["error"]), (400, "invalid_settings"))
        self.assertIn("backfill_days", body["message"])
        self.assertEqual(self.call("PUT", "/api/settings", b"{")[1]["error"], "invalid_json")
        self.assertEqual(self.analyzer.backfill_days, 5)  # unchanged by the refused requests

        self.assertEqual(self.call("PUT", "/api/settings", b"{}"), (200, {"settings": {}}))
        self.assertEqual(self.analyzer.backfill_days, 30)
        self.assertEqual(self.call("POST", "/api/settings")[0], 405)

    def test_settings_without_home_assistant(self):
        self.ha.fail = True
        status, body = self.call("GET", "/api/settings")
        self.assertEqual((status, body["automatic"]), (200, None))

    def test_unknown(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/api/health")[0], 405)
        self.assertEqual(self.call("GET", "/api/analyze?day=2027-03-09")[0], 405)

    def test_real_server(self):
        server = make_server(self.analyzer, self.store, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{port}"
            req = urllib.request.Request(base + "/api/analyze?day=2027-03-09", method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(json.load(resp), {"day": "2027-03-09", "ok": True})
            with urllib.request.urlopen(base + "/api/days", timeout=10) as resp:
                self.assertEqual(resp.headers["Content-Type"], "application/json")
                self.assertEqual(json.load(resp), {"days": ["2027-03-09"], "summary_days": []})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/report/2027-03-01", timeout=10)
            self.assertEqual(cm.exception.code, 404)
            cm.exception.close()
            req = urllib.request.Request(
                base + "/api/settings",
                method="PUT",
                data=b'{"half_life_days": 20}',
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(json.load(resp), {"settings": {"half_life_days": 20}})
            req = urllib.request.Request(base + "/api/settings", method="PUT", data=b"x" * (65 * 1024))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 413)
            cm.exception.close()
        finally:
            server.shutdown()
            server.server_close()


class AddonManifestTests(unittest.TestCase):
    def test_version_and_options(self):
        text = (ROOT / "energy_mapper" / "config.yaml").read_text()
        self.assertIn(f'version: "{VERSION}"', text)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        cfg = yaml.safe_load(text)
        self.assertEqual(cfg["slug"], "energy_mapper")
        self.assertEqual(cfg["discovery"], ["energy_mapper"])
        self.assertLessEqual(set(cfg["options"]), set(cfg["schema"]))


if __name__ == "__main__":
    unittest.main()
