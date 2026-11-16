"""Generate the SYNTHETIC sample data of the panel's dev harness.

A made-up household (no real data): a few appliances at fixed hours, a
measured heat pump, a water heater on a switch and some solar, simulated with
the helpers of ``tests/synth.py`` and analysed with ``nilm_core`` exactly as
the add-on does. The answers of the websocket commands are written to
``frontend/dev/sample/``:

    python3 frontend/dev/make_sample.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import random
import sys
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "energy_mapper" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

from nilm_core import AnalysisConfig, SubmeterConfig, SwitchedLoadConfig, analyze  # noqa: E402
from nilm_core.registry import RegistryConfig, annotate_report  # noqa: E402
from nilm_core.registry import update as update_registry  # noqa: E402
from synth import Cycling, Steady, switch_history  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / "sample"
TZ = zoneinfo.ZoneInfo("Europe/Berlin")
FIRST_DAY = dt.date(2027, 3, 3)
N_DAYS = 7  # the last FULL_DAYS keep their curve, the others are summaries
FULL_DAYS = 3
DT = 10.0

GRID = "sensor.grid_power"
PV = "sensor.solar_power"
HEAT_PUMP = "sensor.heat_pump_power"
WATER_HEATER = "switch.water_heater"


def at(day_start: float, hh: float) -> float:
    return day_start + hh * 3600


def simulate(day: dt.date, seed: int) -> tuple[dict, float, float]:
    rnd = random.Random(seed)
    start = dt.datetime.combine(day, dt.time(0), tzinfo=TZ).timestamp()
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=TZ).timestamp()
    t0 = start - 3 * 3600  # context read before the day

    def j(hh: float, spread: float = 0.3) -> float:
        return at(start, hh + rnd.uniform(-spread, spread))

    loads = [
        Steady(j(7.2, 0.1), j(7.2, 0.1) + 180, 2000),  # kettle
        Steady(j(16.5), j(16.5) + 170, 2000),  # kettle
        Cycling(j(6.5, 0.2), j(7.5, 0.2), 1500, 60, 90),  # hob
        Steady(j(19.0), j(19.0) + 40 * 60, 2600),  # oven
        Steady(j(13.0, 0.5), j(13.0, 0.5) + 22 * 60, 2100),  # dishwasher heating
    ]
    if day.weekday() in (5, 6):
        loads.append(Steady(j(10.5), j(10.5) + 95 * 60, 900))  # washing machine
    heater = (j(1.5, 0.1), j(4.0, 0.2))
    loads.append(Steady(heater[0], heater[1], 2400))
    hp = [Steady(j(6.0), j(9.0), 850), Steady(j(18.0), j(22.0), 900)]
    cloud = rnd.uniform(0.4, 1.0)
    base = 230.0

    grid, pv, sub = [], [], []
    t = t0
    while t < end:
        h = (t - start) / 3600
        prod = 3200 * cloud * math.sin(math.pi * (h - 8) / 10) if 8 <= h <= 18 else 0.0
        prod *= 1 + rnd.gauss(0, 0.03)
        heat = sum(x.power_at(t) for x in hp) + (rnd.gauss(0, 20) if any(x.power_at(t) for x in hp) else 0)
        cons = base + sum(x.power_at(t) for x in loads) + heat + rnd.gauss(0, 15)
        grid.append((t, f"{cons - prod:.1f}"))
        pv.append((t, f"{max(prod, 0):.0f}"))
        sub.append((t, f"{max(heat, 0):.0f}"))
        t += DT
    histories = {
        GRID: grid,
        PV: pv,
        HEAT_PUMP: sub,
        WATER_HEATER: [(t0, "off"), *switch_history([heater])],
    }
    return histories, start, end


def config() -> AnalysisConfig:
    return AnalysisConfig(
        grid_entity=GRID,
        pv_entity=PV,
        submeters=[SubmeterConfig(HEAT_PUMP, "Heat pump")],
        switched_loads=[SwitchedLoadConfig(WATER_HEATER, "Water heater")],
    )


def findings(i: int) -> list[dict]:
    out = [
        {
            "kind": "no_power_sensor",
            "entity_id": "sensor.dryer_energy",
            "detail": "Dryer: no power sensor, not subtracted",
        }
    ]
    if i == N_DAYS - 1:
        out.append(
            {
                "kind": "tracking_gap",
                "entity_id": "sensor.water_heater_energy",
                "detail": "Water heater: declared 0.80 kWh, measured 6.10 kWh",
            }
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    reports: dict[str, dict] = {}
    for i in range(N_DAYS):
        day = FIRST_DAY + dt.timedelta(days=i)
        histories, start, end = simulate(day, seed=i + 1)
        cfg = config()
        report = analyze(
            histories, cfg, start, end, local_hour=lambda ts: dt.datetime.fromtimestamp(ts, TZ).hour
        )
        report.update(
            {
                "day": day.isoformat(),
                "time_zone": str(TZ),
                "addon_version": "0.3.0",
                "generated_at": end + 4.5 * 3600,
                "config": {
                    "grid_entity": GRID,
                    "extra_grid_entities": [],
                    "grid_export_entity": None,
                    "pv_entity": PV,
                    "extra_pv_entities": [],
                    "battery_entity": None,
                    "tracked": [],
                    "findings": [],
                },
                "findings": findings(i),
            }
        )
        report["quality"]["compute_s"] = 0.5
        reports[day.isoformat()] = report

    days = sorted(reports)
    last_end = reports[days[-1]]["end"]
    summaries = {d: {k: v for k, v in r.items() if k != "curve"} for d, r in reports.items()}
    registry = update_registry(
        None,
        summaries,
        last_end,
        RegistryConfig(),
        local_hour=lambda ts: dt.datetime.fromtimestamp(ts, TZ).hour,
    )
    registry["updated_at"] = last_end + 4.5 * 3600
    for s in registry["signatures"]:
        s["created_at"] = registry["updated_at"]
    full, older = days[-FULL_DAYS:], days[:-FULL_DAYS]
    for d in days:
        r = reports[d] if d in full else {**summaries[d], "curve": [], "curve_pruned": True}
        annotate_report(r, registry)
        write(f"report-{d}.json", r)
    write("days.json", {"days": full, "summary_days": older})
    write("signatures.json", registry)
    print(f"{len(days)} days, {len(registry['signatures'])} signatures -> {OUT}")


def write(name: str, data: dict) -> None:
    (OUT / name).write_text(json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
