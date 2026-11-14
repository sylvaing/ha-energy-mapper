"""Replay a recorder export through nilm_core, day by day.

The export is a CSV with one row per state: ``entity_id,state,last_updated_ts``
(extra columns are ignored), e.g. produced with sqlite3 on the recorder:

    select m.entity_id, s.state, s.last_updated_ts from states s
    join states_meta m on s.metadata_id = m.metadata_id
    where m.entity_id in (...) order by s.last_updated_ts;

Keep such exports OUTSIDE the repository: they describe a real household.

Example:
    python3 tools/replay_csv.py ~/export.csv --grid sensor.grid_power \\
        --pv sensor.pv_power --submeter sensor.pool_pump_power \\
        --switch switch.water_heater=on --switch switch.radiator=off
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "energy_mapper" / "app"))

from nilm_core import AnalysisConfig, SubmeterConfig, SwitchedLoadConfig, analyze  # noqa: E402


def load(path: str) -> dict[str, list[tuple[float, str]]]:
    hist: dict[str, list[tuple[float, str]]] = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            try:
                hist[row[0]].append((float(row[2]), row[1]))
            except ValueError:
                continue
    for v in hist.values():
        v.sort()
    return hist


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--grid", required=True)
    ap.add_argument("--pv")
    ap.add_argument("--submeter", action="append", default=[])
    ap.add_argument("--switch", action="append", default=[], help="entity=on_state")
    ap.add_argument("--threshold", type=float, default=300.0)
    ap.add_argument("--out", help="directory for the JSON reports")
    args = ap.parse_args()

    hist = load(args.csv)
    cfg = AnalysisConfig(
        grid_entity=args.grid,
        pv_entity=args.pv,
        submeters=[SubmeterConfig(e, e) for e in args.submeter],
        switched_loads=[
            SwitchedLoadConfig(e.split("=")[0], e.split("=")[0], (e.split("=")[1],) if "=" in e else ("on",))
            for e in args.switch
        ],
        step_threshold_w=args.threshold,
    )
    ts = [t for t, _ in hist[args.grid]]
    first = dt.datetime.fromtimestamp(ts[0]).date() + dt.timedelta(days=1)
    last = dt.datetime.fromtimestamp(ts[-1]).date()
    day = first
    while day < last:
        start = dt.datetime.combine(day, dt.time()).timestamp()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp()
        r = analyze(hist, cfg, start, end)
        e = r["energy_kwh"]
        known = " ".join(
            f"{k['name'].split('.')[-1]}={k['power_w']}W/{e['known'].get(k['name'], 0):.1f}kWh"
            for k in r["known_loads"]
        )
        top = r["peaks"][0] if r["peaks"] else None
        peak = ""
        if top:
            parts = "+".join(f"{p['name']}:{p['power_w']}" for p in top["parts"])
            at = dt.datetime.fromtimestamp(top["max_ts"])
            peak = f"peak {top['house_w']}W @{at:%H:%M} [{parts}] rest {top['unexplained_w']}"
        print(
            f"{day} house {e['house']:5.1f} base {e['base_w']:4}W sig {sum(e['signatures'].values()):4.1f} "
            f"other {e['other']:4.1f} | {known} | sigs {len(r['signatures'])} | {peak} "
            f"| {r['quality']['compute_s']}s"
        )
        if args.out:
            pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
            pathlib.Path(args.out, f"{day}.json").write_text(json.dumps(r))
        day += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
