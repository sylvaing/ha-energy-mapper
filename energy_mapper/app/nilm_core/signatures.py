"""Group activations that look alike into signatures (candidate appliances)."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from .activations import Activation

# duration bands, in seconds: short uses (kettle) vs cycles (washing machine heating)
DURATION_BANDS = ((0, 600, "short"), (600, 3600, "medium"), (3600, math.inf, "long"))


def duration_band(seconds: float) -> str:
    for lo, hi, name in DURATION_BANDS:
        if lo <= seconds < hi:
            return name
    return "long"


@dataclass
class Signature:
    id: str
    kind: str  # "cycling" | "steady"
    band: str
    power_w: float
    count: int
    duration_s: float  # median
    energy_kwh: float
    hours: list[int] = field(default_factory=lambda: [0] * 24)
    on_s: float | None = None
    period_s: float | None = None
    duty: float | None = None
    examples: list[float] = field(default_factory=list)  # activation start times

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "band": self.band,
            "power_w": round(self.power_w),
            "count": self.count,
            "duration_s": round(self.duration_s),
            "energy_kwh": round(self.energy_kwh, 3),
            "hours": self.hours,
            "on_s": self.on_s,
            "period_s": self.period_s,
            "duty": self.duty,
            "examples": self.examples,
        }


def signature_id(kind: str, band: str, power_w: float) -> str:
    """Stable id across days: power rounded to 100 W (50 W under 1 kW)."""
    step = 50 if power_w < 1000 else 100
    rounded = int(round(power_w / step) * step)
    prefix = "cyc" if kind == "cycling" else band
    return f"{prefix}-{rounded}w"


def cluster(activations: list[Activation], power_tol: float = 0.12, local_hour=None) -> list[Signature]:
    """Greedy one-dimensional clustering on power, per kind and duration band."""
    local_hour = local_hour or (lambda ts: time.localtime(ts).tm_hour)
    groups: dict[tuple[str, str], list[Activation]] = {}
    for a in activations:
        kind = "cycling" if a.cycling else "steady"
        band = "session" if a.cycling else duration_band(a.duration_s)
        groups.setdefault((kind, band), []).append(a)

    out: list[Signature] = []
    used_ids: dict[str, int] = {}
    for (kind, band), acts in sorted(groups.items()):
        acts.sort(key=lambda a: a.power_w)
        clusters: list[list[Activation]] = []
        for a in acts:
            if clusters:
                ref = statistics.median(x.power_w for x in clusters[-1])
                if abs(a.power_w - ref) <= power_tol * ref:
                    clusters[-1].append(a)
                    continue
            clusters.append([a])
        for c in clusters:
            power = statistics.median(x.power_w for x in c)
            sid = signature_id(kind, band, power)
            n = used_ids.get(sid, 0)
            used_ids[sid] = n + 1
            if n:  # two clusters rounded to the same id
                sid = f"{sid}-{n + 1}"
            sig = Signature(
                id=sid,
                kind=kind,
                band=band,
                power_w=power,
                count=len(c),
                duration_s=statistics.median(x.duration_s for x in c),
                energy_kwh=sum(x.energy_kwh for x in c),
            )
            for x in c:
                sig.hours[local_hour(x.start)] += 1
                x.signature = sid
            if kind == "cycling":
                ons = [x.on_s for x in c if x.on_s]
                pers = [x.period_s for x in c if x.period_s]
                sig.on_s = round(statistics.median(ons), 1) if ons else None
                sig.period_s = round(statistics.median(pers), 1) if pers else None
                sig.duty = round(statistics.median(x.duty for x in c), 2)
            sig.examples = [x.start for x in sorted(c, key=lambda x: -x.energy_kwh)[:3]]
            out.append(sig)
    out.sort(key=lambda s: -s.energy_kwh)
    return out
