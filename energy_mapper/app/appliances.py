"""The user's appliances, declared by the Energy Mapper integration.

Home Assistant is the source of truth: each appliance is a config subentry
of the integration (name and link), and the integration pushes the whole
list with ``PUT /api/appliances`` after every change. The add-on stores it in
``<data>/appliances.json`` and applies it:

- **signature sources** (``{"type": "signature", "id"}``): the registry
  signature gets the appliance's name as ``label`` (and its key as
  ``appliance``), so it is never forgotten and wins the nightly matching.
  Labels of signatures no appliance declares are cleared: deleting an
  appliance in Home Assistant forgets its label. A declared signature
  missing from the registry (expected before it is seen, or a registry
  rebuilt from scratch) is recreated from the declared group and power.
- **switched-load sources** (``{"type": "switched_load", "entity_id",
  "on_states"?}``): the relay is analysed as a switched load named after the
  appliance, whatever the Energy dashboard says (on-states: the declared
  ones, else those of the settings or the default).
- **sub-meter sources** (``{"type": "submeter", "entity_id"}``): the power
  sensor is subtracted before the analysis, in addition to the sub-meters of
  the settings or of the Energy dashboard.

Several sources make one appliance (merged appliances): the integration adds
their energy; the add-on only needs to know each source.

An appliance may also have a **teacher** (``{"entity_id", "since"?}``, #14):
a power sensor that measures it for a while (a temporary smart plug). The
sensor is not subtracted (the appliance stays in the whole-house curve, as
it will once the plug is gone), and each analysed day keeps the sensor's
runs as the ground truth of the appliance (``teaching.py``). A connected
appliance (#79) teaches from its operation state instead: ``{"entity_id",
"since"?, "mode": "state", "on_states"?, "phase_entity"?, "energy_entity"?,
"program_entity"?}``; nothing is hidden from the analysis, the entities are
read and each day keeps the runs of the state measured on the house curve.

An appliance may also have **labelled examples** (``examples``, #17): at
most ``MAX_EXAMPLES`` uses the user pointed at in the live view, ``{"t_on",
"t_off" (null while the stop is not known), "power_w", "source": "live" |
"guided", "created", "signature_id"? (the signature named at the time),
"live"? ({"id", "session"}: the live event, while open)}``. The nightly
analysis finds them again in the day summaries (``examples.py``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

from nilm_core.registry import SOURCE_GROUPS, expected_signature

ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SIGNATURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_APPLIANCES = 200
MAX_SOURCES = 20
MAX_TEXT = 100
MAX_KEY = 200
SOURCE_TYPES = ("switched_load", "signature", "submeter")
MAX_EXAMPLES = 20
EXAMPLE_SOURCES = ("live", "guided")
MAX_EXAMPLE_S = 86400.0  # one use lasts at most a day


class AppliancesError(ValueError):
    """Invalid appliances; the message names the offending item."""


def _text(where: str, value, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AppliancesError(f"{where}: expected a text of 1 to {limit} characters")
    return value.strip()


def _entity(where: str, value, domain: str | None = None) -> str:
    if not isinstance(value, str) or not ENTITY_RE.match(value):
        raise AppliancesError(f"{where}: expected an entity id, got {value!r}")
    if domain and not value.startswith(domain + "."):
        raise AppliancesError(f"{where}: expected a {domain} entity, got {value!r}")
    return value


def _source(where: str, item) -> dict:
    if not isinstance(item, dict):
        raise AppliancesError(f"{where}: expected an object")
    kind = item.get("type")
    if kind not in SOURCE_TYPES:
        raise AppliancesError(f"{where}.type: expected one of {', '.join(SOURCE_TYPES)}")
    allowed = {
        "switched_load": {"type", "entity_id", "on_states"},
        "signature": {"type", "id", "kind", "band", "power_w"},
        "submeter": {"type", "entity_id"},
    }[kind]
    unknown = set(item) - allowed
    if unknown:
        raise AppliancesError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    if kind == "submeter":
        return {"type": kind, "entity_id": _entity(f"{where}.entity_id", item.get("entity_id"), "sensor")}
    if kind == "switched_load":
        out: dict = {"type": kind, "entity_id": _entity(f"{where}.entity_id", item.get("entity_id"))}
        states = item.get("on_states")
        if states is not None:
            if not isinstance(states, list) or len(states) > MAX_SOURCES:
                raise AppliancesError(f"{where}.on_states: expected a list of states")
            states = list(dict.fromkeys(_text(f"{where}.on_states", s) for s in states))
            if states:
                out["on_states"] = states
        return out
    sid = item.get("id")
    if not isinstance(sid, str) or not SIGNATURE_ID_RE.match(sid):
        raise AppliancesError(f"{where}.id: expected a signature id, got {sid!r}")
    out = {"type": kind, "id": sid}
    group = (item.get("kind"), item.get("band"))
    power = item.get("power_w")
    if group != (None, None) or power is not None:
        # how to recreate it when the registry does not have it
        if group not in SOURCE_GROUPS:
            raise AppliancesError(f"{where}: unknown signature kind/band {group}")
        if isinstance(power, bool) or not isinstance(power, int | float) or not math.isfinite(power):
            raise AppliancesError(f"{where}.power_w: expected a number")
        if not 1 <= power <= 100000:
            raise AppliancesError(f"{where}.power_w: expected a value between 1 and 100000")
        out.update({"kind": group[0], "band": group[1], "power_w": float(power)})
    return out


def validate(data) -> list[dict]:
    """Check and normalise ``{"appliances": [...]}``; returns the list."""
    if not isinstance(data, dict) or set(data) != {"appliances"}:
        raise AppliancesError('expected {"appliances": [...]}')
    items = data["appliances"]
    if not isinstance(items, list) or len(items) > MAX_APPLIANCES:
        raise AppliancesError(f"appliances: expected a list of at most {MAX_APPLIANCES} items")
    out: list[dict] = []
    keys: set[str] = set()
    owners: dict[tuple[str, str], str] = {}
    for i, item in enumerate(items):
        where = f"appliances[{i}]"
        if not isinstance(item, dict):
            raise AppliancesError(f"{where}: expected an object")
        unknown = set(item) - {"key", "name", "statistic_id", "sources", "teacher", "examples"}
        if unknown:
            raise AppliancesError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        key = _text(f"{where}.key", item.get("key"), MAX_KEY)
        if key in keys:
            raise AppliancesError(f"{where}.key: {key!r} is used twice")
        keys.add(key)
        appliance = {"key": key, "name": _text(f"{where}.name", item.get("name"))}
        if item.get("statistic_id") is not None:
            appliance["statistic_id"] = _text(f"{where}.statistic_id", item["statistic_id"], MAX_KEY)
        sources = item.get("sources")
        if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
            raise AppliancesError(f"{where}.sources: expected a list of at most {MAX_SOURCES} items")
        appliance["sources"] = [_source(f"{where}.sources[{j}]", s) for j, s in enumerate(sources)]
        for s in appliance["sources"]:
            ident = (s["type"], s.get("entity_id") or s.get("id"))
            if ident in owners:
                raise AppliancesError(f"{where}: {ident[1]} is already a source of {owners[ident]!r}")
            owners[ident] = key
        if item.get("teacher") is not None:
            appliance["teacher"] = _teacher(f"{where}.teacher", item["teacher"])
            ident = ("teacher", appliance["teacher"]["entity_id"])
            if ident in owners:
                raise AppliancesError(f"{where}: {ident[1]} already teaches {owners[ident]!r}")
            owners[ident] = key
        examples = item.get("examples")
        if examples is not None:
            if not isinstance(examples, list) or len(examples) > MAX_EXAMPLES:
                raise AppliancesError(f"{where}.examples: expected a list of at most {MAX_EXAMPLES} items")
            if examples:
                appliance["examples"] = [
                    _example(f"{where}.examples[{j}]", e) for j, e in enumerate(examples)
                ]
        out.append(appliance)
    return out


def _number(where: str, value, lo: float = -math.inf, hi: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise AppliancesError(f"{where}: expected a number")
    if not lo <= value <= hi:
        raise AppliancesError(f"{where}: expected a value between {lo:g} and {hi:g}")
    return float(value)


def _example(where: str, item) -> dict:
    """A labelled example (#17): one use of the appliance, from the live view."""
    if not isinstance(item, dict):
        raise AppliancesError(f"{where}: expected an object")
    unknown = set(item) - {"t_on", "t_off", "power_w", "source", "created", "signature_id", "live"}
    if unknown:
        raise AppliancesError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    out: dict = {"t_on": _number(f"{where}.t_on", item.get("t_on"), 0)}
    t_off = item.get("t_off")
    out["t_off"] = None
    if t_off is not None:
        out["t_off"] = _number(f"{where}.t_off", t_off, out["t_on"], out["t_on"] + MAX_EXAMPLE_S)
    out["power_w"] = _number(f"{where}.power_w", item.get("power_w"), 1, 100000)
    if item.get("source") not in EXAMPLE_SOURCES:
        raise AppliancesError(f"{where}.source: expected one of {', '.join(EXAMPLE_SOURCES)}")
    out["source"] = item["source"]
    out["created"] = _number(f"{where}.created", item.get("created"), 0)
    sid = item.get("signature_id")
    if sid is not None:
        if not isinstance(sid, str) or not SIGNATURE_ID_RE.match(sid):
            raise AppliancesError(f"{where}.signature_id: expected a signature id, got {sid!r}")
        out["signature_id"] = sid
    live = item.get("live")
    if live is not None:
        if not isinstance(live, dict) or set(live) != {"id", "session"}:
            raise AppliancesError(f'{where}.live: expected {{"id", "session"}}')
        out["live"] = {k: int(_number(f"{where}.live.{k}", live[k], 0)) for k in ("id", "session")}
    return out


def examples_of(appliances: Iterable[Mapping]) -> list[tuple[Mapping, list[Mapping]]]:
    """(appliance, examples) of the appliances with labelled examples."""
    return [(a, a["examples"]) for a in appliances or () if a.get("examples")]


TEACHER_MODES = ("power", "state")
_STATE_TEACHER_KEYS = {"mode", "on_states", "phase_entity", "energy_entity", "program_entity"}


def _teacher(where: str, item) -> dict:
    if not isinstance(item, dict) or set(item) - {"entity_id", "since"} - _STATE_TEACHER_KEYS:
        raise AppliancesError(
            f'{where}: expected {{"entity_id", "since"?, "mode"?, "on_states"?, "phase_entity"?, '
            '"energy_entity"?, "program_entity"?}'
        )
    mode = item.get("mode") or "power"
    if mode not in TEACHER_MODES:
        raise AppliancesError(f"{where}.mode: expected one of {', '.join(TEACHER_MODES)}")
    if mode == "power":
        if set(item) & (_STATE_TEACHER_KEYS - {"mode"}):
            raise AppliancesError(f"{where}: a power sensor teacher has no state entities")
        out = {"entity_id": _entity(f"{where}.entity_id", item.get("entity_id"), "sensor")}
    else:
        # a connected appliance (#79): its operation state, and optionally its
        # phase state, energy counter and programme state
        out = {"entity_id": _entity(f"{where}.entity_id", item.get("entity_id")), "mode": "state"}
        states = item.get("on_states")
        if states is not None:
            if not isinstance(states, list) or len(states) > MAX_SOURCES:
                raise AppliancesError(f"{where}.on_states: expected a list of states")
            states = list(dict.fromkeys(_text(f"{where}.on_states", s) for s in states))
        if not states:
            raise AppliancesError(f"{where}.on_states: the states in which the appliance runs are needed")
        out["on_states"] = states
        for key, domain in (("phase_entity", None), ("energy_entity", "sensor"), ("program_entity", None)):
            if item.get(key) is not None:
                out[key] = _entity(f"{where}.{key}", item[key], domain)
    since = item.get("since")
    if since is not None:
        if isinstance(since, bool) or not isinstance(since, int | float) or not math.isfinite(since):
            raise AppliancesError(f"{where}.since: expected a unix time")
        out["since"] = float(since)
    return out


def teacher_entities(appliances: Iterable[Mapping]) -> list[str]:
    """The entities the connected appliances' teachers need read (#79):
    their operation state, phase state, programme state and energy counter."""
    out: list[str] = []
    for _, t in teachers(appliances):
        if t.get("mode") == "state":
            out += [
                t[k] for k in ("entity_id", "phase_entity", "program_entity", "energy_entity") if t.get(k)
            ]
    return list(dict.fromkeys(out))


def teachers(appliances: Iterable[Mapping]) -> list[tuple[Mapping, Mapping]]:
    """(appliance, teacher) of the appliances learning from a sensor."""
    return [(a, a["teacher"]) for a in appliances or () if a.get("teacher")]


def sources(appliances: Iterable[Mapping], kind: str) -> Iterable[tuple[Mapping, Mapping]]:
    """(appliance, source) for the sources of one type."""
    for a in appliances or ():
        for s in a.get("sources") or ():
            if s.get("type") == kind:
                yield a, s


def apply_to_registry(registry: dict, appliances: list[Mapping], now: float) -> bool:
    """Labels of the registry signatures from the declared appliances
    (cleared when no appliance declares them); declared signatures missing
    from the registry are recreated as expected signatures. Modifies
    ``registry``; returns whether something changed."""
    declared = {s["id"]: (a, s) for a, s in sources(appliances, "signature")}
    changed = False
    sigs = registry.setdefault("signatures", [])
    for sig in sigs:
        a = declared.get(sig.get("id"), (None, None))[0]
        label, key = (a["name"], a["key"]) if a else (None, None)
        if sig.get("label") != label or sig.get("appliance") != key:
            sig["label"], sig["appliance"] = label, key
            changed = True
    present = {s.get("id") for s in sigs}
    for sid, (a, s) in declared.items():
        if sid in present or "power_w" not in s:
            continue
        sig = expected_signature(registry, s["kind"], s["band"], s["power_w"], a["name"], now)
        sig["id"] = sid  # the identity the appliance knows
        sig["appliance"] = a["key"]
        sigs.append(sig)
        changed = True
    return changed


def analysis_options(options: dict, appliances: list[Mapping]) -> dict:
    """``derive()`` options (from ``service.derive_options``) with the
    declared switched loads and sub-meters. A declared relay is a manual
    switched load named after its appliance, keeping the on-states of the
    settings unless the appliance declares its own."""
    out = dict(options)
    loads = {x["entity_id"]: dict(x) for x in options.get("switched_loads") or []}
    for a, s in sources(appliances, "switched_load"):
        load = loads.setdefault(s["entity_id"], {"entity_id": s["entity_id"], "on_states": ["on"]})
        load["name"] = a["name"]
        if s.get("on_states"):
            load["on_states"] = list(s["on_states"])
    if loads:
        out["switched_loads"] = list(loads.values())
    extra = [(s["entity_id"], a["name"]) for a, s in sources(appliances, "submeter")]
    if extra:
        out["extra_submeters"] = extra
    taught = [t["entity_id"] for _, t in teachers(appliances) if t.get("mode") != "state"]
    if taught:
        # never subtracted while they teach (#14), whatever else declares them
        out["teachers"] = taught
    return out
