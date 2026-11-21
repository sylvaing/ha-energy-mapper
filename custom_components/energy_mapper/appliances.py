"""The appliances Energy Mapper knows, and their stable identities.

Two kinds, both learned by the add-on:

- **switched loads**: appliances driven by a Home Assistant entity (relay,
  pilot wire), identified by that ``entity_id``;
- **signatures** with a user ``label`` in the registry (validated by the
  review flow), identified by their registry id, which survives the nightly
  re-clustering and renames.

The key (``load:<entity_id>`` / ``signature:<registry id>``) is the device
identifier and the base of the entity unique ids; the statistic id is
``energy_mapper:<slug>_energy``. A rename only changes the names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.util import slugify

from .const import DOMAIN

KIND_LOAD = "switched_load"
KIND_SIGNATURE = "signature"
_PREFIX = {KIND_LOAD: "load", KIND_SIGNATURE: "signature"}


@dataclass(frozen=True)
class Appliance:
    kind: str  # KIND_LOAD | KIND_SIGNATURE
    source_id: str  # entity id of the switch, or registry id of the signature
    name: str
    power_w: float | None = None

    @property
    def key(self) -> str:
        return f"{_PREFIX[self.kind]}:{self.source_id}"

    @property
    def statistic_id(self) -> str:
        return statistic_id(self.kind, self.source_id)

    @property
    def hourly_kind(self) -> str:
        """Its section in the add-on's hourly answer."""
        return "known" if self.kind == KIND_LOAD else "signatures"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source_id": self.source_id, "name": self.name, "power_w": self.power_w}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Appliance:
        return cls(data["kind"], data["source_id"], data["name"], data.get("power_w"))


def statistic_id(kind: str, source_id: str) -> str:
    return f"{DOMAIN}:{_PREFIX[kind]}_{slugify(source_id)}_energy"


def appliances_from(registry: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Appliance]:
    """The appliances of the registry (``GET /api/signatures``): its switched
    loads (those whose power was learned: a configured switch that never
    matched a power step is not an appliance yet) and its labelled
    signatures. ``names`` (entity id -> name, from the hourly breakdown of
    the last analysed day) gives the current name of the switched loads."""
    out: dict[str, Appliance] = {}
    for load in registry.get("switched_loads") or []:
        entity_id = load.get("entity_id")
        if not entity_id:
            continue
        name = names.get(entity_id) or load.get("name") or entity_id
        a = Appliance(KIND_LOAD, entity_id, name, load.get("power_w"))
        out[a.key] = a
    for sig in registry.get("signatures") or []:
        label = sig.get("label")
        if not label or not sig.get("id"):
            continue
        a = Appliance(KIND_SIGNATURE, sig["id"], str(label), sig.get("power_w"))
        out[a.key] = a
    return out
