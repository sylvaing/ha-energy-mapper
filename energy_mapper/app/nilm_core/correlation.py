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

Everything here is pure (no I/O, no clock): the caller passes the day
summaries, the registry ids of the signatures and the entity changes of
each day (``day_changes``), already read from the recorder.
"""

from __future__ import annotations

import bisect
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
    top: int = 3  # candidates kept per signature


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
    prev: str | None = None
    out: list[tuple[float, str]] = []
    count = 0
    for ts, s in sorted(points, key=lambda p: p[0]):
        s = str(s)
        if s.lower() in UNAVAILABLE:
            continue
        if prev is None or s == prev:
            prev = s
            continue
        prev = s
        if start <= ts < end:
            count += 1
            if count <= max_changes:
                out.append((round(ts, 1), s))
    return (out if count <= max_changes else None), count


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
) -> dict[str, list[dict]]:
    """``{sid: [candidate, ...]}``: for each signature, the entities above
    the thresholds, the best first (at most ``config.top``). ``changes``:
    ``{day: {entity_id: changes or None}}`` (``day_changes``)."""
    config = config or CorrelationConfig()
    edges = signature_edges(summaries, registry, sids, config)
    windows = {day: window_s(summaries[day], config) for day in summaries}
    entities = sorted({e for per_day in changes.values() for e in per_day})
    out: dict[str, list[dict]] = {}
    for sid, by_day in edges.items():
        found = []
        for entity in entities:
            per_day = {day: (changes.get(day) or {}).get(entity) for day in summaries}
            cand = score_entity(by_day, per_day, windows)
            if cand is not None and accepted(cand, config):
                found.append({"entity_id": entity, **cand})
        found.sort(key=lambda c: (-c["score"], -c["explained"], -c["matches"], c["entity_id"]))
        if found:
            out[sid] = found[: config.top]
    return out
