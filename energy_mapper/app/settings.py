"""User settings pushed by the Energy Mapper integration.

The integration keeps the settings in Home Assistant (its options) and pushes
them with ``PUT /api/settings``; they are stored in ``<data>/settings.json``.
A key present there takes precedence over the add-on option of the same name;
a key absent falls back to the add-on option, then to the built-in default
(or, for the sensors, to the Energy dashboard). With no settings pushed, the
add-on behaves exactly as configured by its options.
"""

from __future__ import annotations

import math
import re

ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])(:[0-5][0-9])?$")
MAX_LIST = 100
MAX_NAME = 100

# power sensors: an entity id of the sensor domain, or absent (automatic)
SENSOR_KEYS = ("grid_entity", "grid_export_entity", "pv_entity", "battery_entity")
BOOL_KEYS = ("grid_invert", "battery_invert")
# key: (minimum, maximum, integer)
NUMBER_KEYS = {
    "step_threshold_w": (50, 10000, False),
    "peak_threshold_w": (100, 100000, False),
    "backfill_days": (0, 365, True),
    "half_life_days": (1, 365, True),
    "report_retention_days": (7, 3650, True),
    "peak_window_days": (1, 365, True),
}
KEYS = (
    *SENSOR_KEYS,
    *BOOL_KEYS,
    "submeters",
    "switched_loads",
    "excluded_switched_loads",
    *NUMBER_KEYS,
    "analysis_time",
)

# used for a key that is neither in the settings nor in the add-on options
# (step_threshold_w: the default of the analysis; peak_threshold_w: None means
# relative to the recent maximum)
DEFAULTS: dict = {
    "analysis_time": "04:30",
    "backfill_days": 30,
    "half_life_days": 30,
    "report_retention_days": 90,
    "peak_window_days": 30,
    "grid_invert": False,
    "battery_invert": False,
    "step_threshold_w": 300.0,
    "peak_threshold_w": None,
}


class SettingsError(ValueError):
    """Invalid settings; the message names the offending key."""


def _entity(key: str, value, domain: str | None = None) -> str:
    if not isinstance(value, str) or not ENTITY_RE.match(value.strip()):
        raise SettingsError(f"{key}: expected an entity id, got {value!r}")
    value = value.strip()
    if domain and not value.startswith(domain + "."):
        raise SettingsError(f"{key}: expected a {domain} entity, got {value!r}")
    return value


def _list(key: str, value) -> list:
    if not isinstance(value, list):
        raise SettingsError(f"{key}: expected a list")
    if len(value) > MAX_LIST:
        raise SettingsError(f"{key}: at most {MAX_LIST} items")
    return value


def _switched_load(i: int, item) -> dict:
    where = f"switched_loads[{i}]"
    if not isinstance(item, dict):
        raise SettingsError(f"{where}: expected an object")
    unknown = set(item) - {"entity_id", "name", "on_states"}
    if unknown:
        raise SettingsError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    out = {"entity_id": _entity(f"{where}.entity_id", item.get("entity_id"))}
    name = item.get("name")
    if name is not None:
        if not isinstance(name, str) or len(name) > MAX_NAME:
            raise SettingsError(f"{where}.name: expected a text of at most {MAX_NAME} characters")
        if name.strip():
            out["name"] = name.strip()
    states = item.get("on_states")
    if states is not None:
        states = _list(f"{where}.on_states", states)
        if not all(isinstance(s, str) and s.strip() and len(s) <= MAX_NAME for s in states):
            raise SettingsError(f"{where}.on_states: expected a list of states")
        states = list(dict.fromkeys(s.strip() for s in states))
        if states:
            out["on_states"] = states
    return out


def validate(data) -> dict:
    """Check and normalise settings; ``None`` values and empty lists are
    dropped (the key then falls back to the add-on options)."""
    if not isinstance(data, dict):
        raise SettingsError("expected a JSON object")
    unknown = set(data) - set(KEYS)
    if unknown:
        raise SettingsError(f"unknown key(s): {', '.join(sorted(unknown))}")
    out: dict = {}
    for key, value in data.items():
        if value is None or value == "" or value == []:
            continue
        if key in SENSOR_KEYS:
            out[key] = _entity(key, value, "sensor")
        elif key in BOOL_KEYS:
            if not isinstance(value, bool):
                raise SettingsError(f"{key}: expected true or false")
            out[key] = value
        elif key in NUMBER_KEYS:
            low, high, integer = NUMBER_KEYS[key]
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise SettingsError(f"{key}: expected a number")
            if integer and value != int(value):
                raise SettingsError(f"{key}: expected a whole number")
            if not low <= value <= high:
                raise SettingsError(f"{key}: expected a value between {low} and {high}")
            out[key] = int(value) if integer else float(value)
        elif key == "analysis_time":
            match = TIME_RE.match(value) if isinstance(value, str) else None
            if not match:
                raise SettingsError(f"{key}: expected HH:MM")
            out[key] = f"{match.group(1)}:{match.group(2)}"
        elif key == "submeters":
            items = [_entity(f"submeters[{i}]", v, "sensor") for i, v in enumerate(_list(key, value))]
            out[key] = list(dict.fromkeys(items))
        elif key == "switched_loads":
            loads = [_switched_load(i, v) for i, v in enumerate(_list(key, value))]
            seen = [x["entity_id"] for x in loads]
            if len(set(seen)) != len(seen):
                raise SettingsError("switched_loads: the same entity is listed twice")
            out[key] = loads
        elif key == "excluded_switched_loads":
            items = [_entity(f"{key}[{i}]", v) for i, v in enumerate(_list(key, value))]
            out[key] = list(dict.fromkeys(items))
    return out


def merge(options: dict, settings: dict) -> dict:
    """The add-on options with the settings on top."""
    return {**options, **settings}


def defaults(options: dict) -> dict:
    """Values of the scalar keys when they are not in the settings: the add-on
    option when set, else the built-in default."""
    out = {}
    for key, default in DEFAULTS.items():
        value = options.get(key)
        out[key] = default if value is None or value == "" else value
    return out
