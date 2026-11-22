"""Learning an appliance from a measured device (#14).

The user marks a power sensor (a temporary smart plug, an appliance's own
meter) as the **teacher** of an appliance (``appliances.py``). While it
teaches:

- the analysis does not subtract it, so the appliance stays in the
  whole-house curve exactly as it will be once the plug is gone;
- each analysed day keeps the sensor's runs (:func:`day_truth`) in its
  report, under ``ground_truth``: the true uses of the appliance that day,
  and the periods the sensor reported.

From the stored days, :func:`status` learns the appliance's signature
(:func:`nilm_core.groundtruth.learn`), finds the registry signature it is
(:func:`resolve`) and measures how well the curve-based recognition finds
the true uses (:func:`nilm_core.groundtruth.evaluate`). :func:`apply` gives
the signature to link to the appliance, created as an expected signature
when the registry has none yet.

A **connected appliance** (#79) teaches the same way without a power
sensor: its teacher is its operation state (``mode: "state"``, the states in
which it runs, and optionally its phase state, programme state and energy
counter). Nothing is hidden from the analysis (the appliance is in the curve
already); each day keeps its runs measured from the house curve
(:func:`day_state_truth`), and the learning, resolution and evaluation are
those of a measured device. A multi-phase appliance whose programme state is
recorded learns one template per programme (:func:`program_signatures`).

Pure (no I/O, no clock): the caller passes the summaries, the registry and
``now``.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence

from nilm_core import cycles as mp
from nilm_core.groundtruth import (
    MULTIPHASE,
    GroundTruthConfig,
    Learned,
    Run,
    detections,
    evaluate,
    learn,
    merge_evaluations,
    runs_of,
    state_runs,
    truth_intervals,
)
from nilm_core.registry import (
    Matcher,
    RegistryConfig,
    expected_signature,
    nearest_signature,
)

GROUND_TRUTH_KEY = "ground_truth"
STATE = "state"  # a connected appliance's teacher: its operation state


class TeachingError(ValueError):
    """The learned signature cannot be linked. ``code``: ``not_learned`` (no
    use seen yet), ``not_recognisable`` (below the step threshold: the curve
    cannot show it) or ``conflict`` (it belongs to another appliance,
    ``signature_id``)."""

    def __init__(self, code: str, message: str, signature_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.signature_id = signature_id


def day_truth(
    key: str,
    entity_id: str,
    points: Sequence[tuple[float, float | None]],
    start: float,
    end: float,
    config: GroundTruthConfig | None = None,
) -> dict:
    """The ground truth of one appliance on one day, as stored in the report."""
    runs, covered = runs_of(points, start, end, config)
    return {
        "key": key,
        "entity_id": entity_id,
        "runs": [r.to_dict() for r in runs],
        "covered": [[round(a), round(b)] for a, b in covered],
        "covered_s": round(sum(b - a for a, b in covered)),
        "energy_kwh": round(sum(r.energy_kwh for r in runs), 3),
    }


def residual_curve(report: Mapping, own: str | None = None) -> list[tuple[float, float]]:
    """The residual load per curve row of a report: the house without the
    sub-meters and the switched loads, except the one of ``own`` (the entity
    of the appliance measured: its own switched load must stay in)."""
    names = {k.get("name") for k in report.get("known_loads") or [] if k.get("entity_id") != own}
    out = []
    for row in report.get("curve") or []:
        try:
            others = sum(float(w) for n, w in (row.get("known") or {}).items() if n in names)
            out.append((float(row["t"]), float(row["house"]) - float(row.get("submetered") or 0.0) - others))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _numbers(rows: Sequence[tuple[float, str | None]], scale: float = 1.0) -> list[tuple[float, float]]:
    out = []
    for t, v in rows:
        try:
            out.append((float(t), float(v) * scale))
        except (TypeError, ValueError):
            continue
    return out


def day_state_truth(
    key: str,
    teacher: Mapping,
    histories: Mapping[str, Sequence[tuple[float, str | None]]],
    report: Mapping,
    start: float,
    end: float,
    energy_scale: float = 1.0,
    config: GroundTruthConfig | None = None,
) -> dict:
    """The ground truth of a connected appliance on one day (#79): the runs
    of its operation state, measured on the day's curve (``report``), with
    their phases, programme and counted energy. ``energy_scale`` turns the
    counter's unit into kWh."""
    entity = teacher["entity_id"]
    on_states = teacher.get("on_states") or ["run"]
    phase = teacher.get("phase_entity")
    program = teacher.get("program_entity")
    counter = teacher.get("energy_entity")
    program_rows = histories.get(program) if program else None
    runs, covered = state_runs(
        histories.get(entity) or [],
        on_states,
        residual_curve(report, entity),
        start,
        end,
        config,
        phases=histories.get(phase) if phase else None,
        programs=program_rows,
        counter=_numbers(histories.get(counter) or [], energy_scale) if counter else None,
    )
    out = {
        "key": key,
        "entity_id": entity,
        "mode": STATE,
        "runs": [r.to_dict() for r in runs],
        "covered": [[round(a), round(b)] for a, b in covered],
        "covered_s": round(sum(b - a for a, b in covered)),
        "energy_kwh": round(sum(r.energy_kwh for r in runs), 3),
    }
    if program:
        # a programme state the recorder does not keep (an excluded domain)
        out["program_recorded"] = any(
            v is not None and str(v) not in ("unavailable", "unknown", "") for _, v in program_rows or []
        )
    return out


def truth_days(summaries: Mapping[str, Mapping], key: str, entity_id: str) -> dict[str, Mapping]:
    """``{day: ground truth}`` of the stored days taught by this sensor."""
    out = {}
    for day in sorted(summaries):
        for gt in summaries[day].get(GROUND_TRUTH_KEY) or []:
            if gt.get("key") == key and gt.get("entity_id") == entity_id and gt.get("covered_s"):
                out[day] = gt
    return out


def _runs(days: Mapping[str, Mapping]) -> list[Run]:
    out = []
    for gt in days.values():
        for r in gt.get("runs") or []:
            try:
                out.append(Run.from_dict(r))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _phases(sig: Mapping) -> list[mp.Phase]:
    try:
        return [mp.Phase.from_dict(p) for p in sig.get("phases") or []]
    except (KeyError, TypeError, ValueError):
        return []


def resolve(registry: Mapping | None, learned: Learned, config: RegistryConfig | None = None) -> dict | None:
    """The registry signature the learned one is: for a steady or cycling
    one, the signature its uses are matched to (same group, power within the
    tolerance); for a multi-phase one, the nearest template within the
    registry's cycle distance (an expected one without a template yet: by
    its power). None when the registry has none."""
    config = config or RegistryConfig()
    if learned.kind != MULTIPHASE:
        return nearest_signature(registry, learned.kind, learned.band, learned.power_w)
    tol = float((registry or {}).get("power_tol") or config.power_tol)
    best, best_d = None, math.inf
    for sig in (registry or {}).get("signatures") or []:
        if sig.get("kind") != MULTIPHASE:
            continue
        phases = _phases(sig)
        if phases and learned.phases:
            d = mp.distance(learned.phases, phases)
            if d > config.cycle_distance:
                continue
        else:
            power = float(sig.get("power_w") or 0.0)
            if not power or abs(power - learned.power_w) > tol * power:
                continue
            d = config.cycle_distance + abs(power - learned.power_w) / power  # after the templates
        if d < best_d:
            best, best_d = sig, d
    return best


def new_signature(
    registry: Mapping | None, learned: Learned, appliance: Mapping, entity_id: str, now: float
) -> dict:
    """An expected signature built from the learned one (the registry has
    none): labelled with the appliance, so it is kept and the first cluster
    near it takes its id; a multi-phase one has the learned template, so the
    analysis recognises its cycles at once."""
    sig = expected_signature(registry, learned.kind, learned.band, learned.power_w, appliance["name"], now)
    sig["appliance"] = appliance["key"]
    sig["duration_s"] = round(learned.duration_s)
    if learned.kind == "cycling":
        sig.update(on_s=learned.on_s, period_s=learned.period_s, duty=learned.duty)
    if learned.kind == MULTIPHASE:
        sig["phases"] = [p.to_dict() for p in learned.phases]
        sig["cycle_kwh"] = None if learned.cycle_kwh is None else round(learned.cycle_kwh, 3)
    sig["taught"] = {"entity_id": entity_id, "at": now}
    return sig


def owner_of(sig: Mapping | None, key: str) -> str | None:
    """The other appliance that owns this signature, if any."""
    if not sig:
        return None
    owner = sig.get("appliance")
    if owner and owner != key:
        return owner
    if sig.get("label") and not owner:
        return str(sig["label"])  # labelled by another client
    return None


def status(
    appliance: Mapping,
    summaries: Mapping[str, Mapping],
    registry: Mapping | None,
    now: float,
    step_threshold_w: float = 300.0,
    config: RegistryConfig | None = None,
    gt_config: GroundTruthConfig | None = None,
) -> dict:
    """The learning status of a taught appliance: the days and uses seen,
    the learned signature, the registry signature it is, and the measured
    recognition over the taught days."""
    teacher = appliance["teacher"]
    key, entity = appliance["key"], teacher["entity_id"]
    days = truth_days(summaries, key, entity)
    runs = _runs(days)
    learned = learn(runs, step_threshold_w, gt_config)
    out: dict = {
        "key": key,
        "name": appliance.get("name"),
        "entity_id": entity,
        "mode": teacher.get("mode") or "power",
        "since": teacher.get("since"),
        "days": len(days),
        "first_day": next(iter(days), None),
        "last_day": next(reversed(days), None) if days else None,
        "uses": len(runs),
        "energy_kwh": round(sum(r.energy_kwh for r in runs), 3),
        "learned": learned.to_dict() if learned else None,
        "signature": None,
        "conflict": None,
        "evaluation": None,
        "per_day": [],
    }
    if teacher.get("mode") == STATE:
        out.update(
            on_states=list(teacher.get("on_states") or []),
            phase_entity=teacher.get("phase_entity"),
            energy_entity=teacher.get("energy_entity"),
            program_entity=teacher.get("program_entity"),
            # None: no programme state, or no day says yet
            program_recorded=_program_recorded(days) if teacher.get("program_entity") else None,
        )
    if learned is None:
        return out
    sig = resolve(registry, learned, config)
    owner = owner_of(sig, key)
    if owner:
        out["conflict"] = {"id": sig["id"], "appliance": owner}
    linked = {s.get("id") for s in appliance.get("sources") or [] if s.get("type") == "signature"}
    if sig is None:
        # not in the registry yet: evaluated as the expected signature it would be
        sig = new_signature(registry, learned, appliance, entity, now)
        reg = copy.deepcopy(dict(registry or {}))
        reg.setdefault("signatures", []).append(sig)
        known = False
    else:
        reg = registry
        known = True
    out["signature"] = {
        "id": sig["id"],
        "kind": sig.get("kind"),
        "band": sig.get("band"),
        "power_w": sig.get("power_w"),
        "label": sig.get("label") if known else None,
        "in_registry": known,
        "linked": sig["id"] in linked,
    }
    out["evaluation"], out["per_day"] = evaluate_days(days, summaries, reg, sig, learned, gt_config)
    return out


def _program_recorded(days: Mapping[str, Mapping]) -> bool | None:
    """Whether the programme state was recorded on some learning day."""
    seen = [gt.get("program_recorded") for gt in days.values() if "program_recorded" in gt]
    return any(seen) if seen else None


def evaluate_days(
    days: Mapping[str, Mapping],
    summaries: Mapping[str, Mapping],
    registry: Mapping | None,
    sig: Mapping,
    learned: Learned | None = None,
    gt_config: GroundTruthConfig | None = None,
) -> tuple[dict, list[dict]]:
    """Recognition of ``sig`` (a registry signature) over the days whose
    ground truth is ``days``: the merged evaluation and one per day."""
    gt_config = gt_config or GroundTruthConfig()
    matcher = Matcher(registry)
    template = None
    if sig.get("kind") == MULTIPHASE:
        template = _phases(sig) or (learned.phases if learned else None)
    per_day = []
    for day, gt in days.items():
        found = detections(
            summaries[day], sig["id"], matcher.match, template, gt_config.cycles.match_distance
        )
        covered = [(float(a), float(b)) for a, b in gt.get("covered") or []]
        result = evaluate(truth_intervals(gt.get("runs") or []), found, covered, gt_config.min_overlap)
        per_day.append({"day": day, **result})
    return merge_evaluations(per_day), per_day


def apply(
    appliance: Mapping,
    summaries: Mapping[str, Mapping],
    registry: dict,
    now: float,
    step_threshold_w: float = 300.0,
    config: RegistryConfig | None = None,
) -> tuple[dict, bool, dict]:
    """The signature to link to a taught appliance: ``(signature, created,
    learned)``. A new expected signature is added to ``registry`` (modified)
    when it has none. Raises :class:`TeachingError`."""
    teacher = appliance["teacher"]
    days = truth_days(summaries, appliance["key"], teacher["entity_id"])
    learned = learn(_runs(days), step_threshold_w)
    if learned is None:
        raise TeachingError("not_learned", "no use of the appliance seen by its teacher yet")
    if not learned.recognisable:
        raise TeachingError(
            "not_recognisable",
            f"{round(learned.power_w)} W is below the step threshold: the curve cannot show it",
        )
    sig = resolve(registry, learned, config)
    if sig is not None:
        owner = owner_of(sig, appliance["key"])
        if owner:
            raise TeachingError("conflict", f"{sig['id']} belongs to {owner}", sig["id"])
        return sig, False, learned.to_dict()
    sig = new_signature(registry, learned, appliance, teacher["entity_id"], now)
    registry.setdefault("signatures", []).append(sig)
    return sig, True, learned.to_dict()


def program_signatures(
    appliance: Mapping,
    learned: Mapping,
    registry: dict,
    now: float,
    config: RegistryConfig | None = None,
) -> list[tuple[dict, bool]]:
    """The signatures of the other programmes of a multi-phase connected
    appliance (#79): ``learned["programs"]`` beyond the learned one, used at
    least twice, with a heating phase, and whose template is not within the
    match distance of the learned one (else it is the same signature). Each
    is the registry signature it resolves to (none of another appliance) or
    a new expected one added to ``registry`` (modified). ``[(signature,
    created)]``."""
    config = config or RegistryConfig()
    if learned.get("kind") != MULTIPHASE:
        return []
    try:
        main = [mp.Phase.from_dict(p) for p in learned.get("phases") or []]
    except (KeyError, TypeError, ValueError):
        return []
    out: list[tuple[dict, bool]] = []
    taken = set()
    entity = appliance["teacher"]["entity_id"]
    for item in learned.get("programs") or []:
        if item.get("program") == learned.get("program") or item.get("kind") != MULTIPHASE:
            continue
        if int(item.get("uses") or 0) < 2:
            continue
        try:
            phases = [mp.Phase.from_dict(p) for p in item.get("phases") or []]
        except (KeyError, TypeError, ValueError):
            continue
        if not any(p.kind == mp.HEAT for p in phases):
            continue
        if main and mp.distance(phases, main) <= config.cycle_distance:
            continue
        other = Learned(
            kind=MULTIPHASE,
            band="cycle",
            power_w=float(item.get("power_w") or 0.0),
            duration_s=float(item.get("duration_s") or 0.0),
            uses=int(item["uses"]),
            runs=int(item["uses"]),
            phases=phases,
            cycle_kwh=float(item.get("energy_kwh") or 0.0),
            energy_kwh=float(item.get("energy_kwh") or 0.0),
        )
        sig = resolve(registry, other, config)
        if sig is not None:
            if owner_of(sig, appliance["key"]) or sig["id"] in taken:
                continue
            out.append((sig, False))
        else:
            sig = new_signature(registry, other, appliance, entity, now)
            registry.setdefault("signatures", []).append(sig)
            out.append((sig, True))
        taken.add(sig["id"])
    return out
