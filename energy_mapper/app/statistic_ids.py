"""Ids of the statistics the Energy Mapper integration publishes.

The integration imports the hourly energy of each appliance the add-on knows
as an external statistic ``energy_mapper:<prefix>_<slug>_energy``:

- ``load_<slug of the entity id>`` for a switched load (relay, pilot wire);
- ``signature_<slug of the registry id>`` for a labelled signature.

Users may add these statistics to the Energy dashboard, in place of the
estimate they had before. The automatic configuration then has to recognise
them and map them back to their appliance: this module is the add-on side of
that contract. :func:`statistic_id` must give the same ids as the
integration's ``appliances.statistic_id`` (checked by the integration tests).

The mapping ``statistic id -> appliance`` is kept by the add-on
(:func:`known_statistics`, stored in ``statistic_ids.json``): it is built from
the registry with the same rule as the integration's appliances, and an entry
survives the registry forgetting its appliance (a radiator idle all summer),
so the learned polarity of a relay is not lost.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

DOMAIN = "energy_mapper"
PREFIX = f"{DOMAIN}:"
KIND_LOAD = "load"
KIND_SIGNATURE = "signature"
_SUFFIX = "_energy"
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Home Assistant's ``slugify`` for the characters of entity ids and
    registry ids (ASCII letters, digits, ``.``, ``_``, ``-``)."""
    if not text:
        return ""
    return _NOT_SLUG.sub("_", text.lower()).strip("_") or "unknown"


def statistic_id(kind: str, source_id: str) -> str:
    """``kind``: :data:`KIND_LOAD` (``source_id`` = entity id) or
    :data:`KIND_SIGNATURE` (``source_id`` = registry id)."""
    return f"{PREFIX}{kind}_{slugify(source_id)}{_SUFFIX}"


def is_own(stat_id: str | None) -> bool:
    """A statistic published by Energy Mapper (not an entity)."""
    return bool(stat_id) and stat_id.startswith(PREFIX)


def kind_of(stat_id: str) -> str | None:
    """:data:`KIND_LOAD`, :data:`KIND_SIGNATURE` or None (not one of ours, or
    a form this version does not know)."""
    if not is_own(stat_id):
        return None
    rest = stat_id[len(PREFIX) :]
    for kind in (KIND_LOAD, KIND_SIGNATURE):
        if rest.startswith(kind + "_") and rest.endswith(_SUFFIX):
            return kind
    return None


def known_statistics(
    registry: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None = None,
    appliances: Iterable[Mapping[str, Any]] | None = None,
) -> dict:
    """``{statistic id: {"kind", "source_id", "name", "on_states"}}``: the
    statistics the integration publishes for the appliances of ``registry``
    (every learned switched load, every labelled signature), over the
    ``previous`` mapping (entries of appliances the registry forgot are kept;
    ``on_states`` only for switched loads, empty when not learned yet).

    The ``appliances`` declared by the integration come last and win: their
    statistic id keeps its identity across renames, re-links and merges, so
    its entry names the appliance (``appliance``: its key) and its current
    first relay (``kind`` load) or signature. Only current appliances carry
    ``appliance``: the marker of a deleted one is dropped."""
    out: dict[str, dict] = {}
    for k, v in (previous or {}).items():
        if isinstance(v, Mapping):
            out[k] = {f: x for f, x in v.items() if f != "appliance"}
    registry = registry or {}
    for load in registry.get("switched_loads") or []:
        entity = load.get("entity_id")
        if not entity:
            continue
        sid = statistic_id(KIND_LOAD, entity)
        old_states = (out.get(sid) or {}).get("on_states") or []
        out[sid] = {
            "kind": KIND_LOAD,
            "source_id": entity,
            "name": load.get("name") or entity,
            "on_states": list(load.get("on_states") or old_states),
        }
    for sig in registry.get("signatures") or []:
        if not sig.get("label") or not sig.get("id"):
            continue
        out[statistic_id(KIND_SIGNATURE, sig["id"])] = {
            "kind": KIND_SIGNATURE,
            "source_id": sig["id"],
            "name": str(sig["label"]),
        }
    for appliance in appliances or ():
        sid = appliance.get("statistic_id")
        if not sid or not is_own(sid):
            continue
        srcs = appliance.get("sources") or []
        relay = next((x for x in srcs if x.get("type") == "switched_load"), None)
        sig = next((x for x in srcs if x.get("type") == "signature"), None)
        entry: dict[str, Any] = {"name": appliance.get("name") or sid, "appliance": appliance.get("key")}
        if relay:
            learned = out.get(statistic_id(KIND_LOAD, relay["entity_id"])) or out.get(sid) or {}
            entry.update(
                kind=KIND_LOAD,
                source_id=relay["entity_id"],
                on_states=list(relay.get("on_states") or learned.get("on_states") or []),
            )
        else:
            entry.update(kind=KIND_SIGNATURE, source_id=sig["id"] if sig else None)
        out[sid] = entry
    return dict(sorted(out.items()))
