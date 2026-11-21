"""Entity suggestions for the signatures to review (#41).

Glue between Home Assistant and ``nilm_core.correlation``:

- **candidates**: the entities whose state can tell when an appliance runs,
  from the states and the entity registry: lights, switches, input
  booleans, fans, climate and water heater entities (their state, the HVAC
  or operation mode; attributes such as ``hvac_action`` are not read),
  media players, binary sensors, and sensors with the ``enum`` device class
  (the operation state of a washing machine...). Left out: diagnostic and
  configuration entities, Energy Mapper's own entities, the entities
  already linked to an appliance or used as a switched load, and the
  switched loads the user excluded. Nothing is specific to a vendor.
- **changes**: the state changes of the candidates on one analysed day,
  read once from the recorder (one history request per day, split in
  chunks) and kept in ``<data>/changes/YYYY-MM-DD.json.gz`` for the
  correlation window. An entity changing too often that day is only
  counted; an entity without any recorded state is ``missing``
  (excluded from the recorder, or created later).
- **suggestions**: computed in the nightly run for the signatures proposed
  for review (and those waiting), stored in ``<data>/suggestions.json``
  (at most ``correlation.CorrelationConfig.top`` entities per signature),
  and added to ``GET /api/review``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from appliances import sources
from nilm_core.correlation import CorrelationConfig, day_changes

# entity domains whose state may tell when an appliance runs
CANDIDATE_DOMAINS = (
    "light",
    "switch",
    "input_boolean",
    "fan",
    "climate",
    "water_heater",
    "media_player",
    "binary_sensor",
    "sensor",  # with the enum device class only
)
# the integration of the same name: its own entities are never candidates
OWN_PLATFORM = "energy_mapper"
# bounds: entities compared, days of changes kept and read per night
MAX_CANDIDATES = 500
CORRELATION_DAYS = 30
SUGGESTIONS_VERSION = 1


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


def candidates(states: list[dict], registry: list[dict], excluded: Iterable[str]) -> list[str]:
    """The entities compared with the signatures (sorted, bounded)."""
    skip = set(excluded)
    entries = {e.get("entity_id"): e for e in registry or [] if isinstance(e, dict)}
    out = []
    for st in states or []:
        entity_id = st.get("entity_id") or ""
        if entity_id in skip or not candidate_domain(entity_id, st.get("attributes")):
            continue
        entry = entries.get(entity_id) or {}
        if entry.get("platform") == OWN_PLATFORM or entry.get("entity_category"):
            continue
        out.append(entity_id)
    return sorted(out)[:MAX_CANDIDATES]


def changes_file(
    day: str,
    histories: Mapping[str, list[tuple[float, str]]],
    entities: Iterable[str],
    start: float,
    end: float,
    config: CorrelationConfig,
) -> dict:
    """What is kept of one day's history of the candidates."""
    kept: dict[str, list] = {}
    chatty: dict[str, int] = {}
    missing: list[str] = []
    for entity in entities:
        points = histories.get(entity)
        if not points:
            missing.append(entity)
            continue
        changes, count = day_changes(points, start, end, config.max_changes_per_day)
        if changes is None:
            chatty[entity] = count
        else:
            kept[entity] = [[ts, s] for ts, s in changes]
    return {"day": day, "start": start, "end": end, "entities": kept, "chatty": chatty, "missing": missing}


def day_changes_of(files: Mapping[str, Mapping]) -> dict[str, dict[str, list | None]]:
    """``{day: {entity: changes or None}}`` for the correlation; a day whose
    history was empty (purged by the recorder) is left out."""
    out: dict[str, dict[str, list | None]] = {}
    for day, f in files.items():
        entities = f.get("entities") or {}
        if not entities and not f.get("chatty"):
            continue
        per = {e: [(float(ts), str(s)) for ts, s in ch] for e, ch in entities.items()}
        per.update(dict.fromkeys(f.get("chatty") or {}))
        out[day] = per
    return out


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


def annotate_listing(listing: dict, stored: Mapping | None, linked: Iterable[str]) -> dict:
    """``GET /api/review`` with the suggestions: ``suggestions`` on each
    listed signature that has some (entities linked since are dropped), and
    ``suggest`` on the listing (when they were computed, the domains without
    recorded history). Modifies and returns ``listing``."""
    stored = stored or {}
    skip = set(linked)
    per_sig = stored.get("signatures") or {}
    for sig in listing.get("signatures") or []:
        found = [c for c in per_sig.get(sig.get("id")) or [] if c.get("entity_id") not in skip]
        if found:
            sig["suggestions"] = found
    listing["suggest"] = {
        "computed_at": stored.get("computed_at"),
        "days": stored.get("days", 0),
        "candidates": stored.get("candidates", 0),
        "min_score": stored.get("min_score"),
        "unrecorded_domains": (stored.get("unrecorded") or {}).get("domains") or [],
        "unrecorded_entities": (stored.get("unrecorded") or {}).get("entities") or 0,
    }
    return listing
