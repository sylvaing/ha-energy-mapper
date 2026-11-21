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
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

from nilm_core.registry import GROUPS, expected_signature

ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SIGNATURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_APPLIANCES = 200
MAX_SOURCES = 20
MAX_TEXT = 100
MAX_KEY = 200
SOURCE_TYPES = ("switched_load", "signature", "submeter")


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
        if group not in GROUPS:
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
        unknown = set(item) - {"key", "name", "statistic_id", "sources"}
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
        out.append(appliance)
    return out


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
    return out
