"""Connected appliances (#79): which Home Assistant devices can teach an
appliance from their own state, and which of their entities to use.

Many connected appliances (washing machines, dryers, dishwashers, ovens)
expose an **operation state** (a ``sensor`` of the ``enum`` device class:
``run`` / ``stop`` / ``pause``...), often a **phase state** (another
``enum``: ``drying``, ``cooling``, ``rinsing``...), a cumulative **energy
counter** and the running **programme** (a ``select`` or an ``enum``). The
panel's "Learn from a connected appliance" picks a device; this module
proposes its entities, which the user confirms.

Nothing is specific to a vendor: a device qualifies by having an ``enum``
sensor; the operation state is the ``enum`` whose options include a running
word (``run``, ``running``...), a door-like ``enum`` (``open`` / ``closed`` /
``locked``) is never one; generic words of the entity ids only break ties.

Pure: plain dicts in (entity registry entries, states, device names), plain
dicts out; ``recorded`` tells whether the recorder keeps an entity (None:
unknown), so the panel can say that a programme state is not recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

ENUM = "enum"
ENERGY = "energy"
POWER = "power"
ENERGY_UNITS = ("kWh", "Wh", "MWh")
# option values that mean "the appliance runs"
RUN_WORDS = ("run", "running", "on", "active", "working", "in_progress", "busy")
# option values of a door or lock state: never an operation or a phase state
DOOR_WORDS = frozenset({"open", "opened", "closed", "close", "locked", "unlocked", "ajar"})
# generic words of the entity ids, for ties only
_OPERATION_HINTS = ("operation", "status", "state", "run")
_PHASE_HINTS = ("phase", "job", "step", "stage", "progress", "cycle")
_PROGRAM_HINTS = ("program", "programme", "cycle", "mode", "course")
MAX_DEVICES = 100


def _tokens(entity_id: str) -> set[str]:
    return set(entity_id.split(".", 1)[-1].split("_"))


def _hint(entity_id: str, words: Iterable[str]) -> int:
    tokens = _tokens(entity_id)
    return sum(1 for w in words if w in tokens)


def _options(attrs: Mapping[str, Any]) -> list[str]:
    options = attrs.get("options")
    return [str(o) for o in options] if isinstance(options, list) else []


def run_states(options: Iterable[str]) -> list[str]:
    """The options that mean the appliance runs."""
    return [o for o in options if o.lower() in RUN_WORDS]


def is_door(options: Iterable[str]) -> bool:
    values = {o.lower() for o in options}
    return bool(values) and values <= DOOR_WORDS


def devices(
    registry: Iterable[Mapping[str, Any]],
    states: Mapping[str, Mapping[str, Any]],
    device_names: Mapping[str, str],
    recorded: Callable[[str], bool | None] = lambda _: None,
    appliances: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """The devices that can teach an appliance from their state, by name:
    ``{device_id, name, appliance, entities: [...], proposal: {...}}``.

    ``registry``: entity registry entries (``entity_id``, ``device_id``,
    ``entity_category``, ``disabled_by``); ``states``: ``{entity_id:
    {"state", "attributes"}}``; ``appliances``: the integration's appliances
    (an entity that is a source or the teacher of one names it, and its
    on-states are reused)."""
    by_device: dict[str, list[str]] = {}
    for r in registry:
        dev = r.get("device_id")
        if not dev or r.get("disabled_by") or r.get("entity_category") == "diagnostic":
            continue
        if r["entity_id"] in states:
            by_device.setdefault(dev, []).append(r["entity_id"])
    owner: dict[str, Any] = {}
    forced: dict[str, list[str]] = {}
    for a in appliances:
        for s in a.sources:
            owner.setdefault(s.id, a)
            if s.on_states:
                forced[s.id] = list(s.on_states)
        if a.teacher is not None:
            for e in a.teacher.entities:
                owner.setdefault(e, a)
            if a.teacher.on_states:
                forced[a.teacher.entity_id] = list(a.teacher.on_states)
    out = []
    for dev, entities in by_device.items():
        item = _device(dev, sorted(entities), states, device_names, recorded, owner, forced)
        if item is not None:
            out.append(item)
    out.sort(key=lambda d: (d["name"].casefold(), d["device_id"]))
    return out[:MAX_DEVICES]


def _attrs(states: Mapping[str, Mapping[str, Any]], entity_id: str) -> Mapping[str, Any]:
    return (states.get(entity_id) or {}).get("attributes") or {}


def _device(
    dev: str,
    entities: list[str],
    states: Mapping[str, Mapping[str, Any]],
    device_names: Mapping[str, str],
    recorded: Callable[[str], bool | None],
    owner: Mapping[str, Any],
    forced: Mapping[str, list[str]],
) -> dict[str, Any] | None:
    enums, selects, energies, powers = [], [], [], []
    for e in entities:
        domain = e.split(".", 1)[0]
        attrs = _attrs(states, e)
        dc = attrs.get("device_class")
        if domain == "sensor" and dc == ENUM:
            enums.append(e)
        elif domain == "select":
            selects.append(e)
        elif domain == "sensor" and (dc == ENERGY or attrs.get("unit_of_measurement") in ENERGY_UNITS):
            energies.append(e)
        elif domain == "sensor" and dc == POWER:
            powers.append(e)
    states_ok = [e for e in enums if not is_door(_options(_attrs(states, e)))]
    if not states_ok:
        return None

    def op_rank(e: str):
        opts = _options(_attrs(states, e))
        return (e in forced, bool(run_states(opts)), _hint(e, _OPERATION_HINTS), -len(opts), e)

    operation = max(states_ok, key=op_rank)
    on_states = forced.get(operation) or run_states(_options(_attrs(states, operation)))
    rest = [e for e in states_ok if e != operation]
    phase = max(
        rest,
        key=lambda e: (
            _hint(e, _PHASE_HINTS) - _hint(e, _PROGRAM_HINTS),
            len(_options(_attrs(states, e))),
            e,
        ),
        default=None,
    )
    if phase is not None and _hint(phase, _PROGRAM_HINTS) > _hint(phase, _PHASE_HINTS):
        phase = None  # a programme state, not a phase
    programs = selects + [e for e in rest if e != phase]
    program = max(programs, key=lambda e: (_hint(e, _PROGRAM_HINTS), e in selects, e), default=None)
    if program is not None and not _hint(program, _PROGRAM_HINTS) and program not in selects:
        program = None
    energy = max(
        energies,
        key=lambda e: (_attrs(states, e).get("state_class") in ("total_increasing", "total"), e),
        default=None,
    )

    def describe(e: str, role: str) -> dict[str, Any]:
        attrs = _attrs(states, e)
        a = owner.get(e)
        return {
            "entity_id": e,
            "name": str(attrs.get("friendly_name") or e),
            "role": role,
            "options": _options(attrs),
            "unit": attrs.get("unit_of_measurement"),
            "recorded": recorded(e),
            "appliance": {"key": a.key, "name": a.name} if a is not None else None,
        }

    listed = (
        [describe(e, "state") for e in states_ok]
        + [describe(e, "program") for e in selects]
        + [describe(e, "energy") for e in energies]
        + [describe(e, "power") for e in powers]
    )
    a = next((owner[e] for e in (operation, *entities) if e in owner), None)
    return {
        "device_id": dev,
        "name": device_names.get(dev) or str(_attrs(states, operation).get("friendly_name") or dev),
        "appliance": {"key": a.key, "name": a.name} if a is not None else None,
        "entities": listed,
        "proposal": {
            "entity_id": operation,
            "on_states": on_states,
            "phase_entity": phase,
            "energy_entity": energy,
            "program_entity": program,
            "power_entity": powers[0] if powers else None,
        },
    }


def recorder_filter(hass: Any) -> Callable[[str], bool | None]:
    """Whether the recorder keeps an entity (None: unknown, no recorder)."""
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415

        instance = get_instance(hass)
    except (ImportError, KeyError):
        return lambda _: None
    entity_filter = getattr(instance, "entity_filter", None)
    if entity_filter is None:
        return lambda _: True
    return lambda e: bool(entity_filter(e))
