"""Signature registry: what the house is made of, learned across days.

Every night the activations of all the stored days are clustered together,
each one weighted by its age with an exponential decay (half-life 30 days by
default): a use from 30 days ago counts half, 60 days a quarter... Recent
habits dominate, a replaced appliance fades out in a few weeks, and a rare
one stays known as long as its decayed weight is meaningful.

Identity is stable: the clustering itself starts from scratch every night
(it only depends on the stored activations), then each cluster takes the id
of the previous signature it matches: same kind, same duration band, power
within the tolerance, the closest first, labeled signatures first. So a
cluster that is still there keeps its id from one night to the next, and the
user labels attached to it (v1) stay attached. Clusters that match nothing
get a new id named like the daily ones (``short-2000w``, ``cyc-1400w``...);
the name is only an identity, ``power_w`` is the current typical power.
Previous signatures without a cluster are carried forward with a decaying
weight when nothing is left near their power (a rare appliance whose
activations were all pruned), or dropped when a neighbouring cluster took
their activations (unless labeled).

Daily cluster ids (``Signature.id`` and ``activation["signature"]`` in a
daily report) are recomputed from the power of that day only; registry ids
are the long-lived ones. They follow the same naming, so they often agree,
but only the registry id is an identity: :func:`annotate_report` adds it to
a daily report (``registry_id`` on activations, daily signatures and peak
parts) with :func:`match`, the same rule as the clustering.

Everything here is pure (no I/O, no clock): the caller passes the day
summaries, the previous registry and ``now``.
"""

from __future__ import annotations

import bisect
import datetime as dt
import itertools
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from .signatures import duration_band, signature_id

REGISTRY_VERSION = 1
DAY_S = 86400.0
# a weight below this is forgotten: day summaries, signatures, switched loads
FORGET_WEIGHT = 0.01
# signatures the user ignored are kept after they fade out (below the forget
# weight), so that they are not proposed again; at most this many such ones,
# the most recently ignored first
MAX_FADED_IGNORED = 100
# clusters whose centers are closer than this fraction of the tolerance are
# merged (a unimodal power distribution is one appliance), farther ones are
# kept apart; below 1 so that a merge does not push members out of reach
MERGE_FACTOR = 0.75


@dataclass
class RegistryConfig:
    half_life_days: float = 30.0
    power_tol: float = 0.12  # relative power distance inside one signature
    forget_weight: float = FORGET_WEIGHT
    peak_window_days: int = 30
    peak_relative: float = 0.8
    examples: int = 3
    max_iterations: int = 8


def decay(age_s: float, half_life_days: float) -> float:
    """Weight of something ``age_s`` seconds old (1 when new, 0.5 at one half-life)."""
    return 0.5 ** (max(age_s, 0.0) / DAY_S / half_life_days)


def forget_after_days(half_life_days: float, forget_weight: float = FORGET_WEIGHT) -> float:
    """Age at which one activation's weight falls below ``forget_weight``
    (about 200 days for a 30-day half-life and 1 %)."""
    return half_life_days * math.log2(1.0 / forget_weight)


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
@dataclass
class _Act:
    day: str
    start: float
    end: float
    power: float
    cycling: bool
    duty: float
    on_s: float | None
    period_s: float | None
    weight: float

    @property
    def group(self) -> tuple[str, str]:
        if self.cycling:
            return "cycling", "session"
        return "steady", duration_band(self.end - self.start)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def energy_kwh(self) -> float:
        return self.power * self.duty * self.duration / 3.6e6


def _group_of(kind: str, band: str) -> tuple[str, str]:
    return (kind, "session" if kind == "cycling" else band)


def activation_group(activation: Mapping) -> tuple[str, str]:
    """(kind, band) of a stored activation, as used by the clustering."""
    if activation.get("cycling"):
        return "cycling", "session"
    return "steady", duration_band(float(activation["end"]) - float(activation["start"]))


def day_max_w(summary: Mapping) -> float | None:
    """Highest house power of a stored day (older reports: from the peaks or the curve)."""
    value = summary.get("max_house_w")
    if value is None:
        peaks = [p.get("house_w") for p in summary.get("peaks") or [] if p.get("house_w") is not None]
        curve = [r.get("house") for r in summary.get("curve") or [] if r.get("house") is not None]
        value = max(peaks + curve, default=None)
    return float(value) if value is not None else None


# ---------------------------------------------------------------------------
# weighted statistics
# ---------------------------------------------------------------------------
def weighted_median(pairs: Iterable[tuple[float, float]]) -> float | None:
    """Median of (value, weight) pairs; None when empty."""
    items = sorted((v, w) for v, w in pairs if v is not None and w > 0)
    if not items:
        return None
    half = sum(w for _, w in items) / 2.0
    acc = 0.0
    for v, w in items:
        acc += w
        if acc >= half:
            return v
    return items[-1][0]


def _modes(acts: list[_Act], tol: float) -> list[list[_Act]]:
    """Split activations sorted by power into clusters around their densest
    powers: the heaviest window of +-tol/2 seeds a cluster that takes every
    activation within +-tol, then the rest is split the same way."""
    out = []
    rest = list(acts)
    while rest:
        powers = [a.power for a in rest]
        cum = list(itertools.accumulate((a.weight for a in rest), initial=0.0))
        best, best_w = 0, -1.0
        for i, p in enumerate(powers):
            lo = bisect.bisect_left(powers, p * (1 - tol / 2))
            hi = bisect.bisect_right(powers, p * (1 + tol / 2))
            if cum[hi] - cum[lo] > best_w:
                best, best_w = i, cum[hi] - cum[lo]
        p = powers[best]
        lo = bisect.bisect_left(powers, p * (1 - tol))
        hi = bisect.bisect_right(powers, p * (1 + tol))
        out.append(rest[lo:hi])
        rest = rest[:lo] + rest[hi:]
    return out


# ---------------------------------------------------------------------------
# clustering of one (kind, band) group
# ---------------------------------------------------------------------------
def _nearest(centers: list[tuple[float, object]], power: float, tol: float):
    """Key of the nearest center within ``tol`` (relative to the center)."""
    if not centers:
        return None
    i = bisect.bisect_left(centers, power, key=lambda c: c[0])
    best, best_d = None, math.inf
    for j in (i - 1, i):
        if 0 <= j < len(centers):
            c, key = centers[j]
            d = abs(power - c)
            if d <= tol * c and d < best_d:
                best, best_d = key, d
    return best


def _cluster_group(acts: list[_Act], tol: float, max_iter: int) -> list[list[_Act]]:
    """Cluster one (kind, band) group on power, from scratch (the result
    depends on the activations only, not on the previous registry): seed
    clusters at the densest powers, assign each activation to the nearest
    center within ``tol``, re-centre on the weighted median, merge the
    centers that came close to each other, until stable."""
    acts = sorted(acts, key=lambda a: a.power)
    centers: list[float] = []
    clusters: list[list[_Act]] = []
    for _ in range(max_iter):
        ordered = sorted((c, i) for i, c in enumerate(centers))
        members: list[list[_Act]] = [[] for _ in centers]
        unassigned: list[_Act] = []
        for a in acts:
            k = _nearest(ordered, a.power, tol)
            if k is None:
                unassigned.append(a)
            else:
                members[k].append(a)
        members = [m for m in members if m] + _modes(unassigned, tol)
        members = _merge_close(members, tol * MERGE_FACTOR)
        new_centers = [weighted_median((a.power, a.weight) for a in m) for m in members]
        stable = not unassigned and new_centers == centers
        centers, clusters = new_centers, members
        if stable:
            break
    return clusters


def _merge_close(clusters: list[list[_Act]], tol: float) -> list[list[_Act]]:
    """Merge neighbouring clusters whose centers are within ``tol``."""
    centered = sorted(
        ((weighted_median((a.power, a.weight) for a in m), m) for m in clusters), key=lambda x: x[0]
    )
    out: list[tuple[float, list[_Act]]] = []
    for c2, m in centered:
        if out:
            c1, prev = out[-1]
            if abs(c2 - c1) <= tol * min(c1, c2):
                merged = prev + m
                out[-1] = (weighted_median((a.power, a.weight) for a in merged), merged)
                continue
        out.append((c2, m))
    return [m for _, m in out]


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------
def empty_registry(config: RegistryConfig | None = None) -> dict:
    config = config or RegistryConfig()
    return {
        "version": REGISTRY_VERSION,
        "updated_at": None,
        "half_life_days": config.half_life_days,
        "power_tol": config.power_tol,
        "days": 0,
        "first_day": None,
        "last_day": None,
        "signatures": [],
        "switched_loads": [],
        "peak": None,
    }


def update(
    previous: Mapping | None,
    summaries: Mapping[str, Mapping],
    now: float,
    config: RegistryConfig | None = None,
    local_hour: Callable[[float], int] | None = None,
) -> dict:
    """New registry from the day summaries ``{"YYYY-MM-DD": report without
    curve}`` and the previous registry (None: rebuild from scratch).

    ``now`` is the reference time of the weights (the end of the last day).
    Previous signatures that no longer have activations are carried forward
    with their weight decayed, and dropped once it is negligible unless they
    have a user label."""
    config = config or RegistryConfig()
    local_hour = local_hour or (lambda ts: time.localtime(ts).tm_hour)
    hl = config.half_life_days
    prev = previous if previous and previous.get("version") == REGISTRY_VERSION else None
    prev_sigs = {s["id"]: s for s in (prev or {}).get("signatures", [])}
    carry = decay(now - prev["updated_at"], hl) if prev and prev.get("updated_at") is not None else 1.0

    days = sorted(summaries)
    day_weight = {d: decay(now - _midday(summaries[d], now), hl) for d in days}
    days_w = sum(day_weight.values())

    # 1. activations, weighted by age
    groups: dict[tuple[str, str], list[_Act]] = {}
    for d in days:
        for x in summaries[d].get("activations") or []:
            try:
                a = _Act(
                    day=d,
                    start=float(x["start"]),
                    end=float(x["end"]),
                    power=float(x["power_w"]),
                    cycling=bool(x.get("cycling")),
                    duty=float(x.get("duty") or 1.0),
                    on_s=x.get("on_s"),
                    period_s=x.get("period_s"),
                    weight=decay(now - float(x["start"]), hl),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if a.power > 0 and a.end > a.start:
                groups.setdefault(a.group, []).append(a)

    # 2. cluster each group, then give each cluster the id of the previous
    # signature it matches (same group, power within tolerance)
    out: list[dict] = []
    used_ids = set(prev_sigs)
    tol = config.power_tol
    for g in sorted(set(groups) | {_group_of(s["kind"], s["band"]) for s in prev_sigs.values()}):
        clusters = _cluster_group(groups.get(g, []), tol, config.max_iterations)
        clusters.sort(key=lambda m: -sum(a.weight for a in m))
        centers = [weighted_median((a.power, a.weight) for a in m) for m in clusters]
        olds = [s for s in prev_sigs.values() if _group_of(s["kind"], s["band"]) == g]
        pairs = sorted(
            (not s.get("label"), abs(c - s["power_w"]) / s["power_w"], i, s["id"])
            for i, c in enumerate(centers)
            for s in olds
            if abs(c - s["power_w"]) <= tol * s["power_w"]
        )
        ids: dict[int, str] = {}
        for _, _, i, sid in pairs:
            if i not in ids and sid not in ids.values():
                ids[i] = sid
        for i, members in enumerate(clusters):
            sid = ids.get(i) or _new_id(g, members, used_ids)
            used_ids.add(sid)
            sig = _stats(sid, g, members, prev_sigs.get(sid), now, days_w, config, local_hour)
            if sig["weight"] >= config.forget_weight or is_ignored(sig):
                out.append(sig)
        # previous signatures without a cluster: carried forward, decaying,
        # when nothing is left near their power; merged into a neighbour
        # otherwise (a labeled one is always kept)
        for s in olds:
            if s["id"] in ids.values():
                continue
            absorbed = any(abs(c - s["power_w"]) <= tol * s["power_w"] for c in centers)
            sig = _carried(s, carry, days_w)
            if sig.get("label") or (
                not absorbed and (sig["weight"] >= config.forget_weight or is_ignored(sig))
            ):
                out.append(sig)
    out = _cap_faded_ignored(out, config.forget_weight)
    out.sort(key=lambda s: (-s["weight"], s["id"]))

    registry = empty_registry(config)
    registry.update(
        {
            "updated_at": now,
            "days": len(days),
            "first_day": days[0] if days else (prev or {}).get("first_day"),
            "last_day": days[-1] if days else (prev or {}).get("last_day"),
            "signatures": out,
            "switched_loads": _switched_memory(prev, summaries, day_weight, carry, config),
        }
    )
    today = dt.datetime.fromtimestamp(now, dt.UTC).date().isoformat() if not days else _next_day(days[-1])
    maxima = {d: m for d in days if (m := day_max_w(summaries[d])) is not None}
    ref = peak_reference(maxima, today, config.peak_window_days)
    registry["peak"] = {
        "window_days": config.peak_window_days,
        "relative": config.peak_relative,
        "reference_w": round(ref) if ref is not None else None,
        "threshold_w": round(config.peak_relative * ref) if ref is not None else None,
        "days": sum(1 for d in maxima if _in_window(d, today, config.peak_window_days)),
    }
    return registry


def is_ignored(sig: Mapping) -> bool:
    """The user ignored this signature in the review (never proposed again)."""
    return (sig.get("review") or {}).get("state") == "ignored"


def _cap_faded_ignored(sigs: list[dict], forget_weight: float) -> list[dict]:
    """Bound the ignored signatures kept after fading out: the most recently
    ignored ``MAX_FADED_IGNORED`` stay."""
    faded = [s for s in sigs if not s.get("label") and s["weight"] < forget_weight and is_ignored(s)]
    if len(faded) <= MAX_FADED_IGNORED:
        return sigs
    faded.sort(key=lambda s: -float((s.get("review") or {}).get("at") or 0))
    drop = {id(s) for s in faded[MAX_FADED_IGNORED:]}
    return [s for s in sigs if id(s) not in drop]


def _midday(summary: Mapping, default: float) -> float:
    try:
        return (float(summary["start"]) + float(summary["end"])) / 2
    except (KeyError, TypeError, ValueError):
        return default


def _next_day(day: str) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


def _new_id(group, members, used_ids) -> str:
    kind, band = group
    power = weighted_median((a.power, a.weight) for a in members)
    base = signature_id(kind, band, power)
    sid, n = base, 1
    while sid in used_ids:
        n += 1
        sid = f"{base}-{n}"
    return sid


def _r(value, digits=0):
    if value is None:
        return None
    return round(value) if digits == 0 else round(value, digits)


def _stats(sid, group, members, old, now, days_w, config, local_hour) -> dict:
    kind, band = group
    weight = sum(a.weight for a in members)
    hours = [0.0] * 24
    for a in members:
        hours[local_hour(a.start) % 24] += a.weight
    seen = sorted({a.day for a in members})
    first = min(seen[0], old["first_seen"]) if old and old.get("first_seen") else seen[0]
    recent = sorted(members, key=lambda a: -a.start)[: config.examples]
    cycling = kind == "cycling"
    sig = {
        "id": sid,
        "kind": kind,
        "band": band,
        "label": (old or {}).get("label"),
        "power_w": _r(weighted_median((a.power, a.weight) for a in members)),
        "duration_s": _r(weighted_median((a.duration, a.weight) for a in members)),
        "on_s": _r(weighted_median((a.on_s, a.weight) for a in members), 1) if cycling else None,
        "period_s": _r(weighted_median((a.period_s, a.weight) for a in members), 1) if cycling else None,
        "duty": _r(weighted_median((a.duty, a.weight) for a in members), 2) if cycling else None,
        "weight": round(weight, 6),
        "count": len(members),
        "days_seen": len(seen),
        "per_week": round(weight / days_w * 7, 2) if days_w else None,
        "first_seen": first,
        "last_seen": seen[-1],
        "hours": [round(h, 2) for h in hours],
        "energy_kwh": round(sum(a.energy_kwh for a in members), 3),
        "weighted_energy_kwh": round(sum(a.energy_kwh * a.weight for a in members), 3),
        "examples": [
            {"day": a.day, "start": a.start, "end": a.end, "power_w": round(a.power)} for a in recent
        ],
        "created_at": (old or {}).get("created_at") or now,
    }
    if old and old.get("review"):
        # the user's decision in the review (#10) follows the identity
        sig["review"] = old["review"]
    return sig


def _carried(old, carry, days_w) -> dict:
    """A previous signature without stored activations any more."""
    sig = dict(old)
    sig["weight"] = round(old.get("weight", 0.0) * carry, 6)
    sig["hours"] = [round(h * carry, 2) for h in old.get("hours") or [0.0] * 24]
    sig["weighted_energy_kwh"] = round((old.get("weighted_energy_kwh") or 0.0) * carry, 6)
    sig["per_week"] = round(sig["weight"] / days_w * 7, 2) if days_w else None
    return sig


# ---------------------------------------------------------------------------
# switched loads
# ---------------------------------------------------------------------------
def _switched_memory(prev, summaries, day_weight, carry, config) -> list[dict]:
    """Learned power and polarity of the switch-driven loads. Only days on
    which some transitions were confirmed by the load count; a day without
    transitions keeps the last known values."""
    obs: dict[str, list[tuple[str, dict, float]]] = {}
    for d in sorted(summaries):
        for k in summaries[d].get("known_loads") or []:
            entity = k.get("entity_id")
            if entity and (k.get("matched") or 0) > 0 and (k.get("power_w") or 0) > 0:
                obs.setdefault(entity, []).append((d, k, day_weight[d]))

    old = {x["entity_id"]: x for x in (prev or {}).get("switched_loads", [])}
    out = []
    for entity in sorted(set(obs) | set(old)):
        rows = obs.get(entity)
        if not rows:
            x = dict(old[entity])
            x["weight"] = round(x.get("weight", 0.0) * carry, 6)
            if x["weight"] >= config.forget_weight:
                out.append(x)
            continue
        votes: dict[bool, float] = {}
        for _, k, w in rows:
            inv = bool(k.get("inverted"))
            votes[inv] = votes.get(inv, 0.0) + w * k["matched"]
        inverted = max(votes, key=lambda v: votes[v])
        last = [k for _, k, _ in rows if bool(k.get("inverted")) == inverted][-1]
        weight = sum(w for _, _, w in rows)
        if weight < config.forget_weight:
            continue
        first = rows[0][0]
        if entity in old and old[entity].get("first_seen"):
            first = min(first, old[entity]["first_seen"])
        out.append(
            {
                "entity_id": entity,
                "name": rows[-1][1].get("name") or entity,
                "power_w": _r(weighted_median((k["power_w"], w * k["matched"]) for _, k, w in rows)),
                "inverted": inverted,
                "on_states": list(last.get("on_states") or []),
                "weight": round(weight, 6),
                "days_seen": len(rows),
                "first_seen": first,
                "last_seen": rows[-1][0],
            }
        )
    return out


# ---------------------------------------------------------------------------
# peak threshold
# ---------------------------------------------------------------------------
def _in_window(day: str, until: str, window_days: int) -> bool:
    d, u = dt.date.fromisoformat(day), dt.date.fromisoformat(until)
    return u - dt.timedelta(days=window_days) <= d < u


def peak_reference(maxima: Mapping[str, float], day: str, window_days: int = 30) -> float | None:
    """Highest daily maximum of the ``window_days`` days before ``day`` (ISO
    dates); None when none is stored: the threshold is then relative to the
    day's own maximum."""
    values = [m for d, m in maxima.items() if m is not None and _in_window(d, day, window_days)]
    return max(values) if values else None


# ---------------------------------------------------------------------------
# registry ids in daily reports
# ---------------------------------------------------------------------------
class Matcher:
    """Registry id of an activation: the nearest signature of the same kind
    and duration band within the power tolerance (the clustering's rule)."""

    def __init__(self, registry: Mapping | None):
        registry = registry or {}
        self.tol = float(registry.get("power_tol") or RegistryConfig.power_tol)
        self.centers: dict[tuple[str, str], list[tuple[float, str]]] = {}
        for s in registry.get("signatures") or []:
            self.centers.setdefault(_group_of(s["kind"], s["band"]), []).append(
                (float(s["power_w"]), s["id"])
            )
        for c in self.centers.values():
            c.sort()

    def match(self, activation: Mapping) -> str | None:
        try:
            group = activation_group(activation)
            power = float(activation["power_w"])
        except (KeyError, TypeError, ValueError):
            return None
        return _nearest(self.centers.get(group, []), power, self.tol)


def match(registry: Mapping | None, activation: Mapping) -> str | None:
    return Matcher(registry).match(activation)


def annotate_report(report: dict, registry: Mapping | None) -> dict:
    """Add ``registry_id`` to the activations, the daily signatures (the
    registry id most of its activations have) and the signature parts of the
    peaks. Modifies and returns ``report``; the daily ids are kept."""
    m = Matcher(registry)
    by_start: dict[float, str | None] = {}
    votes: dict[str, dict[str, int]] = {}
    for a in report.get("activations") or []:
        rid = m.match(a)
        a["registry_id"] = rid
        by_start[a.get("start")] = rid
        if a.get("signature") and rid:
            v = votes.setdefault(a["signature"], {})
            v[rid] = v.get(rid, 0) + 1
    for s in report.get("signatures") or []:
        v = votes.get(s.get("id"))
        s["registry_id"] = max(v, key=lambda r: (v[r], r)) if v else None
    for p in report.get("peaks") or []:
        for part in p.get("parts") or []:
            if part.get("kind") == "signature":
                part["registry_id"] = by_start.get(part.get("since"))
    return report


# ---------------------------------------------------------------------------
# user appliances: labels and expected signatures
# ---------------------------------------------------------------------------
# kind, band of the groups a signature can be in
GROUPS = (("steady", "short"), ("steady", "medium"), ("steady", "long"), ("cycling", "session"))
# typical duration of an expected signature, before its first use is seen
_BAND_DURATION_S = {"short": 120, "medium": 1200, "long": 7200, "session": 1800}


def nearest_signature(registry: Mapping | None, kind: str, band: str, power_w: float) -> dict | None:
    """The signature of ``registry`` an activation of this group and power
    would be matched to (the clustering's rule), or None."""
    registry = registry or {}
    tol = float(registry.get("power_tol") or RegistryConfig.power_tol)
    group = _group_of(kind, band)
    sigs = {s["id"]: s for s in registry.get("signatures") or [] if _group_of(s["kind"], s["band"]) == group}
    centers = sorted((float(s["power_w"]), sid) for sid, s in sigs.items() if s.get("power_w"))
    sid = _nearest(centers, float(power_w), tol)
    return sigs.get(sid) if sid else None


def expected_signature(
    registry: Mapping | None, kind: str, band: str, power_w: float, label: str | None, now: float
) -> dict:
    """A signature the user expects before it is seen ("Kettle, 2000 W"):
    no activation, weight 0, kept while labelled. The first cluster of the
    same group within the power tolerance takes its id (labelled signatures
    are matched first), so the appliance is recognised from its first use."""
    kind, band = _group_of(kind, band)
    used = {s["id"] for s in (registry or {}).get("signatures") or []}
    base = signature_id(kind, band, power_w)
    sid, n = base, 1
    while sid in used:
        n += 1
        sid = f"{base}-{n}"
    cycling = kind == "cycling"
    return {
        "id": sid,
        "kind": kind,
        "band": band,
        "label": label,
        "power_w": round(power_w),
        "duration_s": _BAND_DURATION_S[band],
        "on_s": None,
        "period_s": None,
        "duty": 0.5 if cycling else None,
        "weight": 0.0,
        "count": 0,
        "days_seen": 0,
        "per_week": 0.0,
        "first_seen": None,
        "last_seen": None,
        "hours": [0.0] * 24,
        "energy_kwh": 0.0,
        "weighted_energy_kwh": 0.0,
        "examples": [],
        "created_at": now,
        "expected": True,
    }
