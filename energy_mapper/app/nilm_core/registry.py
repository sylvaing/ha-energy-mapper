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

Each signature also keeps its occurrences of the last ``occurrence_days``
days (60 by default, at most ``max_occurrences``, the most recent ones) as
compact ``[start, duration_s, power_w]`` arrays, sorted by start: the day
raster of the panel (#65). They come from the stored activations; those of
days no longer stored are kept from the previous registry (and those of a
signature merged into a neighbour go to the neighbour), pruned to the window.

Multi-phase appliances (#26) are learned here too: the candidate cycles of
the stored days (``cycles`` of the day summaries) are clustered on their
phase sequences (:mod:`.cycles`), and each recurring cluster (at least
``min_cycles``) is a signature of kind ``multiphase`` (band ``cycle``, id
``cycle-<heating power>w``) with its template in ``phases``. Its identity
follows the previous signature whose template is within ``cycle_distance``.
Recognised cycles (activations with ``multiphase``) are not clustered with
the steady ones.

Cold appliances (#89) too: each day's report lists those it followed
(``cold``: the model learned in its quiet hours, the runs, the energy with
its estimated part). Those of the stored days with the same steps
(:func:`.cold.alike`) are one signature of kind ``cold`` (band ``cold``,
id ``cold-<power>w``): its model is the weighted median of the days that
learned it, and goes back to the daily analysis (:func:`cold_models`), so
the appliance is followed, or estimated, on a day whose quiet hours taught
nothing.

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

from . import cold as cd
from . import cycles as mp
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
    occurrence_days: int = 60  # rolling window of the occurrences (the day raster)
    max_occurrences: int = 600  # per signature, the most recent ones
    cycle_distance: float = mp.CycleConfig.match_distance  # multiphase: template distance
    min_cycles: int = 3  # multiphase: candidate cycles of a new signature


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


MULTIPHASE = ("multiphase", "cycle")
COLD = cd.GROUP  # cold appliances (#89)


def activation_group(activation: Mapping) -> tuple[str, str]:
    """(kind, band) of a stored activation, as used by the clustering."""
    if activation.get("multiphase"):
        return MULTIPHASE
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
    # occurrences: the window, and from when the stored days cover it (the
    # older ones of the previous registry are kept)
    since = now - config.occurrence_days * DAY_S
    covered_from = min((t for d in days if (t := _day_start(summaries[d])) is not None), default=None)
    occ = _Occurrences(since, covered_from, config.max_occurrences)

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
            if x.get("multiphase"):
                continue  # a recognised cycle: its signature is learned from the candidates
            if a.power > 0 and a.end > a.start:
                groups.setdefault(a.group, []).append(a)

    # 2. cluster each group, then give each cluster the id of the previous
    # signature it matches (same group, power within tolerance)
    out: list[dict] = []
    used_ids = set(prev_sigs)
    tol = config.power_tol
    previous_groups = {_group_of(s["kind"], s["band"]) for s in prev_sigs.values()}
    for g in sorted((set(groups) | previous_groups) - {MULTIPHASE, COLD}):
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
        made: dict[int, dict] = {}
        for i, members in enumerate(clusters):
            sid = ids.get(i) or _new_id(g, members, used_ids)
            used_ids.add(sid)
            sig = _stats(sid, g, members, prev_sigs.get(sid), now, days_w, config, local_hour)
            sig["occurrences"] = occ.of(members, prev_sigs.get(sid))
            made[i] = sig
            if sig["weight"] >= config.forget_weight or is_ignored(sig):
                out.append(sig)
        # previous signatures without a cluster: carried forward, decaying,
        # when nothing is left near their power; merged into a neighbour
        # otherwise (a labeled one is always kept)
        for s in olds:
            if s["id"] in ids.values():
                continue
            near = [
                (abs(c - s["power_w"]), i)
                for i, c in enumerate(centers)
                if abs(c - s["power_w"]) <= tol * s["power_w"]
            ]
            absorbed = bool(near)
            if near:
                # merged into its nearest neighbour: the occurrences of the
                # days no longer stored follow
                target = made[min(near)[1]]
                target["occurrences"] = occ.union(target["occurrences"], occ.old(s))
            sig = _carried(s, carry, days_w)
            sig["occurrences"] = occ.window(s)
            if sig.get("label") or (
                not absorbed and (sig["weight"] >= config.forget_weight or is_ignored(sig))
            ):
                out.append(sig)
    out += _multiphase(summaries, days, prev_sigs, used_ids, now, days_w, carry, occ, config, local_hour)
    out += _cold(summaries, day_weight, prev_sigs, used_ids, now, days_w, carry, occ, config, local_hour)
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


def _day_start(summary: Mapping) -> float | None:
    """Start of a stored day (None when unknown: the day covers nothing)."""
    try:
        return float(summary["start"])
    except (KeyError, TypeError, ValueError):
        starts = [
            a.get("start")
            for a in summary.get("activations") or []
            if isinstance(a.get("start"), int | float)
        ]
        return float(min(starts)) if starts else None


class _Occurrences:
    """The occurrences of the signatures: ``[start, duration_s, power_w]``
    (integers), sorted by start, within the window, the most recent ``cap``."""

    def __init__(self, since: float, covered_from: float | None, cap: int):
        self.since = since
        self.covered_from = covered_from
        self.cap = max(int(cap), 0)

    def _valid(self, sig: Mapping | None) -> list[list[int]]:
        out = []
        for o in (sig or {}).get("occurrences") or []:
            try:
                start, duration, power = (round(float(v)) for v in o[:3])
            except (TypeError, ValueError):
                continue
            if start >= self.since:
                out.append([start, duration, power])
        return out

    def window(self, sig: Mapping | None) -> list[list[int]]:
        """The stored occurrences of ``sig`` still in the window."""
        return self._finish({o[0]: o for o in self._valid(sig)})

    def old(self, sig: Mapping | None) -> list[list[int]]:
        """The occurrences of ``sig`` in the window on days no longer stored."""
        cut = self.covered_from
        return [o for o in self._valid(sig) if cut is None or o[0] < cut]

    def of(self, members: Iterable[_Act], old: Mapping | None) -> list[list[int]]:
        """From the stored activations, and the previous signature's of the
        days no longer stored."""
        found = {
            round(a.start): [round(a.start), round(a.duration), round(a.power)]
            for a in members
            if a.start >= self.since
        }
        for o in self.old(old):
            found.setdefault(o[0], o)
        return self._finish(found)

    def union(self, a: Iterable[list[int]], b: Iterable[list[int]]) -> list[list[int]]:
        found = {o[0]: o for o in a}
        for o in b:
            found.setdefault(o[0], o)
        return self._finish(found)

    def _finish(self, found: Mapping[int, list[int]]) -> list[list[int]]:
        out = sorted(found.values())
        return out[len(out) - self.cap :] if len(out) > self.cap else out


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


def _multiphase(
    summaries, days, prev_sigs, used_ids, now, days_w, carry, occ, config, local_hour
) -> list[dict]:
    """The ``multiphase`` signatures, from the candidate cycles of the days."""
    hl = config.half_life_days
    samples: list[mp.Sample] = []
    for d in days:
        for x in summaries[d].get("cycles") or []:
            try:
                phases = [mp.Phase.from_dict(p) for p in x.get("phases") or []]
                sample = mp.Sample(
                    day=d,
                    start=float(x["start"]),
                    end=float(x["end"]),
                    energy_kwh=float(x.get("energy_kwh") or 0.0),
                    phases=phases,
                    weight=decay(now - float(x["start"]), hl),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if phases and sample.end > sample.start:
                samples.append(sample)
    clusters = mp.cluster(samples, config.cycle_distance)
    clusters.sort(key=lambda m: (-sum(s.weight for s in m), m[0].start))
    templates = [mp.template_of(m) for m in clusters]
    olds = [s for s in prev_sigs.values() if _group_of(s["kind"], s["band"]) == MULTIPHASE]

    def closeness(i: int, old: Mapping) -> float | None:
        """Distance of cluster ``i`` to a previous signature (None: too far)."""
        try:
            phases = [mp.Phase.from_dict(p) for p in old.get("phases") or []]
        except (KeyError, TypeError, ValueError):
            phases = []
        if phases:
            d = mp.distance(templates[i], phases)
            return d if d <= config.cycle_distance else None
        # expected before it is seen (no template yet): by its power
        power = float(old.get("power_w") or 0.0)
        center = weighted_median((s.power, s.weight) for s in clusters[i])
        if power > 0 and center and abs(center - power) <= config.power_tol * power:
            return abs(center - power) / power
        return None

    pairs = sorted(
        (not s.get("label"), d, i, s["id"])
        for i in range(len(clusters))
        for s in olds
        if (d := closeness(i, s)) is not None
    )
    ids: dict[int, str] = {}
    for _, _, i, sid in pairs:
        if i not in ids and sid not in ids.values():
            ids[i] = sid
    out: list[dict] = []
    made: dict[int, dict] = {}
    for i, members in enumerate(clusters):
        if i not in ids and len(members) < config.min_cycles:
            continue  # not recurring (yet)
        sid = ids.get(i) or _new_id(MULTIPHASE, [_SampleAct(s) for s in members], used_ids)
        used_ids.add(sid)
        old = prev_sigs.get(sid)
        sig = _multiphase_stats(sid, members, templates[i], old, now, days_w, config, local_hour)
        sig["occurrences"] = occ.of(members, old)
        made[i] = sig
        if sig["weight"] >= config.forget_weight or is_ignored(sig) or sig.get("label"):
            out.append(sig)
    for s in olds:
        if s["id"] in ids.values():
            continue
        near = [(d, i) for i in made if (d := closeness(i, s)) is not None]
        if near:
            target = made[min(near)[1]]
            target["occurrences"] = occ.union(target["occurrences"], occ.old(s))
        sig = _carried(s, carry, days_w)
        sig["occurrences"] = occ.window(s)
        if sig.get("label") or (not near and (sig["weight"] >= config.forget_weight or is_ignored(sig))):
            out.append(sig)
    return out


# ---------------------------------------------------------------------------
# cold appliances (#89)
# ---------------------------------------------------------------------------
_COLD_LEARNED = ("on_w", "off_w", "power_w", "on_s", "run_s", "pause_s")


@dataclass
class _ColdDay:
    """One cold appliance on one stored day (an item of its ``cold``)."""

    day: str
    item: Mapping
    weight: float  # the day's
    runs: list[_Run]

    @property
    def learned(self) -> float:
        """What the day's own quiet hours say about the model."""
        return self.weight * float(self.item.get("learned_cycles") or 0)


@dataclass
class _Run:
    """A run of a cold appliance, seen as an activation by the occurrences."""

    start: float
    duration: float
    power: float
    weight: float = 1.0


def _cold_days(summaries: Mapping[str, Mapping], day_weight: Mapping[str, float]) -> list[_ColdDay]:
    out = []
    for d in sorted(summaries):
        for x in summaries[d].get("cold") or []:
            try:
                ok = all(float(x[k]) > 0 for k in _COLD_LEARNED) and float(x.get("energy_kwh") or 0) >= 0
                power = float(x["power_w"])
                runs = [
                    _Run(float(c["start"]), float(c["end"]) - float(c["start"]), power)
                    for c in x.get("cycles") or []
                ]
            except (KeyError, TypeError, ValueError):
                continue
            if ok:
                out.append(_ColdDay(d, x, day_weight[d], runs))
    return out


def _cold_value(members: list[_ColdDay], key: str, old: Mapping | None) -> float | None:
    """A value of the model: the weighted median of the days that learned
    it in their own quiet hours; else the previous one; else the days'."""
    value = weighted_median((float(m.item[key]), m.learned) for m in members)
    if value is None and old and old.get(key):
        return float(old[key])
    return value if value is not None else weighted_median((float(m.item[key]), m.weight) for m in members)


def _cold(
    summaries, day_weight, prev_sigs, used_ids, now, days_w, carry, occ, config, local_hour
) -> list[dict]:
    """The ``cold`` signatures, from the cold appliances of the stored days."""
    # the days with the same steps are one appliance; those that learned
    # the most seed the groups
    groups: list[list[_ColdDay]] = []
    for e in sorted(_cold_days(summaries, day_weight), key=lambda e: (-e.learned, e.day)):
        for g in groups:
            if e.day not in {m.day for m in g} and cd.alike(e.item, g[0].item) is not None:
                g.append(e)
                break
        else:
            groups.append([e])
    olds = [s for s in prev_sigs.values() if _group_of(s["kind"], s["band"]) == COLD]

    def closeness(g: list[_ColdDay], old: Mapping) -> float | None:
        d = cd.alike(g[0].item, old)
        if d is not None or old.get("on_w"):
            return d
        # expected before it is seen (no model yet): by its power
        power, mine = float(old.get("power_w") or 0.0), float(g[0].item["power_w"])
        if power > 0 and abs(mine - power) <= config.power_tol * power:
            return abs(mine - power) / power
        return None

    pairs = sorted(
        (not s.get("label"), d, i, s["id"])
        for i, g in enumerate(groups)
        for s in olds
        if (d := closeness(g, s)) is not None
    )
    ids: dict[int, str] = {}
    for _, _, i, sid in pairs:
        if i not in ids and sid not in ids.values():
            ids[i] = sid
    out: list[dict] = []
    for i, members in enumerate(groups):
        sid = ids.get(i)
        if sid is None:
            base = cd.daily_id(_cold_value(members, "power_w", None))
            sid, n = base, 1
            while sid in used_ids:
                n += 1
                sid = f"{base}-{n}"
        used_ids.add(sid)
        old = prev_sigs.get(sid)
        sig = _cold_stats(sid, members, old, now, days_w, config, local_hour)
        sig["occurrences"] = occ.of([r for m in members for r in m.runs], old)
        if sig["weight"] >= config.forget_weight or is_ignored(sig) or sig.get("label"):
            out.append(sig)
    for s in olds:
        if s["id"] in ids.values():
            continue
        sig = _carried(s, carry, days_w)
        sig["occurrences"] = occ.window(s)
        if sig.get("label") or sig["weight"] >= config.forget_weight or is_ignored(sig):
            out.append(sig)
    return out


def _cold_stats(sid, members: list[_ColdDay], old, now, days_w, config, local_hour) -> dict:
    members = sorted(members, key=lambda m: m.day)
    value = {k: _cold_value(members, k, old) for k in _COLD_LEARNED}
    period = value["run_s"] + value["pause_s"]
    hours = [0.0] * 24
    for m in members:
        for r in m.runs:
            hours[local_hour(r.start) % 24] += m.weight
    # the usual run lengths: those of the day that learned the most
    best = max(members, key=lambda m: (m.learned, m.day))
    modes = best.item.get("on_modes_s") if best.learned > 0 else None
    modes = modes or (old or {}).get("on_modes_s") or best.item.get("on_modes_s") or [value["on_s"]]
    energy = [(float(m.item.get("energy_kwh") or 0.0), m.weight) for m in members]
    estimated = sum(float(m.item.get("estimated_kwh") or 0.0) for m in members)
    recent = [(m.day, r) for m in reversed(members) for r in reversed(m.runs)][: config.examples]
    first = members[0].day
    sig = {
        "id": sid,
        "kind": COLD[0],
        "band": COLD[1],
        "label": (old or {}).get("label"),
        "power_w": _r(value["power_w"]),
        "duration_s": _r(value["on_s"]),
        "on_s": _r(value["on_s"], 1),
        "period_s": _r(period, 1),
        "duty": _r(value["run_s"] / period, 2),
        # one use is one run of the compressor
        "weight": round(sum(m.weight * len(m.runs) for m in members), 6),
        "count": sum(len(m.runs) for m in members),
        "days_seen": len({m.day for m in members}),
        "per_week": round(sum(m.weight * len(m.runs) for m in members) / days_w * 7, 2) if days_w else None,
        "first_seen": min(first, old["first_seen"]) if old and old.get("first_seen") else first,
        "last_seen": members[-1].day,
        "hours": [round(h, 2) for h in hours],
        "energy_kwh": round(sum(e for e, _ in energy), 3),
        "weighted_energy_kwh": round(sum(e * w for e, w in energy), 3),
        "examples": [
            {"day": d, "start": r.start, "end": r.start + r.duration, "power_w": round(r.power)}
            for d, r in recent
        ],
        "created_at": (old or {}).get("created_at") or now,
        # the model the daily analysis follows (:func:`cold_models`)
        "on_w": _r(value["on_w"]),
        "off_w": _r(value["off_w"]),
        "on_modes_s": [round(float(s)) for s in modes],
        "run_s": _r(value["run_s"]),
        "pause_s": _r(value["pause_s"]),
        # of ``energy_kwh``: estimated from the duty, where it was not followed
        "estimated_kwh": round(estimated, 3),
    }
    if old and old.get("review"):
        sig["review"] = old["review"]
    return sig


def cold_models(registry: Mapping | None) -> list[dict]:
    """The models of the cold signatures, for the daily analysis
    (``AnalysisConfig.cold_models``): every one that was learned, the ones
    the user ignored marked so (their steps stay theirs, they are not
    reported)."""
    out = []
    for s in (registry or {}).get("signatures") or []:
        if _group_of(s.get("kind"), s.get("band")) != COLD or not s.get("on_w"):
            continue
        model = {k: s.get(k) for k in ("id", *_COLD_LEARNED, "on_modes_s")}
        out.append({**model, "ignored": True} if is_ignored(s) else model)
    return out


class _SampleAct:
    """A candidate cycle seen as an activation by :func:`_new_id`."""

    def __init__(self, sample: mp.Sample):
        self.power = sample.power
        self.weight = sample.weight


def _multiphase_stats(sid, members, template, old, now, days_w, config, local_hour) -> dict:
    weight = sum(s.weight for s in members)
    hours = [0.0] * 24
    for s in members:
        hours[local_hour(s.start) % 24] += s.weight
    seen = sorted({s.day for s in members})
    first = min(seen[0], old["first_seen"]) if old and old.get("first_seen") else seen[0]
    recent = sorted(members, key=lambda s: -s.start)[: config.examples]
    sig = {
        "id": sid,
        "kind": MULTIPHASE[0],
        "band": MULTIPHASE[1],
        "label": (old or {}).get("label"),
        "power_w": _r(weighted_median((s.power, s.weight) for s in members)),
        "duration_s": _r(weighted_median((s.duration, s.weight) for s in members)),
        "on_s": None,
        "period_s": None,
        "duty": None,
        "weight": round(weight, 6),
        "count": len(members),
        "days_seen": len(seen),
        "per_week": round(weight / days_w * 7, 2) if days_w else None,
        "first_seen": first,
        "last_seen": seen[-1],
        "hours": [round(h, 2) for h in hours],
        "energy_kwh": round(sum(s.energy_kwh for s in members), 3),
        "weighted_energy_kwh": round(sum(s.energy_kwh * s.weight for s in members), 3),
        "examples": [
            {"day": s.day, "start": s.start, "end": s.end, "power_w": round(s.power)} for s in recent
        ],
        "created_at": (old or {}).get("created_at") or now,
        "phases": [p.to_dict() for p in template],
        "cycle_kwh": _r(weighted_median((s.energy_kwh, s.weight) for s in members), 3),
    }
    if old and old.get("review"):
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
        # recognised cycles carry the id of their template
        self.multiphase = {
            s["id"] for s in registry.get("signatures") or [] if _group_of(s["kind"], s["band"]) == MULTIPHASE
        }
        self.colds = [s for s in registry.get("signatures") or [] if _group_of(s["kind"], s["band"]) == COLD]
        for s in registry.get("signatures") or []:
            self.centers.setdefault(_group_of(s["kind"], s["band"]), []).append(
                (float(s["power_w"]), s["id"])
            )
        for c in self.centers.values():
            c.sort()

    def match(self, activation: Mapping) -> str | None:
        if activation.get("multiphase"):
            sid = activation.get("signature")
            return sid if sid in self.multiphase else None
        try:
            group = activation_group(activation)
            power = float(activation["power_w"])
        except (KeyError, TypeError, ValueError):
            return None
        return _nearest(self.centers.get(group, []), power, self.tol)

    def cold(self, item: Mapping) -> str | None:
        """Registry id of a cold appliance of a daily report (``cold``):
        the cold signature with the same steps, the nearest first."""
        near = [(d, s["id"]) for s in self.colds if (d := cd.alike(item, s)) is not None]
        return min(near)[1] if near else None

    def near(self, kind: str, band: str, power_w: float, within: float = 2.0) -> list[tuple[str, float]]:
        """The signatures of a group whose power is within ``within`` times
        the tolerance, as ``(id, distance)`` with the distance in tolerances
        (``|power - center| / (tol x center)``), nearest in watts first:
        the first one within a distance of 1 is the one :meth:`match` gives
        (the live candidates rank all of them)."""
        out = []
        for center, sid in self.centers.get(_group_of(kind, band), []):
            if center > 0:
                d = abs(power_w - center) / (self.tol * center)
                if d <= within:
                    out.append((abs(power_w - center), sid, d))
        out.sort(key=lambda x: x[0])
        return [(sid, d) for _, sid, d in out]


def cycle_templates(registry: Mapping | None, validated_only: bool = True) -> dict[str, list[dict]]:
    """``{id: phases}`` of the multiphase signatures the daily analysis
    recognises cycles of (``AnalysisConfig.cycle_templates``): by default
    only those the user validated (named by an appliance, or linked in the
    review), so that the energy of the steady signatures a cycle absorbs
    (its heating phase) only moves once the user agreed."""
    out = {}
    for s in (registry or {}).get("signatures") or []:
        if _group_of(s.get("kind"), s.get("band")) != MULTIPHASE or not s.get("phases") or is_ignored(s):
            continue
        validated = s.get("label") or (s.get("review") or {}).get("state") == "validated"
        if validated or not validated_only:
            out[s["id"]] = list(s["phases"])
    return out


def match(registry: Mapping | None, activation: Mapping) -> str | None:
    return Matcher(registry).match(activation)


def annotate_report(report: dict, registry: Mapping | None) -> dict:
    """Add ``registry_id`` to the activations, the daily signatures (the
    registry id most of its activations have), the cold appliances (#89:
    the cold signature with the same steps) and the signature parts of the
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
    cold = {}
    for c in report.get("cold") or []:
        c["registry_id"] = cold[c.get("id")] = m.cold(c)
    for s in report.get("signatures") or []:
        if s.get("kind") == COLD[0]:
            s["registry_id"] = cold.get(s.get("id"))
            continue
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
# the groups an appliance's signature source can name (a multiphase one is
# learned only: it cannot be expected from a power alone, see the API)
SOURCE_GROUPS = (*GROUPS, MULTIPHASE, COLD)
# typical duration of an expected signature, before its first use is seen
_BAND_DURATION_S = {"short": 120, "medium": 1200, "long": 7200, "session": 1800, "cycle": 5400, "cold": 600}


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
        "occurrences": [],
        "created_at": now,
        "expected": True,
    } | ({"phases": [], "cycle_kwh": None} if (kind, band) == MULTIPHASE else {})
