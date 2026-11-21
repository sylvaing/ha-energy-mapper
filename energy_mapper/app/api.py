"""Internal HTTP API used by the Energy Mapper integration.

GET  /api/health              {"status": "ok", "version", "last_run", "days",
                               "signatures", "storage_bytes"}
GET  /api/days                {"days": [...], "summary_days": [...], "generated": {day: ts}}
                              (ascending; generated: when each full report was made)
GET  /api/report/YYYY-MM-DD   the stored report with registry ids (404 if absent)
GET  /api/hourly/YYYY-MM-DD   hourly kWh per appliance (nilm_core.hourly; 404 if no curve)
GET  /api/signatures          the signature registry
POST /api/analyze?day=YYYY-MM-DD
                              {"day", "ok": true} or {"day", "ok": false, "error", "message"}
GET  /api/settings            {"settings", "options", "defaults", "automatic"}
PUT  /api/settings            replaces the settings: {"settings": <as stored>}
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from const import VERSION
from ha_client import HAError
from nilm_core.hourly import hourly_breakdown
from nilm_core.registry import annotate_report, empty_registry
from service import AnalysisError, Analyzer
from settings import KEYS as SETTINGS_KEYS
from settings import SettingsError
from settings import defaults as settings_defaults
from settings import validate as validate_settings
from storage import ReportStore, parse_day

_LOGGER = logging.getLogger(__name__)

# HTTP status of each AnalysisError code
_STATUS = {"invalid_day": 400, "no_grid": 422, "no_data": 422, "partial_day": 422}
MAX_BODY = 64 * 1024


def _settings(analyzer: Analyzer) -> dict:
    try:
        automatic = analyzer.automatic()
    except HAError as err:
        _LOGGER.warning("cannot read the Energy dashboard: %s", err)
        automatic = None
    except Exception:  # never fail the whole answer for it
        _LOGGER.exception("cannot derive the automatic configuration")
        automatic = None
    base = analyzer.base_options
    return {
        "settings": analyzer.settings,
        "options": {k: base[k] for k in SETTINGS_KEYS if k in base},
        "defaults": settings_defaults(base),
        "automatic": automatic,
    }


def route(
    analyzer: Analyzer, store: ReportStore, method: str, target: str, body: bytes = b""
) -> tuple[int, dict]:
    """Answer one request: (HTTP status, JSON body)."""
    parts = urlsplit(target)
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)

    if path == "/api/health" and method == "GET":
        last = store.state().get("last_run")
        registry = store.load_registry() or {}
        return 200, {
            "status": "ok",
            "version": VERSION,
            "last_run": last,
            "days": len(store.days()),
            "signatures": len(registry.get("signatures") or []),
            "storage_bytes": store.size_bytes(),
        }

    if path == "/api/days" and method == "GET":
        full = store.days()
        kept = set(full)
        summaries = store.summaries_all()
        return 200, {
            "days": full,
            "summary_days": [d for d in store.summary_days() if d not in kept],
            "generated": {d: (summaries.get(d) or {}).get("generated_at") for d in full},
        }

    if path == "/api/signatures" and method == "GET":
        return 200, store.load_registry() or empty_registry(analyzer.registry_config)

    if path.startswith("/api/report/") and method == "GET":
        day = path.removeprefix("/api/report/")
        if not parse_day(day):
            return 400, {"error": "invalid_day", "message": "expected YYYY-MM-DD"}
        report = store.load(day)
        if report is None:
            # past the retention of full reports: the summary, without curve
            report = store.load_summary(day)
            if report is None:
                return 404, {"error": "not_found", "message": f"no report for {day}"}
            report["curve"] = []
            report["curve_pruned"] = True
        return 200, annotate_report(report, store.load_registry())

    if path.startswith("/api/hourly/") and method == "GET":
        day = path.removeprefix("/api/hourly/")
        if not parse_day(day):
            return 400, {"error": "invalid_day", "message": "expected YYYY-MM-DD"}
        report = store.load(day)
        if report is None or not report.get("curve"):
            message = f"no per-minute curve for {day}" if store.has(day) else f"no report for {day}"
            return 404, {"error": "not_found", "message": message}
        return 200, hourly_breakdown(report, store.load_registry())

    if path == "/api/analyze" and method == "POST":
        value = (query.get("day") or [""])[0]
        day = parse_day(value)
        if not day:
            return 400, {
                "day": value,
                "ok": False,
                "error": "invalid_day",
                "message": "expected day=YYYY-MM-DD",
            }
        try:
            analyzer.analyze_day(day)
        except AnalysisError as err:
            return _STATUS.get(err.code, 500), {
                "day": value,
                "ok": False,
                "error": err.code,
                "message": err.message,
            }
        except HAError as err:
            return 502, {"day": value, "ok": False, "error": "ha_error", "message": str(err)}
        except Exception as err:  # reported to the caller, logged here
            _LOGGER.exception("analysis of %s failed", value)
            return 500, {"day": value, "ok": False, "error": "internal", "message": str(err)}
        return 200, {"day": value, "ok": True}

    if path == "/api/settings" and method == "GET":
        return 200, _settings(analyzer)

    if path == "/api/settings" and method == "PUT":
        try:
            data = json.loads(body or b"null")
        except ValueError:
            return 400, {"error": "invalid_json", "message": "the body is not JSON"}
        try:
            settings = validate_settings(data)
        except SettingsError as err:
            return 400, {"error": "invalid_settings", "message": str(err)}
        analyzer.set_settings(settings)
        return 200, {"settings": settings}

    known = {"/api/health", "/api/days", "/api/analyze", "/api/signatures", "/api/settings"}
    if path in known or path.startswith(("/api/report/", "/api/hourly/")):
        return 405, {"error": "method_not_allowed"}
    return 404, {"error": "not_found"}


def make_server(analyzer: Analyzer, store: ReportStore, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"EnergyMapper/{VERSION}"

        def _answer(self, method: str, body: bytes = b"") -> None:
            status, answer = route(analyzer, store, method, self.path, body)
            self._send(status, answer)

        def _send(self, status: int, answer: dict) -> None:
            data = json.dumps(answer, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._answer("GET")

        def _body(self) -> bytes | None:
            """The request body, or None (answered) when too large or invalid."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                if MAX_BODY < length <= 16 * MAX_BODY:
                    self.rfile.read(length)  # let the client finish sending
                self.close_connection = True
                self._send(413 if length > 0 else 400, {"error": "invalid_body"})
                return None
            return self.rfile.read(length) if length else b""

        def do_POST(self):  # noqa: N802
            body = self._body()
            if body is not None:
                self._answer("POST", body)

        def do_PUT(self):  # noqa: N802
            body = self._body()
            if body is not None:
                self._answer("PUT", body)

        def log_message(self, fmt, *args):
            _LOGGER.debug("%s %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
