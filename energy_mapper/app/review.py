"""The review of the signatures (#10): which ones the user is asked about,
and the decisions taken in the Energy Mapper panel.

A signature of the registry is in one of these review states:

- ``validated``: it belongs to one of the user's appliances: named (the
  integration declared it as a signature source, so it has a ``label``), or
  validated by linking it to a switch or a power sensor (its decision names
  the appliance). When that appliance no longer exists (deleted in Home
  Assistant), the signature is ``ignored``: deleting an appliance means "not
  interested", and the "Ignored" list of the panel can bring it back;
- ``ignored``: never proposed again, until the user restores it. Kept by the
  registry after it fades out (bounded, ``registry.MAX_FADED_IGNORED``) so
  that a later cluster at its power keeps the decision;
- ``waiting``: the user asked to wait for more data; it comes back when its
  number of uses reached ``wait_factor`` times the number at the decision, or
  ``wait_days`` days later;
- ``proposed``: mature (at least ``min_uses`` uses on ``min_days`` different
  days) and not decided: shown in the "To review" section of the panel;
- ``immature``: not seen enough yet.

The decision is stored in the registry signature as ``review``:
``{"state": "waiting", "at", "count", "until_count", "until"}``,
``{"state": "ignored", "at"}`` or ``{"state": "validated", "at",
"appliance"}``. The registry carries it from night to night with the
signature's id (``registry.update``). Everything here is pure: the caller
passes ``now`` and the keys of the declared appliances.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

DAY_S = 86400.0
ACTIONS = ("validate", "wait", "ignore", "restore")
# states listed by GET /api/review
LISTED = ("proposed", "waiting", "ignored")


@dataclass(frozen=True)
class Criteria:
    min_uses: int = 5
    min_days: int = 3
    wait_days: int = 14
    wait_factor: float = 2.0

    def as_dict(self) -> dict:
        return {
            "min_uses": self.min_uses,
            "min_days": self.min_days,
            "wait_days": self.wait_days,
            "wait_factor": self.wait_factor,
        }


class ReviewError(ValueError):
    """An invalid decision; the message says why."""


def is_mature(sig: Mapping, criteria: Criteria) -> bool:
    return (sig.get("count") or 0) >= criteria.min_uses and (sig.get("days_seen") or 0) >= criteria.min_days


def state(sig: Mapping, now: float, criteria: Criteria, appliances: Iterable[str]) -> str:
    """The review state of a registry signature (see the module docstring);
    ``appliances``: the keys of the appliances declared by the integration."""
    if sig.get("label") or sig.get("appliance"):
        return "validated"
    review = sig.get("review") or {}
    decided = review.get("state")
    if decided == "validated":
        return "validated" if review.get("appliance") in set(appliances) else "ignored"
    if decided == "ignored":
        return "ignored"
    if decided == "waiting":
        more = (sig.get("count") or 0) >= (review.get("until_count") or 0)
        late = now >= float(review.get("until") or 0)
        if not (more or late):
            return "waiting"
    return "proposed" if is_mature(sig, criteria) else "immature"


def decide(sig: dict, action: str, now: float, criteria: Criteria, appliance: str | None = None) -> None:
    """Record a decision in the signature (modified in place)."""
    if action == "restore":
        sig.pop("review", None)
    elif action == "ignore":
        sig["review"] = {"state": "ignored", "at": now}
    elif action == "wait":
        count = int(sig.get("count") or 0)
        sig["review"] = {
            "state": "waiting",
            "at": now,
            "count": count,
            "until_count": max(count + 1, math.ceil(count * criteria.wait_factor)),
            "until": now + criteria.wait_days * DAY_S,
        }
    elif action == "validate":
        if not appliance:
            raise ReviewError("validate: the appliance key is missing")
        sig["review"] = {"state": "validated", "at": now, "appliance": appliance}
    else:
        raise ReviewError(f"action: expected one of {', '.join(ACTIONS)}")


def annotated(sig: Mapping, now: float, criteria: Criteria, appliances: Iterable[str]) -> dict:
    """The signature with its ``review_state``."""
    return {**sig, "review_state": state(sig, now, criteria, appliances)}


def listing(registry: Mapping | None, now: float, criteria: Criteria, appliances: Iterable[str]) -> dict:
    """``GET /api/review``: the signatures to review, waiting and ignored
    (proposed first, the most energy first)."""
    keys = set(appliances)
    sigs = [annotated(s, now, criteria, keys) for s in (registry or {}).get("signatures") or []]
    listed = [s for s in sigs if s["review_state"] in LISTED]
    order = {name: i for i, name in enumerate(LISTED)}
    listed.sort(
        key=lambda s: (
            order[s["review_state"]],
            -(s.get("weighted_energy_kwh") or 0.0),
            -(s.get("weight") or 0),
        )
    )
    counts = {name: sum(1 for s in sigs if s["review_state"] == name) for name in (*LISTED, "validated")}
    return {"criteria": criteria.as_dict(), "now": now, "counts": counts, "signatures": listed}
