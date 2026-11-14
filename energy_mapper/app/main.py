"""Energy Mapper add-on: nightly analysis service.

Environment:
    SUPERVISOR_TOKEN        set by the Supervisor (add-on mode)
    HA_URL, HA_TOKEN        Home Assistant URL and long-lived token (standalone)
    DATA_DIR                where reports are stored (default /data)
    OPTIONS_FILE            add-on options (default $DATA_DIR/options.json)
    PORT                    HTTP API port (default 8099)
    LOG_LEVEL               overrides the log_level option
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import signal
import socket
import sys
import threading
import time

from api import make_server
from const import DEFAULT_PORT, VERSION
from ha_client import HAClient, HAError, endpoint_from_env, post_discovery
from service import Analyzer, Scheduler
from storage import ReportStore

_LOGGER = logging.getLogger("energy_mapper")


def load_options(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as err:
        _LOGGER.error("invalid options file %s: %s", path, err)
        return {}


def announce(token: str, port: int, stop: threading.Event) -> None:
    host = socket.gethostname()
    for delay in (0, 10, 30, 60, 300):
        if stop.wait(delay):
            return
        try:
            post_discovery(token, host, port)
            _LOGGER.info("announced to Home Assistant as %s:%d", host, port)
            return
        except HAError as err:
            _LOGGER.warning("%s", err)


def main() -> int:
    data_dir = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
    options = load_options(pathlib.Path(os.environ.get("OPTIONS_FILE", data_dir / "options.json")))
    level = (os.environ.get("LOG_LEVEL") or options.get("log_level") or "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s (%(threadName)s) [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    _LOGGER.info("Energy Mapper %s starting", VERSION)

    try:
        endpoint = endpoint_from_env()
    except HAError as err:
        _LOGGER.error("%s", err)
        return 1
    _LOGGER.info("Home Assistant API: %s", endpoint.rest)

    store = ReportStore(data_dir)
    analyzer = Analyzer(HAClient(endpoint), store, options)
    scheduler = Scheduler(analyzer, options.get("analysis_time"))
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    try:
        server = make_server(analyzer, store, "0.0.0.0", port)
    except OSError as err:
        _LOGGER.error("cannot listen on port %d: %s", port, err)
        return 1
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    _LOGGER.info("API listening on port %d, %d report(s) stored", port, len(store.days()))

    if endpoint.supervised:
        threading.Thread(
            target=announce, args=(endpoint.token, port, scheduler.stop), name="discovery", daemon=True
        ).start()

    def _stop(signum, _frame):
        _LOGGER.info("stopping (signal %d)", signum)
        scheduler.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker = threading.Thread(target=scheduler.loop, name="scheduler", daemon=True)
    worker.start()
    while worker.is_alive() and not scheduler.stop.is_set():
        time.sleep(1)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
