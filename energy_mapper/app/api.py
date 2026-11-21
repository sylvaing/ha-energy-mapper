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
GET  /api/appliances          {"appliances": [...]}: the user's appliances (appliances.py)
PUT  /api/appliances          replaces them: {"appliances": <as stored>}
POST /api/signatures          {"kind", "band", "power_w", "label"}: the signature of an
                              appliance expected before it is seen: {"signature", "created"}
GET  /api/review              {"criteria", "now", "counts", "signatures"}: the signatures to
                              review, waiting and ignored (review.py)
POST /api/review              {"id", "action": validate|wait|ignore|restore, "appliance"?}:
                              {"signature": <with its review_state>}
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from appliances import AppliancesError
from appliances import validate as validate_appliances
from const import VERSION
from ha_client import HAError
from nilm_core.hourly import hourly_breakdown
from nilm_core.registry import GROUPS, annotate_report, empty_registry
from review import ACTIONS as REVIEW_ACTIONS
from review import ReviewError
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


def _expect_signature(analyzer: Analyzer, body: bytes) -> tuple[int, dict]:
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return 400, {"error": "invalid_json", "message": "the body is not JSON"}
    if not isinstance(data, dict):
        return 400, {"error": "invalid_signature", "message": "expected a JSON object"}
    group = (data.get("kind"), data.get("band") or ("session" if data.get("kind") == "cycling" else None))
    power = data.get("power_w")
    label = data.get("label")
    if group not in GROUPS:
        return 400, {"error": "invalid_signature", "message": "kind/band: unknown group"}
    if isinstance(power, bool) or not isinstance(power, int | float) or not 50 <= power <= 100000:
        return 400, {"error": "invalid_signature", "message": "power_w: expected 50 to 100000"}
    if not isinstance(label, str) or not label.strip() or len(label) > 100:
        return 400, {"error": "invalid_signature", "message": "label: expected a text of 1 to 100 characters"}
    try:
        sig, created = analyzer.expect_signature(group[0], group[1], float(power), label.strip())
    except ValueError as err:
        return 409, {
            "error": "conflict",
            "message": f"a signature of another appliance is at this power: {err}",
            "id": str(err),
        }
    return 200, {"signature": sig, "created": created}


def _review_decision(analyzer: Analyzer, body: bytes) -> tuple[int, dict]:
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return 400, {"error": "invalid_json", "message": "the body is not JSON"}
    if not isinstance(data, dict) or set(data) - {"id", "action", "appliance"}:
        return 400, {"error": "invalid_review", "message": 'expected {"id", "action", "appliance"?}'}
    sid, action, appliance = data.get("id"), data.get("action"), data.get("appliance")
    if not isinstance(sid, str) or not sid or len(sid) > 64:
        return 400, {"error": "invalid_review", "message": "id: expected a signature id"}
    if action not in REVIEW_ACTIONS:
        return 400, {
            "error": "invalid_review",
            "message": f"action: expected one of {', '.join(REVIEW_ACTIONS)}",
        }
    if appliance is not None and (
        not isinstance(appliance, str) or not appliance.strip() or len(appliance) > 200
    ):
        return 400, {"error": "invalid_review", "message": "appliance: expected an appliance key"}
    try:
        sig = analyzer.decide(sid, action, appliance.strip() if appliance else None)
    except ReviewError as err:
        return 400, {"error": "invalid_review", "message": str(err)}
    if sig is None:
        return 404, {"error": "not_found", "message": f"no signature {sid}"}
    return 200, {"signature": sig}


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

    if path == "/api/appliances" and method == "GET":
        return 200, {"appliances": analyzer.appliances}

    if path == "/api/appliances" and method == "PUT":
        try:
            data = json.loads(body or b"null")
        except ValueError:
            return 400, {"error": "invalid_json", "message": "the body is not JSON"}
        try:
            appliances = validate_appliances(data)
        except AppliancesError as err:
            return 400, {"error": "invalid_appliances", "message": str(err)}
        analyzer.set_appliances(appliances)
        return 200, {"appliances": appliances}

    if path == "/api/signatures" and method == "POST":
        return _expect_signature(analyzer, body)

    if path == "/api/review" and method == "GET":
        return 200, analyzer.review()

    if path == "/api/review" and method == "POST":
        return _review_decision(analyzer, body)

    known = {
        "/api/health",
        "/api/days",
        "/api/analyze",
        "/api/signatures",
        "/api/settings",
        "/api/appliances",
        "/api/review",
    }
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
