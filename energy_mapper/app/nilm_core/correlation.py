"""Which Home Assistant entity a signature may be (#41).

A signature's activations start with an *on* step and end with an *off*
step in the house load. An entity whose state changes at those times (a
light, a fan, a connected appliance reporting its operation state...) is
probably the appliance. For each candidate entity this module scores:

- ``on_share``: the share of the signature's on steps with a change of the
  entity within ``±window`` into one of its *on states*;
- ``off_share``: the same for the off steps, into any other state;
- ``score``: both together, the share of all the steps matched (what the
  panel shows: "Probably: <entity> (92 %)");
- ``explained``: the reverse share, the entity's changes that happen near a
  step of the signature. An entity changing all the time matches every step
  by chance but explains few of its own changes: it is rejected.

The on states are learned, not configured: the states the entity enters at
more on steps than off steps. So the polarity comes for free (a pilot-wire
relay is *off* while its radiator heats), and so do multi-state entities (a
washing machine reporting ``running``, an HVAC mode ``heat``).

**Containment** (#43). Edge coincidence is the wrong test for an entity that
stays in a state for long periods while the load cycles inside them (a
thermostat ``heating`` for an hour while its radiator switches on and off on
its own): most of its steps fall far from any change. What matters is that
the signature only runs while the entity is in that state. For each entity
``score_containment`` measures:

- ``contained``: the share of the signature's on-time (its activations)
  spent while the entity is in one of its on states;
- ``time_share``: the share of the time the entity spends in its on states;
- ``sessions`` / ``matches``: the signature's uses grouped in sessions (a gap
  of ``session_gap_s`` at least between two sessions, so that uses close
  together count once), and those spent mostly inside;
- ``chance``: the probability that so many sessions fall inside by chance,
  if they ignored the entity (binomial tail, the time share being the
  probability). It rejects an entity that is "on" most of the time;
- ``used``: the share of the entity's on periods during which the signature
  runs at least once (the entity's on-time actually used). It rejects an
  entity that is often on without the load.

The on states of containment are the states holding more of the
signature's on-time than of the entity's time (a lift above 1). A candidate
is accepted on either test; the edge test wins when both pass.

Everything here is pure (no I/O, no clock): the caller passes the day
summaries, the registry ids of the signatures and the entity changes of
each day (``day_changes``), already read from the recorder.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .registry import Matcher

# states that are neither on nor off: not a change
UNAVAILABLE = {"unavailable", "unknown", "none", ""}


@dataclass(frozen=True)
class CorrelationConfig:
    min_score: float = 0.8  # share of the steps matched
    min_matches: int = 5  # steps matched, at least
    min_days: int = 3  # on that many different days
    min_explained: float = 0.3  # share of the entity's changes near a step
    # a change within this window of a step coincides with it: twice the
    # sampling interval of the day, within these bounds
    window_min_s: float = 60.0
    window_max_s: float = 300.0
    # an entity changing more often than this on a day is not compared that day
    max_changes_per_day: int = 100
    # the same for an attribute series (#43): a thermostat's hvac_action may
    # follow a short regulation cycle, which containment copes with
    max_attribute_changes_per_day: int = 300
    top: int = 3  # candidates kept per signature
    # containment (#43): contained >= min_score, at least min_matches sessions
    # inside, on min_days days, used >= min_used and chance <= max_chance
    min_used: float = 0.3
    max_chance: float = 1e-4
    session_gap_s: float = 1800.0


@dataclass(frozen=True)
class Edge:
    ts: float
    on: bool


def window_s(summary: Mapping, config: CorrelationConfig) -> float:
    """The coincidence window of a day: twice its mean sampling interval,
    within the bounds of ``config``."""
    try:
        span = float(summary["end"]) - float(summary["start"])
        samples = int((summary.get("quality") or {}).get("samples") or 0)
    except (KeyError, TypeError, ValueError):
        return config.window_min_s
    interval = span / samples if samples > 0 else 0.0
    return min(max(2.0 * interval, config.window_min_s), config.window_max_s)


def day_changes(
    points: Iterable[tuple[float, str]], start: float, end: float, max_changes: int
) -> tuple[list[tuple[float, str]] | None, int]:
    """The state changes of one entity in ``[start, end)`` from its recorded
    states (the first one being the state at ``start``): ``(changes,
    count)``, ``changes`` None when there are more than ``max_changes``.

    The first known state is not a change; unavailable states are skipped
    (a change through ``unavailable`` back to the same state is none)."""
    _, changes, count = day_states(points, start, end, max_changes)
    return changes, count


def day_states(
    points: Iterable[tuple[float, str]], start: float, end: float, max_changes: int
) -> tuple[str | None, list[tuple[float, str]] | None, int]:
    """``day_changes`` with the state at ``start`` first: ``(initial,
    changes, count)``; ``initial`` None when no state is known at ``start``
    (the time before the first change is then unknown)."""
    prev: str | None = None
    initial: str | None = None
    out: list[tuple[float, str]] = []
    count = 0
    for ts, s in sorted(points, key=lambda p: p[0]):
        s = str(s)
        if s.lower() in UNAVAILABLE:
            continue
        if ts <= start:
            initial = s
        if prev is None or s == prev:
            prev = s
            continue
        prev = s
        if start <= ts < end:
            count += 1
            if count <= max_changes:
                out.append((round(ts, 1), s))
    return initial, (out if count <= max_changes else None), count


def signature_edges(
    summaries: Mapping[str, Mapping],
    registry: Mapping | None,
    sids: Iterable[str],
    config: CorrelationConfig,
) -> dict[str, dict[str, list[Edge]]]:
    """``{sid: {day: [Edge]}}``: the on and off steps of the activations of
    the signatures ``sids`` (registry ids, the clustering's rule). A step
    belongs to the day it falls in; steps closer than the window to the
    day's bounds are left out (the entity changes are read per day)."""
    wanted = set(sids)
    matcher = Matcher(registry)
    out: dict[str, dict[str, list[Edge]]] = {}
    for day in sorted(summaries):
        summary = summaries[day]
        try:
            start, end = float(summary["start"]), float(summary["end"])
        except (KeyError, TypeError, ValueError):
            continue
        margin = window_s(summary, config)
        seen: set[tuple[str, int, bool]] = set()
        for a in summary.get("activations") or []:
            sid = matcher.match(a)
            if sid not in wanted:
                continue
            for key, on in (("start", True), ("end", False)):
                try:
                    ts = float(a[key])
                except (KeyError, TypeError, ValueError):
                    continue
                ident = (sid, round(ts), on)
                if not start + margin <= ts < end - margin or ident in seen:
                    continue
                seen.add(ident)
                out.setdefault(sid, {}).setdefault(day, []).append(Edge(ts, on))
    return out


def score_entity(
    edges: Mapping[str, list[Edge]],
    changes: Mapping[str, list[tuple[float, str]] | None],
    windows: Mapping[str, float],
) -> dict | None:
    """Score one entity against one signature. ``edges``: the signature's
    steps per day; ``changes``: the entity's changes on every analysed day
    (None: the day is not compared, no history or too many changes; the
    changes of a day without step count against ``explained``). None when no
    step can be compared or no on state stands out."""
    n_on = n_off = 0
    hits: list[tuple[bool, str, str]] = []  # (on step, state entered, day)
    total = explained = 0
    days = 0
    for day, ch in changes.items():
        if ch is None:
            continue
        steps = edges.get(day) or []
        days += 1 if steps else 0
        w = windows.get(day, 60.0)
        times = [c[0] for c in ch]
        total += len(ch)
        near: set[int] = set()
        for e in steps:
            if e.on:
                n_on += 1
            else:
                n_off += 1
            i = bisect.bisect_left(times, e.ts - w)
            j = bisect.bisect_right(times, e.ts + w)
            if i < j:
                k = min(range(i, j), key=lambda k: abs(times[k] - e.ts))
                hits.append((e.on, ch[k][1], day))
                near.update(range(i, j))
        explained += len(near)
    n = n_on + n_off
    if not n:
        return None
    entered_on = Counter(s for on, s, _ in hits if on)
    entered_off = Counter(s for on, s, _ in hits if not on)
    on_states = sorted(s for s, c in entered_on.items() if c > entered_off.get(s, 0))
    if not on_states:
        return None
    ok = [(on, d) for on, s, d in hits if (s in on_states) == on]
    ok_on = sum(1 for on, _ in ok if on)
    return {
        "score": round(len(ok) / n, 3),
        "on_share": round(ok_on / n_on, 3) if n_on else None,
        "off_share": round((len(ok) - ok_on) / n_off, 3) if n_off else None,
        "explained": round(explained / total, 3) if total else 0.0,
        "matches": len(ok),
        "steps": n,
        "days": len({d for _, d in ok}),
        "days_compared": days,
        "on_states": on_states,
        # a two-state entity that consumes while "off" (pilot wire)
        "inverted": on_states == ["off"],
    }


def signature_uses(
    summaries: Mapping[str, Mapping],
    registry: Mapping | None,
    sids: Iterable[str],
) -> dict[str, dict[str, list[tuple[float, float]]]]:
    """``{sid: {day: [(start, end)]}}``: the activations of the signatures
    ``sids`` (registry ids, the clustering's rule), clipped to their day,
    sorted."""
    wanted = set(sids)
    matcher = Matcher(registry)
    out: dict[str, dict[str, set[tuple[float, float]]]] = {}
    for day in sorted(summaries):
        summary = summaries[day]
        try:
            start, end = float(summary["start"]), float(summary["end"])
        except (KeyError, TypeError, ValueError):
            continue
        for a in summary.get("activations") or []:
            sid = matcher.match(a)
            if sid not in wanted:
                continue
            try:
                a0, a1 = max(float(a["start"]), start), min(float(a["end"]), end)
            except (KeyError, TypeError, ValueError):
                continue
            if a1 > a0:
                out.setdefault(sid, {}).setdefault(day, set()).add((a0, a1))
    return {sid: {d: sorted(u) for d, u in per.items()} for sid, per in out.items()}


def segments(
    initial: str | None, changes: list[tuple[float, str]], start: float, end: float
) -> list[tuple[float, float, str]]:
    """The periods ``(from, to, state)`` of a day in which the entity's state
    is known: from ``start`` (when ``initial`` is known) or from its first
    change, to ``end``."""
    out: list[tuple[float, float, str]] = []
    t, state = start, initial
    for ts, s in changes:
        ts = min(max(ts, start), end)
        if state is not None and ts > t:
            out.append((t, ts, state))
        t, state = ts, s
    if state is not None and end > t:
        out.append((t, end, state))
    return out


def binomial_tail(n: int, k: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    lfn = math.lgamma(n + 1)
    total = sum(
        math.exp(lfn - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
        for i in range(k, n + 1)
    )
    return min(total, 1.0)


def _overlap(
    a0: float, a1: float, segs: list[tuple[float, float, str]], starts: list[float]
) -> list[tuple[str, float]]:
    """Time of ``[a0, a1)`` spent in each state of ``segs``."""
    out = []
    i = max(bisect.bisect_right(starts, a0) - 1, 0)
    n = len(segs)
    while i < n and segs[i][0] < a1:
        s0, s1, state = segs[i]
        d = (a1 if a1 < s1 else s1) - (a0 if a0 > s0 else s0)
        if d > 0:
            out.append((state, d))
        i += 1
    return out


def _on_periods(segs: list[tuple[float, float, str]], on: set[str]) -> list[tuple[float, float]]:
    """The periods of consecutive segments in an on state."""
    out: list[list[float]] = []
    prev_on = False
    for s0, s1, state in segs:
        if state in on:
            if prev_on and out[-1][1] == s0:
                out[-1][1] = s1
            else:
                out.append([s0, s1])
        prev_on = state in on
    return [(a, b) for a, b in out]


def score_containment(
    uses: Mapping[str, list[tuple[float, float]]],
    day_segments: Mapping[str, list[tuple[float, float, str]] | None],
    config: CorrelationConfig,
    full: bool = True,
) -> dict | None:
    """Score how much a signature runs only while one entity is in some
    states (see the module's docstring). ``uses``: the signature's
    activations per day; ``day_segments``: the entity's known periods
    (``segments``) on every compared day (None or empty: not compared).
    None when nothing can be compared or no state stands out, and, unless
    ``full``, when the share contained is below ``config.min_score`` (it
    cannot be accepted: the rest is not computed)."""
    per_day = []
    time_in: dict[str, float] = {}
    use_in: dict[str, float] = {}
    for day, segs in day_segments.items():
        if not segs:
            continue
        for s0, s1, state in segs:
            time_in[state] = time_in.get(state, 0.0) + (s1 - s0)
        day_uses = uses.get(day) or []
        if not day_uses:
            per_day.append((day, segs, []))
            continue
        starts = [x[0] for x in segs]
        overlaps = []
        for a0, a1 in day_uses:
            ov = _overlap(a0, a1, segs, starts)
            for state, d in ov:
                use_in[state] = use_in.get(state, 0.0) + d
            overlaps.append((a0, a1, ov))
        per_day.append((day, segs, overlaps))
    known_time, known_use = sum(time_in.values()), sum(use_in.values())
    if known_time <= 0 or known_use <= 0:
        return None
    # a lift above 1: the state holds more of the signature's on-time than of the time
    on = {s for s, u in use_in.items() if u / known_use > time_in[s] / known_time}
    time_share = sum(time_in[s] for s in on) / known_time
    if not on or time_share >= 1.0:
        return None
    contained = sum(use_in[s] for s in on) / known_use
    if not full and contained < config.min_score:
        return None
    sessions = inside = periods = used = 0
    days: set[str] = set()
    for day, segs, overlaps in per_day:
        # sessions: uses closer than the gap belong together
        total = within = 0.0
        last_end: float | None = None
        for a0, a1, ov in overlaps:
            if last_end is not None and a0 - last_end >= config.session_gap_s and total > 0:
                sessions += 1
                if within >= 0.5 * total:
                    inside += 1
                    days.add(day)
                total = within = 0.0
            for state, d in ov:
                total += d
                if state in on:
                    within += d
            last_end = a1 if last_end is None else max(last_end, a1)
        if total > 0:
            sessions += 1
            if within >= 0.5 * total:
                inside += 1
                days.add(day)
        for p0, p1 in _on_periods(segs, on):
            periods += 1
            if any(a0 < p1 and a1 > p0 for a0, a1, _ in overlaps):
                used += 1
    if not sessions:
        return None
    return {
        "match": "containment",
        "score": round(contained, 3),
        "contained": round(contained, 3),
        "time_share": round(time_share, 3),
        "used": round(used / periods, 3) if periods else 0.0,
        "matches": inside,
        "sessions": sessions,
        "days": len(days),
        "days_compared": len(per_day),
        "chance": float(f"{binomial_tail(sessions, inside, time_share):.3g}"),
        "on_states": sorted(on),
        "inverted": sorted(on) == ["off"],
    }


def accepted_containment(candidate: Mapping, config: CorrelationConfig) -> bool:
    return (
        candidate["contained"] >= config.min_score
        and candidate["matches"] >= config.min_matches
        and candidate["days"] >= config.min_days
        and candidate["used"] >= config.min_used
        and candidate["chance"] <= config.max_chance
    )


def accepted(candidate: Mapping, config: CorrelationConfig) -> bool:
    return (
        candidate["score"] >= config.min_score
        and candidate["matches"] >= config.min_matches
        and candidate["days"] >= config.min_days
        and candidate["explained"] >= config.min_explained
    )


def suggest(
    summaries: Mapping[str, Mapping],
    registry: Mapping | None,
    sids: Iterable[str],
    changes: Mapping[str, Mapping[str, list[tuple[float, str]] | None]],
    config: CorrelationConfig | None = None,
    initial: Mapping[str, Mapping[str, str | None]] | None = None,
) -> dict[str, list[dict]]:
    """``{sid: [candidate, ...]}``: for each signature, the entities above
    the thresholds of the edge test (``match: "edge"``) or else of the
    containment test (``match: "containment"``): the edge matches first,
    then by score (at most ``config.top``). ``changes``: ``{day: {entity:
    changes or None}}`` (``day_changes``); ``initial``: ``{day: {entity:
    state at the start of the day}}`` (``day_states``; without it, the time
    before an entity's first change of a day is unknown). An entity may be
    any series key, an attribute's included."""
    config = config or CorrelationConfig()
    initial = initial or {}
    sids = list(sids)
    edges = signature_edges(summaries, registry, sids, config)
    uses = signature_uses(summaries, registry, sids)
    windows = {day: window_s(summaries[day], config) for day in summaries}
    bounds = {}
    for day, summary in summaries.items():
        try:
            bounds[day] = (float(summary["start"]), float(summary["end"]))
        except (KeyError, TypeError, ValueError):
            continue
    entities = sorted({e for per_day in changes.values() for e in per_day})
    per_entity = {e: {day: (changes.get(day) or {}).get(e) for day in summaries} for e in entities}
    seg_cache: dict[str, dict[str, list | None]] = {}

    def segs_of(entity: str) -> dict[str, list | None]:
        if entity not in seg_cache:
            seg_cache[entity] = {
                day: None
                if ch is None or day not in bounds
                else segments((initial.get(day) or {}).get(entity), ch, *bounds[day])
                for day, ch in per_entity[entity].items()
            }
        return seg_cache[entity]

    out: dict[str, list[dict]] = {}
    for sid in sorted(set(edges) | set(uses)):
        found = []
        for entity in entities:
            cand = score_entity(edges.get(sid) or {}, per_entity[entity], windows)
            if cand is not None and accepted(cand, config):
                found.append({"entity_id": entity, "match": "edge", **cand})
                continue
            cont = score_containment(uses.get(sid) or {}, segs_of(entity), config, full=False)
            if cont is not None and accepted_containment(cont, config):
                found.append({"entity_id": entity, **cont})
        found.sort(
            key=lambda c: (
                c["match"] != "edge",
                -c["score"],
                -c.get("explained", c.get("used", 0.0)),
                -c["matches"],
                c["entity_id"],
            )
        )
        if found:
            out[sid] = found[: config.top]
    return out
