"""The appliances of Energy Mapper: one config subentry each.

An appliance is what the user sees and names; Home Assistant is its source
of truth (a config subentry of type ``appliance`` of the Energy Mapper
entry). It is made of one or more **sources**, what the add-on measures:

- ``switched_load``: a relay or pilot wire (``entity_id``; ``on_states``
  only when the user forces them, else the add-on's automatic polarity);
- ``signature``: a signature of the add-on's registry (``id``), with its
  group and power so the add-on can recreate it if its registry is lost;
- ``submeter``: a power sensor that measures it (subtracted before the
  analysis; its energy is the sensor's own, so it gets no statistics here).

Several sources are the result of a merge: their energy is added.

Identity (stable across renames, link changes and merges): the ``key`` is
the device identifier, the subentry's ``unique_id`` and the base of the
entity unique ids; ``statistic_id`` is the id of the hourly energy
statistic. Both are given at creation from the first source
(``load:<entity_id>`` / ``signature:<registry id>``, as published since
#12) and never change afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.util import slugify

from .const import DOMAIN

SUBENTRY_TYPE = "appliance"

KIND_LOAD = "switched_load"
KIND_SIGNATURE = "signature"
KIND_SUBMETER = "submeter"
_PREFIX = {KIND_LOAD: "load", KIND_SIGNATURE: "signature", KIND_SUBMETER: "submeter"}
# section of the add-on's hourly answer of each source type
HOURLY_SECTION = {KIND_LOAD: "known", KIND_SIGNATURE: "signatures"}


@dataclass(frozen=True)
class Source:
    """What the add-on measures for (a part of) an appliance."""

    type: str  # KIND_LOAD | KIND_SIGNATURE | KIND_SUBMETER
    id: str  # entity id (switched load, sub-meter) or registry id (signature)
    on_states: tuple[str, ...] | None = None  # switched load: forced on-states
    # signature: its group and power (to recreate it when the registry lost it)
    kind: str | None = None
    band: str | None = None
    power_w: float | None = None

    @property
    def section(self) -> str | None:
        """Its section in the add-on's hourly answer (None: not estimated here)."""
        return HOURLY_SECTION.get(self.type)

    def as_dict(self) -> dict[str, Any]:
        if self.type == KIND_SIGNATURE:
            out: dict[str, Any] = {"type": self.type, "id": self.id}
            if self.kind and self.band and self.power_w:
                out.update(kind=self.kind, band=self.band, power_w=self.power_w)
            return out
        out = {"type": self.type, "entity_id": self.id}
        if self.type == KIND_LOAD and self.on_states:
            out["on_states"] = list(self.on_states)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Source:
        kind = data["type"]
        if kind == KIND_SIGNATURE:
            power = data.get("power_w")
            return cls(
                kind,
                str(data["id"]),
                kind=data.get("kind"),
                band=data.get("band"),
                power_w=float(power) if power else None,
            )
        if kind not in (KIND_LOAD, KIND_SUBMETER):
            raise ValueError(f"unknown source type {kind!r}")
        states = data.get("on_states")
        return cls(kind, str(data["entity_id"]), tuple(states) if states else None)


def relay(entity_id: str, on_states: Iterable[str] | None = None) -> Source:
    states = tuple(s.strip() for s in on_states or () if s and s.strip())
    return Source(KIND_LOAD, entity_id, tuple(dict.fromkeys(states)) or None)


def signature(sig: Mapping[str, Any]) -> Source:
    """The source of a registry signature (``GET /api/signatures`` item)."""
    power = sig.get("power_w")
    return Source(
        KIND_SIGNATURE,
        str(sig["id"]),
        kind=sig.get("kind"),
        band=sig.get("band"),
        power_w=float(power) if power else None,
    )


@dataclass(frozen=True)
class Appliance:
    key: str
    name: str
    statistic_id: str
    sources: tuple[Source, ...]
    subentry_id: str | None = None
    power_w: float | None = None  # typical power learned by the add-on

    @property
    def estimated(self) -> tuple[Source, ...]:
        """The sources whose energy the add-on estimates (statistics, sensors)."""
        return tuple(s for s in self.sources if s.section)

    def has(self, source: Source) -> bool:
        return any((s.type, s.id) == (source.type, source.id) for s in self.sources)

    def subentry_data(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "statistic_id": self.statistic_id,
            "sources": [s.as_dict() for s in self.sources],
        }

    def declaration(self) -> dict[str, Any]:
        """The appliance as pushed to the add-on (``PUT /api/appliances``)."""
        return {"key": self.key, "name": self.name, **self.subentry_data()}

    @classmethod
    def from_data(
        cls, data: Mapping[str, Any], name: str, subentry_id: str | None = None, power_w: float | None = None
    ) -> Appliance:
        sources = tuple(Source.from_dict(s) for s in data.get("sources") or [])
        key = str(data["key"])
        return cls(
            key, name, data.get("statistic_id") or key_statistic_id(key), sources, subentry_id, power_w
        )


def statistic_id(kind: str, source_id: str) -> str:
    """Statistic id of an appliance created from this source."""
    return f"{DOMAIN}:{_PREFIX[kind]}_{slugify(source_id)}_energy"


def key_statistic_id(key: str) -> str:
    """Statistic id of the appliance with this key (``<prefix>:<id>``)."""
    prefix, _, rest = key.partition(":")
    return f"{DOMAIN}:{prefix}_{slugify(rest)}_energy"


def new_appliance(name: str, sources: Iterable[Source], taken_keys: Iterable[str] = ()) -> Appliance:
    """A new appliance: key and statistic id from its first source (with a
    suffix in the rare case they are taken, e.g. by an appliance re-linked
    away from that source)."""
    sources = tuple(sources)
    first = sources[0]
    base = f"{_PREFIX[first.type]}:{first.id}"
    taken = set(taken_keys)
    key, n = base, 1
    while key in taken:
        n += 1
        key = f"{base}-{n}"
    return Appliance(key, name, key_statistic_id(key), sources)


def appliance_of_key(key: str, name: str) -> Appliance | None:
    """The appliance of a device created before subentries (#12)."""
    prefix, _, rest = key.partition(":")
    if not rest:
        return None
    if prefix == "load":
        source = Source(KIND_LOAD, rest)
    elif prefix == "signature":
        source = Source(KIND_SIGNATURE, rest)
    else:
        return None
    return Appliance(key, name, key_statistic_id(key), (source,))


def discovered(
    registry: Mapping[str, Any], names: Mapping[str, str], excluded: Iterable[str] = ()
) -> list[tuple[str, Source]]:
    """(name, source) of the appliances the add-on found: its switched loads
    whose power was learned (a configured switch that never matched a power
    step is not an appliance yet), except the excluded ones, and its labelled
    signatures (labels set before #11, or by another client). ``names``
    (entity id -> name, from the hourly breakdown of the last analysed day)
    gives the current name of the switched loads."""
    out: list[tuple[str, Source]] = []
    skip = set(excluded)
    for load in registry.get("switched_loads") or []:
        entity_id = load.get("entity_id")
        if not entity_id or entity_id in skip:
            continue
        out.append((names.get(entity_id) or load.get("name") or entity_id, relay(entity_id)))
    for sig in registry.get("signatures") or []:
        # a label the add-on holds for one of our appliances (``appliance``
        # set) is not a new appliance: when no subentry has it any more, the
        # appliance was deleted and the next push clears the label
        if sig.get("label") and sig.get("id") and not sig.get("appliance"):
            out.append((str(sig["label"]), signature(sig)))
    return out


def powers(registry: Mapping[str, Any]) -> dict[str, float]:
    """Learned power of each source (``<type>:<id>``) of the registry."""
    out: dict[str, float] = {}
    for load in registry.get("switched_loads") or []:
        if load.get("entity_id") and load.get("power_w"):
            out[f"{KIND_LOAD}:{load['entity_id']}"] = load["power_w"]
    for sig in registry.get("signatures") or []:
        if sig.get("id") and sig.get("power_w"):
            out[f"{KIND_SIGNATURE}:{sig['id']}"] = sig["power_w"]
    return out


def power_of(sources: Iterable[Source], learned: Mapping[str, float]) -> float | None:
    """Typical power of an appliance: the largest of its sources'."""
    values = [learned[f"{s.type}:{s.id}"] for s in sources if f"{s.type}:{s.id}" in learned]
    return max(values) if values else None


def source_value(section_values: Mapping[str, Mapping[str, Any]], appliance: Appliance, default: Any) -> Any:
    """Sum over the estimated sources of ``{"known": {...}, "signatures": {...}}``."""
    total = None
    for s in appliance.estimated:
        value = (section_values.get(s.section) or {}).get(s.id)
        if value is not None:
            total = value if total is None else total + value
    return default if total is None else total


def link_text(appliance: Appliance) -> str:
    """The sources, for the flows' descriptions."""
    return ", ".join(s.id for s in appliance.sources) or "-"
