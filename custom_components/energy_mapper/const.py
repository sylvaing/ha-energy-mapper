"""Constants of the Energy Mapper integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "energy_mapper"

CONF_URL: Final = "url"
# the entry was created (and is kept up to date) by Supervisor discovery
CONF_DISCOVERED: Final = "discovered"
# entry.options[OPT_SETTINGS]: the settings pushed to the add-on
OPT_SETTINGS: Final = "settings"

DEFAULT_URL: Final = "http://local-energy-mapper:8099"
UPDATE_INTERVAL: Final = timedelta(minutes=5)

# request timeouts (seconds): an analysis reads a whole day of history
TIMEOUT: Final = 30
TIMEOUT_ANALYZE: Final = 600

# settings of the add-on (PUT /api/settings); see energy_mapper/app/settings.py
SENSOR_KEYS: Final = ("grid_entity", "grid_export_entity", "pv_entity", "battery_entity")
LIST_KEYS: Final = ("submeters", "switched_loads")
BOOL_KEYS: Final = ("grid_invert", "battery_invert")
THRESHOLD_KEYS: Final = ("step_threshold_w", "peak_threshold_w")
ANALYSIS_KEYS: Final = (
    "analysis_time",
    "backfill_days",
    "half_life_days",
    "report_retention_days",
    "peak_window_days",
)
