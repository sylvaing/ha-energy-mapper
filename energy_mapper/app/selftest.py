"""Nightly self-test (#15): hide a measured device, find it again.

A sub-meter measures one device exactly, and the analysis subtracts it from
the whole-house curve. After the nightly analysis, the add-on analyses the
same day once more, in memory, with one such device **not** subtracted: its
power stays hidden in the curve, as if it had no meter. Its own readings are
the truth (:func:`nilm_core.groundtruth.runs_of`); the test tells whether the
curve-based recognition finds its uses again.

- **Which devices** (:func:`candidates`, :func:`pick`): the subtracted
  sub-meters that measure (not the estimates). A few per night
  (``self_test_devices``, 1 by default, 0 disables), in rotation: the device
  whose last test is the oldest first, never tested first, ties by entity id.
- **What is looked for** (:func:`hide_and_find`): the signature learned
  (:func:`nilm_core.groundtruth.learn`) from the device's runs of the day and
  of its previous tests, resolved in the registry (:func:`teaching.resolve`)
  or, when the registry has none (it never sees a subtracted device), the
  expected signature it would be, added to a copy of the registry. The
  hidden day is evaluated with :func:`teaching.evaluate_days` (precision,
  recall, energy). A signature below the step threshold cannot be seen in the
  curve: no hidden analysis is run for it.
- **What is kept** (:func:`record`): one entry per test in
  ``<data>/self_test.json``, over a rolling window of ``WINDOW_DAYS``.
- **What is shown** (:func:`summary`): per device, the merged evaluation of
  its tests, the share of the true uses found again (its recall) once it has
  at least ``MIN_USES`` of them, and the trend (newer half of the tests
  against the older half).

The hidden analysis never touches the real report, the registry or the
statistics: the caller passes a copy of the configuration and never stores
the hidden report. Pure (no I/O, no clock).
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence

from nilm_core.groundtruth import GroundTruthConfig, Run, learn, runs_of
from nilm_core.registry import RegistryConfig
from teaching import evaluate_days, new_signature, resolve

VERSION = 1
DEFAULT_DEVICES = 1  # devices tested per night
MAX_DEVICES = 10
WINDOW_DAYS = 60  # tests kept and summarised
MIN_USES = 5  # true uses before a success rate is shown
TREND_MIN_USES = 3  # true uses in each half before a trend is shown
TREND_DELTA = 0.1  # recall difference between the halves that is a trend
MAX_POOL_RUNS = 200  # runs of the previous tests the signature is learned from

# status of a test
TESTED = "tested"  # the hidden day was analysed and evaluated
NO_USE = "no_use"  # no run of the device yet: nothing to look for
NOT_RECOGNISABLE = "not_recognisable"  # below the step threshold: the curve cannot show it


def empty() -> dict:
    return {"version": VERSION, "tests": []}


def candidates(submeters: Iterable) -> list[tuple[str, str]]:
    """``(entity_id, name)`` of the devices that can be hidden: the subtracted
    sub-meters that measure (an estimate is not a truth), sorted by entity."""
    out: dict[str, str] = {}
    for m in submeters:
        if getattr(m, "estimated", False) or not m.entity_id:
            continue
        out.setdefault(m.entity_id, m.name or m.entity_id)
    return sorted(out.items())


def pick(entities: Sequence[str], data: Mapping | None, count: int) -> list[str]:
    """The ``count`` devices to test tonight: those whose last test is the
    oldest, never tested first; ties by entity id."""
    if count <= 0:
        return []
    last: dict[str, str] = {}
    for t in (data or {}).get("tests") or []:
        e, day = t.get("entity_id"), str(t.get("day") or "")
        if e and day > last.get(e, ""):
            last[e] = day
    return sorted(set(entities), key=lambda e: (last.get(e, ""), e))[:count]


def tested_days(data: Mapping | None, day: str) -> set[str]:
    """The devices already tested on ``day``."""
    return {t["entity_id"] for t in (data or {}).get("tests") or [] if t.get("day") == day}


def hidden_config(config, entity_id: str):
    """A copy of the analysis configuration that does not subtract
    ``entity_id`` (the real one is left untouched)."""
    cfg = copy.deepcopy(config)
    cfg.submeters = [m for m in cfg.submeters if m.entity_id != entity_id]
    return cfg


def previous_runs(data: Mapping | None, entity_id: str) -> list[Run]:
    """The runs of the device's stored tests, newest last."""
    out: list[Run] = []
    for t in (data or {}).get("tests") or []:
        if t.get("entity_id") != entity_id:
            continue
        for r in t.get("runs") or []:
            try:
                out.append(Run.from_dict(r))
            except (KeyError, TypeError, ValueError):
                continue
    return out[-MAX_POOL_RUNS:]


def hide_and_find(
    day: str,
    entity_id: str,
    name: str,
    points: Sequence[tuple[float, float | None]],
    start: float,
    end: float,
    hidden: Callable[[], Mapping],
    registry: Mapping | None,
    previous: Sequence[Run],
    now: float,
    appliance: str | None = None,
    step_threshold_w: float = 300.0,
    config: RegistryConfig | None = None,
    gt_config: GroundTruthConfig | None = None,
    visible_w: float | None = None,
) -> dict:
    """One test: the device's runs on the day (``points``, its readings in
    W), the signature learned from them and from ``previous`` runs, and the
    evaluation of the hidden day. ``hidden()`` analyses the day with the
    device not subtracted; it is only called when there is something the
    curve can show. ``registry`` is not modified."""
    gt_config = gt_config or GroundTruthConfig()
    runs, covered = runs_of(points, start, end, gt_config)
    learned = learn([*previous, *runs], step_threshold_w, gt_config, visible_w)
    test: dict = {
        "day": day,
        "entity_id": entity_id,
        "name": name,
        "appliance": appliance,
        "at": now,
        "runs": [r.to_dict() for r in runs],
        "covered_s": round(sum(b - a for a, b in covered)),
        "learned": learned.to_dict() if learned else None,
        "signature": None,
        "evaluation": None,
    }
    if learned is None:
        test["status"] = NO_USE
        return test
    if not learned.recognisable:
        test["status"] = NOT_RECOGNISABLE
        return test
    sig = resolve(registry, learned, config)
    reg: Mapping | None = registry
    known = sig is not None
    if sig is None:
        # a subtracted device never reaches the registry: evaluated as the
        # expected signature it would be, in a copy of the registry
        sig = new_signature(registry, learned, {"key": appliance or entity_id, "name": name}, entity_id, now)
        reg = copy.deepcopy(dict(registry or {}))
        reg.setdefault("signatures", []).append(sig)
    truth = {
        "runs": test["runs"],
        "covered": [[round(a), round(b)] for a, b in covered],
    }
    _, per_day = evaluate_days({day: truth}, {day: hidden()}, reg, sig, learned, gt_config)
    evaluation = dict(per_day[0])
    evaluation.pop("day", None)
    test["signature"] = {
        "id": sig["id"],
        "kind": sig.get("kind"),
        "band": sig.get("band"),
        "power_w": sig.get("power_w"),
        "label": sig.get("label") if known else None,
        "in_registry": known,
    }
    test["evaluation"] = evaluation
    test["status"] = TESTED
    return test


def record(
    data: Mapping | None, tests: Iterable[Mapping], today: dt.date, window_days: int = WINDOW_DAYS
) -> dict:
    """The stored tests with ``tests`` added (a test of the same device and
    day replaces the old one) and those older than the window removed."""
    first = (today - dt.timedelta(days=window_days)).isoformat()
    new = list(tests)
    replaced = {(t["entity_id"], t["day"]) for t in new}
    kept = [
        t
        for t in (data or {}).get("tests") or []
        if str(t.get("day") or "") >= first and (t.get("entity_id"), t.get("day")) not in replaced
    ]
    out = [*kept, *(dict(t) for t in new if str(t["day"]) >= first)]
    out.sort(key=lambda t: (str(t["day"]), str(t["entity_id"])))
    return {"version": VERSION, "tests": out}


def _rate(found: int, uses: int) -> float | None:
    return round(found / uses, 3) if uses else None


def _merge(tests: Sequence[Mapping]) -> dict:
    keys = ("uses", "found", "detections", "correct")
    total = {k: sum(int((t.get("evaluation") or {}).get(k) or 0) for t in tests) for k in keys}
    truth = sum(float((t.get("evaluation") or {}).get("truth_kwh") or 0.0) for t in tests)
    detected = sum(float((t.get("evaluation") or {}).get("detected_kwh") or 0.0) for t in tests)
    return {
        **total,
        "recall": _rate(total["found"], total["uses"]),
        "precision": _rate(total["correct"], total["detections"]),
        "truth_kwh": round(truth, 3),
        "detected_kwh": round(detected, 3),
        "energy_ratio": round(detected / truth, 3) if truth > 0 else None,
    }


def trend(tests: Sequence[Mapping], min_uses: int = TREND_MIN_USES, delta: float = TREND_DELTA) -> str | None:
    """``up``, ``down`` or ``stable``: the recall of the newer half of the
    tests against the older half (``tests`` oldest first); None while either
    half has fewer than ``min_uses`` true uses."""
    if len(tests) < 2:
        return None
    half = len(tests) // 2
    older, newer = _merge(tests[:half]), _merge(tests[half:])
    if older["uses"] < min_uses or newer["uses"] < min_uses:
        return None
    diff = newer["recall"] - older["recall"]
    if diff >= delta:
        return "up"
    if diff <= -delta:
        return "down"
    return "stable"


def device_summary(tests: Sequence[Mapping], min_uses: int = MIN_USES) -> dict:
    """The self-test of one device from its tests (oldest first).

    ``state``: ``ok`` (a success rate: ``rate``), ``not_enough_data`` (fewer
    than ``min_uses`` true uses: no rate, never 0 %), ``not_recognisable``
    (too small for the curve) or ``no_use`` (never ran yet)."""
    last = tests[-1]
    done = [t for t in tests if t.get("status") == TESTED]
    merged = _merge(done)
    enough = merged["uses"] >= min_uses
    if done:
        state = "ok" if enough else "not_enough_data"
    else:
        state = NOT_RECOGNISABLE if any(t.get("status") == NOT_RECOGNISABLE for t in tests) else NO_USE
    signature = next((t["signature"] for t in reversed(done) if t.get("signature")), None)
    learned = last.get("learned")
    return {
        "entity_id": last["entity_id"],
        "name": last.get("name") or last["entity_id"],
        "appliance": last.get("appliance"),
        "state": state,
        "tests": len(done),
        "nights": len(tests),
        "first_day": tests[0]["day"],
        "last_day": last["day"],
        **merged,
        "rate": merged["recall"] if enough else None,
        "trend": trend(done) if enough else None,
        "learned": learned,
        "signature": signature,
        "history": [
            {
                "day": t["day"],
                "uses": int((t.get("evaluation") or {}).get("uses") or 0),
                "found": int((t.get("evaluation") or {}).get("found") or 0),
                "detections": int((t.get("evaluation") or {}).get("detections") or 0),
                "correct": int((t.get("evaluation") or {}).get("correct") or 0),
            }
            for t in done
        ],
    }


def summary(
    data: Mapping | None,
    today: dt.date,
    devices_per_night: int = DEFAULT_DEVICES,
    window_days: int = WINDOW_DAYS,
    min_uses: int = MIN_USES,
) -> dict:
    """``GET /api/self_test``: the settings and one summary per device tested
    in the window, by name."""
    first = (today - dt.timedelta(days=window_days)).isoformat()
    by_device: dict[str, list[Mapping]] = {}
    for t in sorted((data or {}).get("tests") or [], key=lambda t: str(t.get("day") or "")):
        if str(t.get("day") or "") >= first and t.get("entity_id"):
            by_device.setdefault(t["entity_id"], []).append(t)
    devices = [device_summary(tests, min_uses) for tests in by_device.values()]
    devices.sort(key=lambda d: (str(d["name"]).casefold(), d["entity_id"]))
    days = [d["last_day"] for d in devices]
    return {
        "enabled": devices_per_night > 0,
        "devices_per_night": devices_per_night,
        "window_days": window_days,
        "min_uses": min_uses,
        "last_day": max(days) if days else None,
        "devices": devices,
    }
