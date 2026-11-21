"""Entity suggestions for the signatures to review (#41).

Glue between Home Assistant and ``nilm_core.correlation``:

- **candidates**: the entities whose state can tell when an appliance runs,
  from the states and the entity registry: lights, switches, input
  booleans, fans, climate, humidifier and water heater entities (their
  state: the HVAC or operation mode), media players, binary sensors, and
  sensors with the ``enum`` device class (the operation state of a washing
  machine...). Left out: diagnostic and configuration entities, Energy
  Mapper's own entities, the meters and sub-meters the analysis uses, and
  the switched loads the user excluded. The switched loads and the entities
  linked to an appliance are candidates (#43): a signature matching one of
  them is part of that appliance, and the panel proposes to merge it.
  Nothing is specific to a vendor.
- **attribute candidates** (#43): a few "what is it doing" attributes, read
  as if they were states, for the entities of their domain only
  (``ATTRIBUTE_CANDIDATES``: ``climate.hvac_action``, ``humidifier.action``).
  A thermostat's HVAC mode stays ``heat`` all winter; its ``hvac_action``
  tells when it actually heats. Their history is read in a separate request
  with attributes, bounded to ``MAX_ATTRIBUTE_ENTITIES`` entities. Their key
  is ``<entity_id>::<attribute>``; a suggestion carries ``entity_id`` and
  ``attribute``.
- **changes**: the state changes of the candidates on one analysed day,
  read once from the recorder (one history request per day, split in
  chunks, plus one for the attributes) and kept in
  ``<data>/changes/YYYY-MM-DD.json.gz`` for the correlation window, with
  the state at the start of the day (``initial``, version 2: containment
  needs the periods, not only the changes). An entity changing too often
  that day is only counted; an entity without any recorded state is
  ``missing`` (excluded from the recorder, or created later).
- **suggestions**: computed in the nightly run for the signatures proposed
  for review (and those waiting), stored in ``<data>/suggestions.json``
  (at most ``correlation.CorrelationConfig.top`` entities per signature),
  and added to ``GET /api/review``, with the appliance a suggested entity
  already belongs to (``appliance``: it is one of its sources, or shares a
  Home Assistant device with one).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from appliances import sources
from nilm_core.correlation import CorrelationConfig, day_states

# entity domains whose state may tell when an appliance runs
CANDIDATE_DOMAINS = (
    "light",
    "switch",
    "input_boolean",
    "fan",
    "climate",
    "humidifier",
    "water_heater",
    "media_player",
    "binary_sensor",
    "sensor",  # with the enum device class only
)
# attributes read as states (#43), per domain: what the entity is doing now,
# not its setting. Left out: a fan's percentage (a speed setting; the fan's
# state already tells when it runs) and a water heater's operation mode (its
# state already); no standard attribute says when a water heater heats.
ATTRIBUTE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "climate": ("hvac_action",),
    "humidifier": ("action",),
}
# key of an attribute series: "<entity_id>::<attribute>"
ATTRIBUTE_SEP = "::"
# the integration of the same name: its own entities are never candidates
OWN_PLATFORM = "energy_mapper"
# bounds: entities compared, days of changes kept and read per night
MAX_CANDIDATES = 500
MAX_ATTRIBUTE_ENTITIES = 50
CORRELATION_DAYS = 30
SUGGESTIONS_VERSION = 2
# changes files: 2 adds the state at the start of the day and the attributes
CHANGES_VERSION = 2


def attribute_key(entity_id: str, attribute: str) -> str:
    return f"{entity_id}{ATTRIBUTE_SEP}{attribute}"


def split_key(key: str) -> tuple[str, str | None]:
    """``(entity_id, attribute or None)`` of a series key."""
    entity_id, sep, attribute = key.partition(ATTRIBUTE_SEP)
    return entity_id, (attribute if sep else None)


def attribute_keys(entities: Iterable[str]) -> list[str]:
    """The attribute series of ``entities`` (``ATTRIBUTE_CANDIDATES``)."""
    return [attribute_key(e, a) for e in entities for a in ATTRIBUTE_CANDIDATES.get(e.split(".", 1)[0], ())]


def candidate_domain(entity_id: str, attributes: Mapping | None) -> bool:
    domain = entity_id.split(".", 1)[0]
    if domain not in CANDIDATE_DOMAINS:
        return False
    return domain != "sensor" or (attributes or {}).get("device_class") == "enum"


def linked_entities(appliances: Iterable[Mapping]) -> set[str]:
    """The entities already sources of an appliance (switched loads and
    sub-meters)."""
    out: set[str] = set()
    for kind in ("switched_load", "submeter"):
        out |= {s["entity_id"] for _, s in sources(appliances, kind)}
    return out


def candidates(
    states: list[dict], registry: list[dict], excluded: Iterable[str], attributes: bool = False
) -> list[str]:
    """The entities compared with the signatures (sorted, bounded); with
    ``attributes``, those whose attributes are compared
    (``ATTRIBUTE_CANDIDATES``)."""
    skip = set(excluded)
    entries = {e.get("entity_id"): e for e in registry or [] if isinstance(e, dict)}
    out = []
    for st in states or []:
        entity_id = st.get("entity_id") or ""
        if entity_id in skip:
            continue
        if attributes:
            if entity_id.split(".", 1)[0] not in ATTRIBUTE_CANDIDATES:
                continue
        elif not candidate_domain(entity_id, st.get("attributes")):
            continue
        entry = entries.get(entity_id) or {}
        if entry.get("platform") == OWN_PLATFORM or entry.get("entity_category"):
            continue
        out.append(entity_id)
    return sorted(out)[: MAX_ATTRIBUTE_ENTITIES if attributes else MAX_CANDIDATES]


def devices(registry: list[dict], entities: Iterable[str]) -> dict[str, str]:
    """``{entity_id: device_id}`` of ``entities`` that belong to a device."""
    wanted = set(entities)
    return {
        e["entity_id"]: e["device_id"]
        for e in registry or []
        if isinstance(e, dict) and e.get("entity_id") in wanted and e.get("device_id")
    }


def changes_file(
    day: str,
    histories: Mapping[str, list[tuple[float, str]]],
    entities: Iterable[str],
    start: float,
    end: float,
    config: CorrelationConfig,
    attribute_histories: Mapping[str, list[tuple[float, str]]] | None = None,
) -> dict:
    """What is kept of one day's history of the candidates: their changes,
    and their state at ``start``. ``attribute_histories``: the attribute
    series (keyed ``<entity_id>::<attribute>``), all of them (an entity
    without history has no key)."""
    kept: dict[str, list] = {}
    initial: dict[str, str] = {}
    chatty: dict[str, int] = {}
    missing: list[str] = []
    series = [(e, histories.get(e), config.max_changes_per_day) for e in entities]
    series += [(k, v, config.max_attribute_changes_per_day) for k, v in (attribute_histories or {}).items()]
    for key, points, limit in series:
        if not points:
            if split_key(key)[1] is None:
                missing.append(key)
            continue
        first, changes, count = day_states(points, start, end, limit)
        if first is not None:
            initial[key] = first
        if changes is None:
            chatty[key] = count
        else:
            kept[key] = [[ts, s] for ts, s in changes]
    return {
        "version": CHANGES_VERSION,
        "day": day,
        "start": start,
        "end": end,
        "entities": kept,
        "initial": initial,
        "chatty": chatty,
        "missing": missing,
    }


def has_history(data: Mapping) -> bool:
    """Whether a changes file holds any recorded state (else purged)."""
    return bool(data.get("entities") or data.get("chatty"))


def day_changes_of(files: Mapping[str, Mapping]) -> dict[str, dict[str, list | None]]:
    """``{day: {entity: changes or None}}`` for the correlation; a day whose
    history was empty (purged by the recorder) is left out."""
    out: dict[str, dict[str, list | None]] = {}
    for day, f in files.items():
        if not has_history(f):
            continue
        entities = f.get("entities") or {}
        per = {e: [(float(ts), str(s)) for ts, s in ch] for e, ch in entities.items()}
        per.update(dict.fromkeys(f.get("chatty") or {}))
        out[day] = per
    return out


def day_initial_of(files: Mapping[str, Mapping]) -> dict[str, dict[str, str]]:
    """``{day: {entity: state at the start of the day}}`` (empty for the
    files written before version 2)."""
    return {day: dict(f.get("initial") or {}) for day, f in files.items()}


def unrecorded(files: Mapping[str, Mapping], entities: Iterable[str]) -> dict:
    """The candidates without any recorded state on the days that have some
    history: ``{"entities": n, "domains": [domains none of whose
    candidates is recorded]}``."""
    useful = [f for f in files.values() if f.get("entities") or f.get("chatty")]
    entities = list(entities)
    if not useful or not entities:
        return {"entities": 0, "domains": []}
    recorded = set()
    for f in useful:
        recorded |= set(f.get("entities") or {}) | set(f.get("chatty") or {})
    missing = [e for e in entities if e not in recorded]
    by_domain: dict[str, list[bool]] = {}
    for e in entities:
        by_domain.setdefault(e.split(".", 1)[0], []).append(e in recorded)
    domains = sorted(d for d, flags in by_domain.items() if not any(flags))
    return {"entities": len(missing), "domains": domains}


def public_candidates(result: Mapping[str, list[dict]]) -> dict[str, list[dict]]:
    """The correlation's candidates with their key split: ``entity_id`` and,
    for an attribute series, ``attribute``."""
    out: dict[str, list[dict]] = {}
    for sid, found in result.items():
        out[sid] = []
        for c in found:
            entity_id, attribute = split_key(c["entity_id"])
            item = {**c, "entity_id": entity_id}
            if attribute:
                item["attribute"] = attribute
            out[sid].append(item)
    return out


def owners(appliances: Iterable[Mapping], device_of: Mapping[str, str]) -> dict[str, tuple[Mapping, str]]:
    """``{entity_id: (appliance, "entity" | "device")}``: the entities that
    are a source of an appliance (switched load, sub-meter), then those that
    share a Home Assistant device with one."""
    direct: dict[str, Mapping] = {}
    for kind in ("switched_load", "submeter"):
        for a, s in sources(appliances, kind):
            direct.setdefault(s["entity_id"], a)
    by_device: dict[str, Mapping] = {}
    for entity_id, a in direct.items():
        device = device_of.get(entity_id)
        if device:
            by_device.setdefault(device, a)
    out: dict[str, tuple[Mapping, str]] = {
        e: (a, "device") for e, d in device_of.items() if (a := by_device.get(d))
    }
    out.update({e: (a, "entity") for e, a in direct.items()})
    return out


def annotate_listing(listing: dict, stored: Mapping | None, appliances: Iterable[Mapping] = ()) -> dict:
    """``GET /api/review`` with the suggestions: ``suggestions`` on each
    listed signature that has some, and ``suggest`` on the listing (when
    they were computed, the domains without recorded history). A suggested
    entity that belongs to an appliance (``owners``) gets ``appliance``:
    ``{key, name, via}`` (the panel proposes to merge the signature into
    it). Modifies and returns ``listing``."""
    stored = stored or {}
    owned = owners(appliances, stored.get("devices") or {})
    per_sig = stored.get("signatures") or {}
    for sig in listing.get("signatures") or []:
        found = []
        for c in per_sig.get(sig.get("id")) or []:
            c = dict(c)
            owner = owned.get(c.get("entity_id"))
            if owner is not None:
                c["appliance"] = {"key": owner[0]["key"], "name": owner[0]["name"], "via": owner[1]}
            found.append(c)
        if found:
            sig["suggestions"] = found
    listing["suggest"] = {
        "computed_at": stored.get("computed_at"),
        "days": stored.get("days", 0),
        "candidates": stored.get("candidates", 0),
        "attribute_candidates": stored.get("attribute_candidates", 0),
        "min_score": stored.get("min_score"),
        "unrecorded_domains": (stored.get("unrecorded") or {}).get("domains") or [],
        "unrecorded_entities": (stored.get("unrecorded") or {}).get("entities") or 0,
    }
    return listing
