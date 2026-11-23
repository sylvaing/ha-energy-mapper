"""Labelled examples (#17): the uses the user pointed at in the live view.

An example says "this appliance ran from ``t_on`` to ``t_off`` at about
``power_w``" (``appliances.py``; ``t_off`` is null while the stop is not
known). It is a tiny ground truth, of the same nature as the runs of a
teaching sensor (#14, ``teaching.py``), and it is used the same way, from the
analysed days (the day summaries of the nightly analysis):

1. **Find** (:func:`find_activation`): the activation of the day that
   overlaps the example by at least half of the shorter of the two (the rule
   of :func:`nilm_core.groundtruth.evaluate`); for an open example, the
   activation that starts within ``OPEN_WINDOW_S`` of ``t_on``. Its registry
   signature is the one the analysis recognises (``Matcher.match``).
2. **Link** (:func:`status`): when the appliance has no signature of its own
   seen by the analysis yet (none, or only expected ones), the signature most
   examples were found as is proposed as its signature (``link``), replacing
   its expected ones (``replace``), unless it belongs to another appliance
   (``conflict``). The integration makes it a source of the appliance and
   records the review decision; the add-on then gives the signature the
   appliance's label (``apply_to_registry``): the registry is refined.
3. **Measure**: each example is ``found`` (its activation is recognised as
   one of the appliance's signatures), ``other`` (as another signature:
   contradicting examples are listed in ``disagreement``) or ``pending``,
   with a reason. ``found`` of ``total`` is the recognition the panel shows
   on the appliance's card.

An example with no activation and a power under the step threshold is not
tracked. Whether the curve can show it is said from the measured noise of
the curve (``visible_w``, #88, #100), as for teaching and the self-test: at
or above it, ``small_untracked`` (seen by the curve, too small to be tracked
yet); under it, ``too_small``. A small use of the day (``small_uses``, the
low steps of the adaptive threshold) that overlaps the example by the rule
of the activations is the curve showing it: ``small_untracked`` whatever the
noise, with the small use in ``small_use``.

Pure (no I/O, no clock): the caller passes the summaries and the registry.
"""

from __future__ import annotations

from collections.abc import Mapping

from nilm_core.groundtruth import Interval, matches, overlap_s
from nilm_core.registry import Matcher
from teaching import owner_of

# an open example (no stop yet) is the activation that starts this close to it
OPEN_WINDOW_S = 30.0

FOUND, OTHER, PENDING = "found", "other", "pending"
# why an example is pending
DAY_NOT_ANALYSED = "day_not_analysed"  # its day is not analysed yet (today)
DAY_MISSING = "day_missing"  # its day has no stored summary (failed or pruned)
NO_ACTIVATION = "no_activation"  # the analysis saw no activation there
# below the step threshold, so not tracked, and ...
TOO_SMALL = "too_small"  # ... under the noise of the curve: it cannot show it
SMALL_UNTRACKED = "small_untracked"  # ... at or above it: seen by the curve, not tracked yet
NOT_IN_REGISTRY = "not_in_registry"  # an activation the registry does not know (yet)
MEASURED = "measured"  # the appliance is measured by a switch or a power sensor


def _day_of(summaries: Mapping[str, Mapping], ts: float) -> str | None:
    for day, s in summaries.items():
        try:
            if float(s["start"]) <= ts < float(s["end"]):
                return day
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _last_end(summaries: Mapping[str, Mapping]) -> float | None:
    ends = []
    for s in summaries.values():
        try:
            ends.append(float(s["end"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(ends) if ends else None


def find_activation(
    example: Mapping, summary: Mapping, min_overlap: float = 0.5, field: str = "activations"
) -> Mapping | None:
    """The activation of a day summary that is this example (None: none).
    ``field``: the list searched (``small_uses``: the day's small uses)."""
    t_on = float(example["t_on"])
    t_off = example.get("t_off")
    best, best_key = None, None
    for a in summary.get(field) or []:
        try:
            start, end = float(a["start"]), float(a["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if t_off is None:
            gap = abs(start - t_on)
            if gap > OPEN_WINDOW_S:
                continue
            key = -gap
        else:
            ex, act = Interval(t_on, float(t_off)), Interval(start, end)
            if not matches(ex, act, min_overlap):
                continue
            key = overlap_s(ex, act)
        if best_key is None or key > best_key:
            best, best_key = a, key
    return best


def _item(example: Mapping, reason: str | None = None) -> dict:
    return {
        "t_on": example["t_on"],
        "t_off": example.get("t_off"),
        "power_w": example.get("power_w"),
        "source": example.get("source"),
        "status": PENDING,
        "reason": reason,
        "signature_id": None,
        "activation": None,
        "small_use": None,
    }


def _found(day: str, a: Mapping) -> dict:
    return {
        "day": day,
        "start": round(float(a["start"])),
        "end": round(float(a["end"])),
        "power_w": round(float(a.get("power_w") or 0.0)),
    }


def resolve(
    example: Mapping,
    summaries: Mapping[str, Mapping],
    matcher: Matcher,
    step_threshold_w: float = 300.0,
    min_overlap: float = 0.5,
    visible_w: float | None = None,
) -> dict:
    """One example: the activation it is and its registry signature, or why
    it is pending (``status`` is set by :func:`status`). ``visible_w``: the
    smallest step the whole-house curve can show
    (:func:`nilm_core.quality.visible_w`); None (not measured yet): an
    example under the step threshold is ``too_small``, the former verdict."""
    out = _item(example)
    t_on = float(example["t_on"])
    day = _day_of(summaries, t_on)
    if day is None:
        last = _last_end(summaries)
        out["reason"] = DAY_NOT_ANALYSED if last is None or t_on >= last else DAY_MISSING
        return out
    a = find_activation(example, summaries[day], min_overlap)
    if a is None:
        power = float(example.get("power_w") or 0.0)
        if power >= step_threshold_w:
            out["reason"] = NO_ACTIVATION
            return out
        # not tracked; the curve shows it when one of the day's small uses
        # is this example, or when its power reaches the measured noise
        small = find_activation(example, summaries[day], min_overlap, "small_uses")
        if small is not None:
            out["small_use"] = _found(day, small)
        seen = small is not None or (visible_w is not None and power >= visible_w)
        out["reason"] = SMALL_UNTRACKED if seen else TOO_SMALL
        return out
    out["activation"] = _found(day, a)
    sid = matcher.match(a)
    if sid is None:
        out["reason"] = NOT_IN_REGISTRY
        return out
    out["signature_id"] = sid
    return out


def _summary(sig: Mapping | None) -> dict | None:
    if not sig:
        return None
    return {
        "id": sig["id"],
        "kind": sig.get("kind"),
        "band": sig.get("band"),
        "power_w": sig.get("power_w"),
        "count": int(sig.get("count") or 0),
        "label": sig.get("label"),
    }


def _seen(sig: Mapping | None) -> bool:
    """A signature the analysis has seen (not only expected)."""
    return bool(sig) and not sig.get("expected") and (sig.get("count") or 0) > 0


def status(
    appliance: Mapping,
    summaries: Mapping[str, Mapping],
    registry: Mapping | None,
    step_threshold_w: float = 300.0,
    matcher: Matcher | None = None,
    min_overlap: float = 0.5,
    visible_w: float | None = None,
) -> dict:
    """What the analysed days say about the examples of one appliance
    (``visible_w``: see :func:`resolve`)."""
    key = appliance["key"]
    matcher = matcher or Matcher(registry)
    sigs = {s.get("id"): s for s in (registry or {}).get("signatures") or []}
    sources = appliance.get("sources") or []
    own = [s["id"] for s in sources if s.get("type") == "signature"]
    measured = any(s.get("type") in ("switched_load", "submeter") for s in sources)
    items = []
    for e in appliance.get("examples") or []:
        if measured:
            items.append(_item(e, MEASURED))
        else:
            items.append(resolve(e, summaries, matcher, step_threshold_w, min_overlap, visible_w))
    # votes: how many examples each signature was found as (ties: the latest)
    votes: dict[str, tuple[int, float]] = {}
    for it in items:
        sid = it["signature_id"]
        if sid:
            n, last = votes.get(sid, (0, 0.0))
            votes[sid] = (n + 1, max(last, float(it["t_on"])))
    mine = set(own)
    link = conflict = None
    replace: list[str] = []
    if not measured and votes and not any(_seen(sigs.get(sid)) for sid in own):
        best = max(votes, key=lambda sid: votes[sid])
        if best not in mine:
            owner = owner_of(sigs.get(best), key)
            if owner:
                conflict = {"id": best, "appliance": owner}
            else:
                link = _summary(sigs.get(best))
                replace = [sid for sid in own if sid != best]
                mine = {best}
    for it in items:
        sid = it["signature_id"]
        if sid is None:
            continue
        if sid in mine:
            it["status"] = FOUND
        else:
            it["status"] = OTHER
            it["owner"] = owner_of(sigs.get(sid), key)
    found_votes = {sid: v for sid, v in votes.items() if sid in mine}
    main = max(found_votes, key=lambda sid: found_votes[sid]) if found_votes else None
    if main is None:
        main = next((sid for sid in own if sid in sigs), None)
    return {
        "key": key,
        "name": appliance.get("name"),
        "measured": measured,
        "total": len(items),
        "found": sum(1 for it in items if it["status"] == FOUND),
        "other": sum(1 for it in items if it["status"] == OTHER),
        "pending": sum(1 for it in items if it["status"] == PENDING),
        "signature": _summary(sigs.get(main)) if main else None,
        "link": link,
        "replace": replace,
        "conflict": conflict,
        "disagreement": sorted({it["signature_id"] for it in items if it["status"] == OTHER}),
        "examples": items,
    }
