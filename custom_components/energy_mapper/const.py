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
# entry.options[OPT_REVIEW_REPAIR]: notify in Repairs when signatures wait for a review
OPT_REVIEW_REPAIR: Final = "review_repair"
# the Repairs issue "signatures to review" (#10)
ISSUE_REVIEW: Final = "signatures_to_review"
# opens the "To review" tab of the panel
PANEL_REVIEW_PATH: Final = "/energy-mapper?review=1"

# the add-on: its slug is "<repository>_energy_mapper" ("local_energy_mapper" for
# a local copy) and its host name the slug with dashes (#32)
ADDON_SLUG: Final = "energy_mapper"
ADDON_PORT: Final = 8099
UPDATE_INTERVAL: Final = timedelta(minutes=5)

# hourly statistics: days fetched at once from the add-on
HOURLY_CONCURRENCY: Final = 4

# request timeouts (seconds): an analysis reads a whole day of history
TIMEOUT: Final = 30
TIMEOUT_ANALYZE: Final = 600
# the live view (#17): the add-on's long-poll answers within 25 s
LIVE_WAIT: Final = 25
TIMEOUT_LIVE: Final = LIVE_WAIT + 15
# the first add-on with the live view
LIVE_MIN_ADDON: Final = "0.12.0"
# the first add-on that learns from a connected appliance's state (#79)
CONNECTED_MIN_ADDON: Final = "0.14.0"

# settings of the add-on (PUT /api/settings); see energy_mapper/app/settings.py
SENSOR_KEYS: Final = ("grid_entity", "grid_export_entity", "pv_entity", "battery_entity")
LIST_KEYS: Final = ("submeters", "switched_loads")
INVERT_KEYS: Final = ("grid_invert", "battery_invert")
# the noise-adaptive step threshold (#88), shown with the thresholds
ADAPTIVE_THRESHOLD: Final = "adaptive_threshold"
BOOL_KEYS: Final = (*INVERT_KEYS, ADAPTIVE_THRESHOLD)
THRESHOLD_KEYS: Final = ("step_threshold_w", "night_step_threshold_w", "peak_threshold_w")
# entities whose state tells whether an appliance runs
SWITCH_DOMAINS: Final = [
    "switch",
    "input_boolean",
    "light",
    "fan",
    "binary_sensor",
    "climate",
    "humidifier",
    "water_heater",
    "media_player",
]
# a sensor of this device class (an operation state) is linked like a switch,
# with the on-states learned by the entity suggestions (#41)
STATE_SENSOR_CLASS: Final = "enum"
# units of a power sensor that can teach an appliance (#14)
POWER_UNITS: Final = ("W", "kW")
REVIEW_KEYS: Final = (
    "review_min_uses",
    "review_min_days",
    "review_wait_days",
    "review_wait_factor",
    "review_suggest_score",
)
ANALYSIS_KEYS: Final = (
    "analysis_time",
    "backfill_days",
    "half_life_days",
    "report_retention_days",
    "peak_window_days",
    "self_test_devices",
)
