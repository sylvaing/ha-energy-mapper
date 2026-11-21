"""Hourly energy per appliance, from the per-minute curve of a daily report.

Used to publish delayed (nightly) values with the right hour in Home
Assistant's long-term statistics. Hours are UTC hours (unix times multiple
of 3600), as the recorder stores them: a day has 24 of them, 23 or 25 on DST
days, and in time zones with a half-hour offset its first and last hours are
shared with the neighbouring days (each day only counts its own minutes).

Appliances are keyed by a stable identity:

- switched loads by their ``entity_id`` (the curve keys them by name, mapped
  with the report's ``known_loads``);
- signatures by their **registry id**: the curve keys them by the day's own
  cluster id, so each minute's power of a daily signature is shared among the
  activations of that signature running in that minute, in proportion of
  their energy in the minute, and each share goes to the registry id of its
  activation (:class:`registry.Matcher`, the rule of :func:`annotate_report`).
  Activations that match no registry signature go under ``"?"``.

Everything here is pure: the caller passes the report and the registry.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from .registry import Matcher

HOUR_S = 3600
UNMATCHED = "?"
# kWh are rounded to 0.1 Wh: the sum over a day stays within a few Wh of the
# report's own totals (themselves rounded to 1 Wh)
DIGITS = 4


def curve_resolution(curve: list[Mapping]) -> float:
    """Seconds per curve row: the smallest gap between two rows (60 by default)."""
    gaps = [b["t"] - a["t"] for a, b in zip(curve, curve[1:], strict=False) if b["t"] > a["t"]]
    return float(min(gaps)) if gaps else 60.0


def _hour(ts: float) -> int:
    return int(math.floor(ts / HOUR_S) * HOUR_S)


def _add(target: dict, key: str, value: float) -> None:
    target[key] = target.get(key, 0.0) + value


def hourly_breakdown(report: Mapping, registry: Mapping | None) -> dict:
    """``{"day", "start", "end", "time_zone", "generated_at", "resolution_s",
    "hours", "energy_kwh", "last_active", "uses", "names"}``.

    ``hours`` lists every UTC hour overlapping ``[start, end)``, in order:
    ``{"start", "house", "base", "other", "known": {entity_id: kWh},
    "signatures": {registry id: kWh}}`` (zeros left out of the dicts).
    ``last_active`` gives the end of the last curve row in which each
    appliance used energy, ``uses`` the number of activations of each
    signature (a cycling session counts once) and of on-periods of each
    switched load, ``names`` the name of each switched load."""
    curve = report.get("curve") or []
    start = float(report["start"])
    end = float(report["end"])
    res = curve_resolution(curve)
    kwh = res / 3600 / 1000  # W during one row -> kWh

    names: dict[str, str] = {}
    for k in report.get("known_loads") or []:
        if k.get("name") and k.get("entity_id"):
            names.setdefault(k["name"], k["entity_id"])

    matcher = Matcher(registry)
    # daily signature id -> [(start, end, mean power, registry id)]
    acts: dict[str, list[tuple[float, float, float, str]]] = {}
    uses_sig: dict[str, int] = {}
    for a in report.get("activations") or []:
        try:
            a0, a1 = float(a["start"]), float(a["end"])
            power = float(a["power_w"]) * float(a.get("duty") or 1.0)
        except (KeyError, TypeError, ValueError):
            continue
        rid = matcher.match(a) or UNMATCHED
        acts.setdefault(a.get("signature") or UNMATCHED, []).append((a0, a1, power, rid))
        if a1 > start and a0 < end:
            uses_sig[rid] = uses_sig.get(rid, 0) + 1
    # fallback when a row's signature has no running activation (should not happen)
    daily_rid: dict[str, str] = {}
    for key, items in acts.items():
        votes: dict[str, float] = {}
        for _, _, _, rid in items:
            votes[rid] = votes.get(rid, 0) + 1
        daily_rid[key] = max(votes, key=lambda r: (votes[r], r))

    hours: dict[int, dict] = {}
    h = _hour(start)
    while h < end:
        hours[h] = {"start": h, "house": 0.0, "base": 0.0, "other": 0.0, "known": {}, "signatures": {}}
        h += HOUR_S

    last_active: dict[str, dict[str, float]] = {"known": {}, "signatures": {}}
    uses_known: dict[str, int] = {}
    was_on: set[str] = set()
    for row in curve:
        t = float(row["t"])
        if not start <= t < end:
            continue
        bucket = hours.get(_hour(t))
        if bucket is None:
            continue
        t1 = t + res
        for key in ("house", "base", "other"):
            bucket[key] += max(float(row.get(key) or 0.0), 0.0) * kwh
        on_now = set()
        for name, watts in (row.get("known") or {}).items():
            if not watts or watts <= 0:
                continue
            eid = names.get(name, name)
            _add(bucket["known"], eid, watts * kwh)
            last_active["known"][eid] = t1
            on_now.add(eid)
            if eid not in was_on:
                uses_known[eid] = uses_known.get(eid, 0) + 1
        was_on = on_now
        for key, watts in (row.get("signatures") or {}).items():
            if not watts or watts <= 0:
                continue
            shares: dict[str, float] = {}
            for a0, a1, power, rid in acts.get(key, ()):
                overlap = min(a1, t1) - max(a0, t)
                if overlap > 0:
                    _add(shares, rid, power * overlap)
            total = sum(shares.values())
            if total <= 0:
                shares, total = {daily_rid.get(key, UNMATCHED): 1.0}, 1.0
            for rid, weight in shares.items():
                _add(bucket["signatures"], rid, watts * kwh * weight / total)
                last_active["signatures"][rid] = t1

    out_hours = []
    for h in sorted(hours):
        b = hours[h]
        out_hours.append(
            {
                "start": h,
                "house": round(b["house"], DIGITS),
                "base": round(b["base"], DIGITS),
                "other": round(b["other"], DIGITS),
                "known": {k: round(v, DIGITS) for k, v in sorted(b["known"].items()) if v > 0},
                "signatures": {k: round(v, DIGITS) for k, v in sorted(b["signatures"].items()) if v > 0},
            }
        )

    def total(kind: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for b in out_hours:
            for k, v in b[kind].items():
                _add(out, k, v)
        return {k: round(v, DIGITS) for k, v in sorted(out.items())}

    return {
        "day": report.get("day"),
        "start": start,
        "end": end,
        "time_zone": report.get("time_zone"),
        "generated_at": report.get("generated_at"),
        "resolution_s": res,
        "hours": out_hours,
        "energy_kwh": {
            "house": round(sum(b["house"] for b in out_hours), DIGITS),
            "base": round(sum(b["base"] for b in out_hours), DIGITS),
            "other": round(sum(b["other"] for b in out_hours), DIGITS),
            "base_w": (report.get("energy_kwh") or {}).get("base_w"),
            "known": total("known"),
            "signatures": total("signatures"),
        },
        "last_active": last_active,
        "uses": {"known": dict(sorted(uses_known.items())), "signatures": dict(sorted(uses_sig.items()))},
        "names": {eid: name for name, eid in names.items()},
    }
