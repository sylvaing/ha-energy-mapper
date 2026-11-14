"""Internal HTTP API used by the Energy Mapper integration.

GET  /api/health              {"status": "ok", "version", "last_run", "days",
                               "signatures", "storage_bytes"}
GET  /api/days                {"days": [...], "summary_days": [...]} (ascending)
GET  /api/report/YYYY-MM-DD   the stored report with registry ids (404 if absent)
GET  /api/signatures          the signature registry
POST /api/analyze?day=YYYY-MM-DD
                              {"day", "ok": true} or {"day", "ok": false, "error", "message"}
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from const import VERSION
from ha_client import HAError
from nilm_core.registry import annotate_report, empty_registry
from service import AnalysisError, Analyzer
from storage import ReportStore, parse_day

_LOGGER = logging.getLogger(__name__)

# HTTP status of each AnalysisError code
_STATUS = {"invalid_day": 400, "no_grid": 422, "no_data": 422, "partial_day": 422}


def route(analyzer: Analyzer, store: ReportStore, method: str, target: str) -> tuple[int, dict]:
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
        return 200, {"days": full, "summary_days": [d for d in store.summary_days() if d not in kept]}

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

    known = {"/api/health", "/api/days", "/api/analyze", "/api/signatures"}
    if path in known or path.startswith("/api/report/"):
        return 405, {"error": "method_not_allowed"}
    return 404, {"error": "not_found"}


def make_server(analyzer: Analyzer, store: ReportStore, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"EnergyMapper/{VERSION}"

        def _answer(self, method: str) -> None:
            status, body = route(analyzer, store, method, self.path)
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._answer("GET")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._answer("POST")

        def log_message(self, fmt, *args):
            _LOGGER.debug("%s %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
