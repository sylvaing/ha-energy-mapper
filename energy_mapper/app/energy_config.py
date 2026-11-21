"""Derive the analysis configuration from the Home Assistant Energy dashboard.

Inputs are plain JSON as returned by the websocket API, so this module is
testable without Home Assistant:

* ``prefs``: result of ``energy/get_prefs``;
* ``states``: result of ``get_states`` (entity_id, state, attributes);
* ``registry``: result of ``config/entity_registry/list`` (entity_id,
  device_id, platform);
* ``statistics``: the statistics Energy Mapper publishes, by id
  (:func:`statistic_ids.known_statistics`).

The Energy dashboard lists *energy* sensors (kWh). The engine needs *power*
sensors (W), so each energy sensor is mapped to the power sensor of the same
device. Manual options always win over what is derived here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from nilm_core import AnalysisConfig, SubmeterConfig, SwitchedLoadConfig
from statistic_ids import KIND_LOAD, KIND_SIGNATURE, is_own, kind_of, statistic_id

POWER_UNITS = {"W": 1.0, "kW": 1000.0, "mW": 0.001, "MW": 1_000_000.0}
UNAVAILABLE = {"unavailable", "unknown"}
# domains whose on/off state tells when an estimated device runs
SWITCH_DOMAINS = {"switch", "input_boolean"}
# common suffixes of energy and power sensors, removed to compare names
_SUFFIXES = re.compile(
    r"_(energy|energy_returned|energy_consumed|energy_produced|total_production|"
    r"total_energy|summation_delivered|summation_received|consumption|production|"
    r"power|active_power|current_power|device_power|device_energy|"
    r"import|export|today|total)(_\d+)?$"
)


@dataclass
class Finding:
    """Something worth telling the user (shown in the report)."""

    kind: str  # grid_ignored | no_power_sensor | unavailable | battery_sign | ...
    entity_id: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "entity_id": self.entity_id, "detail": self.detail}


@dataclass
class TrackedDevice:
    """A device of the Energy dashboard and the role it plays in the analysis."""

    name: str
    energy_entity: str
    power_entity: str | None
    # submeter | switched | estimated | energy_only | unavailable | nested | energy_mapper
    role: str
    source_entity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "energy_entity": self.energy_entity,
            "power_entity": self.power_entity,
            "role": self.role,
            "source_entity": self.source_entity,
        }


@dataclass
class DerivedConfig:
    config: AnalysisConfig | None  # None: no grid meter could be found
    tracked: list[TrackedDevice] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        c = self.config
        return {
            "grid_entity": c.grid_entity if c else None,
            "extra_grid_entities": c.extra_grid_entities if c else [],
            "grid_export_entity": c.grid_export_entity if c else None,
            "pv_entity": c.pv_entity if c else None,
            "extra_pv_entities": c.extra_pv_entities if c else [],
            "battery_entity": c.battery_entity if c else None,
            "tracked": [t.to_dict() for t in self.tracked],
            "findings": [f.to_dict() for f in self.findings],
        }


class _Index:
    def __init__(self, states: list[dict], registry: list[dict]):
        self.states = {s["entity_id"]: s for s in states}
        self.device_of = {r["entity_id"]: r.get("device_id") for r in registry}
        self.platform_of = {r["entity_id"]: r.get("platform") for r in registry}
        self.by_device: dict[str, list[str]] = {}
        for r in registry:
            if r.get("device_id"):
                self.by_device.setdefault(r["device_id"], []).append(r["entity_id"])

    def attrs(self, entity_id: str) -> dict:
        return (self.states.get(entity_id) or {}).get("attributes") or {}

    def state(self, entity_id: str) -> str | None:
        s = self.states.get(entity_id)
        return s.get("state") if s else None

    def is_power(self, entity_id: str) -> bool:
        if not entity_id.startswith("sensor."):
            return False
        a = self.attrs(entity_id)
        return a.get("device_class") == "power" or a.get("unit_of_measurement") in POWER_UNITS

    def scale(self, entity_id: str) -> float:
        return POWER_UNITS.get(self.attrs(entity_id).get("unit_of_measurement"), 1.0)

    def available(self, entity_id: str) -> bool:
        st = self.state(entity_id)
        return st is not None and st not in UNAVAILABLE


def _stem(entity_id: str) -> str:
    return _SUFFIXES.sub("", entity_id.split(".", 1)[-1])


_GENERIC = {"energy", "power", "total", "active", "current", "device", "kwh", "w", "sensor"}


def _tokens(entity_id: str) -> set[str]:
    return {t for t in entity_id.split(".", 1)[-1].split("_") if t and t not in _GENERIC}


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def power_for(energy_entity: str, idx: _Index) -> str | None:
    """The power sensor that goes with an energy sensor.

    1. Same ``source_entity`` (estimation integrations such as powercalc).
    2. Same device, closest name (a Shelly EM has one power sensor per channel).
    3. Same name with "power" instead of "energy".
    """
    src = idx.attrs(energy_entity).get("source_entity")
    if src:
        same_src = [e for e in idx.states if idx.is_power(e) and idx.attrs(e).get("source_entity") == src]
        if same_src:
            return min(same_src, key=len)
    stem = _stem(energy_entity)
    dev = idx.device_of.get(energy_entity)
    if dev:
        cands = [e for e in idx.by_device.get(dev, []) if idx.is_power(e)]
        # ignore power factor, apparent/reactive power and the like
        cands = [e for e in cands if idx.attrs(e).get("device_class", "power") == "power"]
        if cands:
            tokens = _tokens(energy_entity)
            return max(
                cands,
                key=lambda e: (len(_tokens(e) & tokens), _common_prefix(_stem(e), stem), -len(e)),
            )
    guess = "sensor." + stem + "_power"
    if guess in idx.states and idx.is_power(guess):
        return guess
    return None


def _flows(source: dict, key: str) -> list[str]:
    """Energy sensors of a source. Handles the flat format (``stat_energy_from``)
    and the older ``flow_from`` / ``flow_to`` lists."""
    if key in source and source[key]:
        return [source[key]]
    legacy = {"stat_energy_from": "flow_from", "stat_energy_to": "flow_to"}[key]
    return [f[key] for f in source.get(legacy) or [] if f.get(key)]


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _own_statistic(
    stat_id: str, name: str | None, statistics: Mapping[str, Mapping], idx: _Index, by_stat: dict
) -> tuple[str, SwitchedLoadConfig | None, str | None, Finding | None]:
    """An Energy Mapper statistic of the Energy dashboard -> (name, switched
    load to keep measuring, its entity, finding). Never a sub-meter: its
    energy is what the analysis itself attributes to the appliance."""
    known = statistics.get(stat_id) or {}
    if known.get("appliance"):
        # an appliance declared in Home Assistant: known whatever its links
        label = name or known.get("name") or stat_id
        entity = known.get("source_id") if known.get("kind") == KIND_LOAD else None
        if entity and entity in idx.states:
            on_states = tuple(known.get("on_states") or ("on",))
            return label, SwitchedLoadConfig(entity, label, on_states), entity, None
        return label, None, entity, None
    kind = kind_of(stat_id)
    if kind == KIND_LOAD:
        entity = known.get("source_id") if known.get("kind") == KIND_LOAD else None
        if not entity:
            # not in the stored mapping (older add-on, lost data): the entity
            # whose statistic id it is, if it still exists
            if not by_stat:
                by_stat.update({statistic_id(KIND_LOAD, e): e for e in idx.states})
            entity = by_stat.get(stat_id)
        if entity and entity in idx.states:
            label = name or known.get("name") or idx.attrs(entity).get("friendly_name") or entity
            on_states = tuple(known.get("on_states") or ("on",))
            return label, SwitchedLoadConfig(entity, label, on_states), entity, None
    elif kind == KIND_SIGNATURE and known.get("kind") == KIND_SIGNATURE:
        return name or known.get("name") or stat_id, None, None, None
    label = name or known.get("name") or stat_id
    return (
        label,
        None,
        None,
        Finding(
            "unknown_statistic",
            stat_id,
            f"{label}: Energy Mapper statistic of an appliance it no longer knows "
            "(not used by the analysis; remove it from the Energy dashboard if it is obsolete)",
        ),
    )


def derive(
    prefs: dict | None,
    states: list[dict],
    registry: list[dict],
    options: dict | None = None,
    statistics: Mapping[str, Mapping] | None = None,
) -> DerivedConfig:
    """Build the analysis configuration. ``options`` are the user's manual
    settings (grid_entity, grid_export_entity, grid_invert, pv_entity,
    battery_entity, battery_invert, submeters, switched_loads, thresholds;
    excluded_switched_loads: switches never measured as switched loads;
    extra_submeters: (entity, name) always subtracted).
    ``statistics`` maps Energy Mapper's own statistic ids to their appliance
    (an appliance whose estimate was replaced by its Energy Mapper statistic
    in the Energy dashboard stays measured)."""
    options = options or {}
    statistics = statistics or {}
    by_stat: dict[str, str] = {}  # statistic id -> entity id, built on demand
    idx = _Index(states, registry)
    out = DerivedConfig(config=None)
    sources = (prefs or {}).get("energy_sources") or []

    # --- grid --------------------------------------------------------------
    grids: list[str] = []
    exports: list[str] = []
    for src in (s for s in sources if s.get("type") == "grid"):
        imports = _flows(src, "stat_energy_from")
        if not imports:
            continue
        energy = imports[0]
        if _number(idx.state(energy)) == 0:
            out.findings.append(Finding("grid_ignored", energy, "grid meter that has never counted"))
            continue
        power = power_for(energy, idx)
        if not power:
            out.findings.append(Finding("no_power_sensor", energy, "grid meter without power sensor"))
            continue
        grids.append(power)
        for exp in _flows(src, "stat_energy_to"):
            exp_power = power_for(exp, idx)
            if exp_power and exp_power != power:
                exports.append(exp_power)

    # --- solar and battery -------------------------------------------------
    pvs: list[str] = []
    for src in (s for s in sources if s.get("type") == "solar"):
        for energy in _flows(src, "stat_energy_from"):
            p = power_for(energy, idx)
            if p:
                pvs.append(p)
            else:
                out.findings.append(Finding("no_power_sensor", energy, "solar without power sensor"))
    battery = None
    for src in (s for s in sources if s.get("type") == "battery"):
        energy = (_flows(src, "stat_energy_from") or _flows(src, "stat_energy_to") or [None])[0]
        battery = power_for(energy, idx) if energy else None
        if battery:
            out.findings.append(
                Finding("battery_sign", battery, "check the sign: positive must mean discharging")
            )
        break

    # --- devices -----------------------------------------------------------
    submeters: list[SubmeterConfig] = []
    switched: list[SwitchedLoadConfig] = []
    own: dict[str, SwitchedLoadConfig] = {}  # from Energy Mapper statistics, by entity
    # switched loads the user excluded: an estimate driven by such a switch is
    # subtracted like any other estimate instead of being measured
    excluded = set(options.get("excluded_switched_loads") or [])
    devices = (prefs or {}).get("device_consumption") or []
    parents = {d.get("stat_consumption") for d in devices}
    for d in devices:
        energy = d.get("stat_consumption")
        if not energy:
            continue
        parent = d.get("included_in_stat")
        if is_own(energy):
            name, load, entity, finding = _own_statistic(energy, d.get("name"), statistics, idx, by_stat)
            if finding:
                out.findings.append(finding)
            if parent and parent in parents:
                # counted inside another tracked device: never subtract twice
                out.tracked.append(TrackedDevice(name, energy, None, "nested", entity))
                continue
            if load and load.entity_id not in excluded:
                own[load.entity_id] = load
            out.tracked.append(TrackedDevice(name, energy, None, "energy_mapper", entity))
            continue
        name = d.get("name") or idx.attrs(energy).get("friendly_name") or energy
        src = idx.attrs(energy).get("source_entity")
        power = power_for(energy, idx)
        if not idx.available(energy):
            out.tracked.append(TrackedDevice(name, energy, power, "unavailable", src))
            out.findings.append(Finding("unavailable", energy, f"{name}: sensor unavailable"))
            continue
        if parent and parent in parents:
            # counted inside another tracked device: never subtract twice
            out.tracked.append(TrackedDevice(name, energy, power, "nested", src))
            continue
        if src and src.split(".")[0] in SWITCH_DOMAINS and src not in excluded:
            # estimated from a switch: the engine measures it from the switch
            switched.append(SwitchedLoadConfig(src, name))
            out.tracked.append(TrackedDevice(name, energy, power, "switched", src))
            continue
        if not power:
            out.tracked.append(TrackedDevice(name, energy, None, "energy_only", src))
            out.findings.append(
                Finding("no_power_sensor", energy, f"{name}: no power sensor, not subtracted")
            )
            continue
        role = "estimated" if src else "submeter"
        submeters.append(SubmeterConfig(power, name))
        out.tracked.append(TrackedDevice(name, energy, power, role, src))

    # the same relay behind an estimate and an Energy Mapper statistic: the
    # latter carries the learned polarity
    switched = [s for s in switched if s.entity_id not in own] + list(own.values())

    # --- manual options win --------------------------------------------------
    grid = options.get("grid_entity") or (grids[0] if grids else None)
    if not grid:
        out.findings.append(Finding("no_grid", "", "no grid power sensor: choose one in the settings"))
        return out
    extra_grids = [] if options.get("grid_entity") else grids[1:]
    export = options.get("grid_export_entity") or (exports[0] if exports and len(grids) == 1 else None)
    pv = options.get("pv_entity") or (pvs[0] if pvs else None)
    extra_pv = [] if options.get("pv_entity") else pvs[1:]
    battery = options.get("battery_entity") or battery
    if options.get("submeters") is not None:
        submeters = [SubmeterConfig(e, e) for e in options["submeters"]]
    # sub-meters of the user's appliances: always added
    for entity, label in options.get("extra_submeters") or []:
        if all(m.entity_id != entity for m in submeters):
            submeters.append(SubmeterConfig(entity, label))
    for item in options.get("switched_loads") or []:
        entity = item["entity_id"]
        switched = [s for s in switched if s.entity_id != entity]
        switched.append(
            SwitchedLoadConfig(entity, item.get("name") or entity, tuple(item.get("on_states") or ("on",)))
        )

    entities = [grid, *extra_grids, export, pv, *extra_pv, battery, *(s.entity_id for s in submeters)]
    cfg = AnalysisConfig(
        grid_entity=grid,
        extra_grid_entities=extra_grids,
        grid_export_entity=export,
        grid_invert=bool(options.get("grid_invert")),
        pv_entity=pv,
        extra_pv_entities=extra_pv,
        battery_entity=battery,
        battery_invert=bool(options.get("battery_invert")),
        submeters=submeters,
        switched_loads=switched,
        scales={e: idx.scale(e) for e in entities if e and idx.scale(e) != 1.0},
    )
    if options.get("step_threshold_w"):
        cfg.step_threshold_w = float(options["step_threshold_w"])
    if options.get("peak_threshold_w"):
        cfg.peak_threshold_w = float(options["peak_threshold_w"])
    out.config = cfg
    return out


def tracking_gaps(
    report: dict,
    tracked: list[TrackedDevice],
    histories: dict[str, list[tuple[float, str]]],
    min_kwh: float = 0.5,
    ratio: float = 2.0,
) -> list[Finding]:
    """Switch-driven devices whose declared energy is far from what the
    analysis measured over the same period (e.g. an estimate configured at a
    few watts for a 2 kW water heater)."""
    out: list[Finding] = []
    measured = report.get("energy_kwh", {}).get("known", {})
    # switched loads by entity: their name may differ from the device's
    by_entity = {k.get("entity_id"): k.get("name") for k in report.get("known_loads") or []}
    for t in tracked:
        if t.role != "switched":
            continue
        got = measured.get(by_entity.get(t.source_entity, t.name))
        rows = [(ts, _number(v)) for ts, v in histories.get(t.energy_entity, [])]
        rows = [(ts, v) for ts, v in rows if v is not None and report["start"] <= ts < report["end"]]
        if got is None or len(rows) < 2:
            continue
        declared = max(rows[-1][1] - rows[0][1], 0.0)  # total_increasing counter
        big, small = max(got, declared), min(got, declared)
        if big >= min_kwh and (small == 0 or big / small >= ratio):
            out.append(
                Finding(
                    "tracking_gap",
                    t.energy_entity,
                    f"{t.name}: declared {declared:.2f} kWh, measured {got:.2f} kWh",
                )
            )
    return out
