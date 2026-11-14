# Energy Mapper

Energy Mapper finds the appliances of the home in the curve of the
whole-house power meter (non-intrusive load monitoring). Every night it
analyses the previous day from the recorder history and stores a report:
house consumption, base load, appliances driven by a switch, recurring
signatures (appliances not identified yet) and the biggest peaks with what
was running.

The add-on only **reads** Home Assistant: states, entity registry, Energy
dashboard settings and recorder history. It never calls a service and never
changes your configuration.

## How it works

1. On start, the add-on analyses the past days that have no report yet, up to
   `backfill_days`, as long as the recorder still holds the whole day.
2. Every day at `analysis_time` it analyses the previous day (and catches up
   any day missed while it was stopped).
3. Each report is stored in `/data/reports/YYYY-MM-DD.json`. The folder is
   part of the add-on backups.

Days run from midnight to midnight in the time zone of Home Assistant.

## Configuration

Nothing to configure if the **Energy dashboard** is set up: the add-on reads
the grid meter, the solar production, the battery and the individual devices
from it, and finds the power sensor (W) that goes with each energy sensor
(kWh) of the dashboard.

All other options are manual overrides, for installations without an Energy
dashboard or when the automatic choice is wrong:

| Option | Default | Description |
|---|---|---|
| `analysis_time` | `04:30` | Local time of the nightly analysis. |
| `backfill_days` | `30` | Past days analysed when they have no report yet. |
| `grid_entity` | Energy dashboard | Grid power sensor (W or kW), positive when importing. |
| `grid_export_entity` | Energy dashboard | Export power sensor, for meters that report import and export separately. |
| `grid_invert` | `false` | The grid sensor is positive when exporting. |
| `pv_entity` | Energy dashboard | Solar power sensor. |
| `battery_entity` | Energy dashboard | Battery power sensor, positive when discharging. |
| `battery_invert` | `false` | The battery sensor is positive when charging. |
| `submeters` | Energy dashboard | Power sensors of measured appliances, subtracted before the analysis. |
| `switched_loads` | Energy dashboard | Appliances driven by a switch. |
| `step_threshold_w` | `300` | Smallest power change considered as an appliance switching. |
| `peak_threshold_w` | 80 % of the maximum | Fixed threshold of the peaks (for example the subscribed power). |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. |

Example of manual settings:

```yaml
grid_entity: sensor.grid_power
pv_entity: sensor.solar_power
submeters:
  - sensor.heat_pump_power
switched_loads:
  - entity_id: switch.water_heater
    name: Water heater
  - entity_id: switch.radiator_pilot_wire
    name: Radiator
    on_states:
      - "off"
```

`on_states` lists the states in which the appliance runs (default `on`). A
switch that cuts the pilot wire of a radiator lets it heat when it is `off`.

## Reports

Each report is a JSON document with:

- `day`, `start`, `end`, `time_zone`: the analysed period;
- `energy_kwh`: house, grid import and export, solar, sub-metered, base load,
  known loads, signatures and the unexplained rest;
- `curve`: the stacked breakdown of the house load, one point per minute;
- `known_loads`: switch-driven appliances with their measured power;
- `signatures`, `activations`: recurring appliances found in the curve;
- `peaks`: the biggest peaks and what was running;
- `quality`: number of samples, longest gap, computing time;
- `config`: the sensors used, derived from the Energy dashboard;
- `findings`: what is worth checking (sensor without power sensor,
  unavailable device, estimated device far from what was measured...).

## Internal API

The Energy Mapper integration reads the reports from the add-on on port 8099
of the internal network (the port is not exposed on the host). The add-on
announces itself to Home Assistant through Supervisor discovery.

| Request | Answer |
|---|---|
| `GET /api/health` | `{"status": "ok", "version": "0.1.0", "last_run": <unix time or null>, "days": <number of reports>}` |
| `GET /api/days` | `{"days": ["YYYY-MM-DD", ...]}`, oldest first |
| `GET /api/report/YYYY-MM-DD` | The report, or 404 |
| `POST /api/analyze?day=YYYY-MM-DD` | Analyses that day now: `{"day": "...", "ok": true}`, or `{"day": "...", "ok": false, "error": "<code>", "message": "..."}` |

Error codes: `invalid_day` (400: not a date, or a day that is not over),
`no_grid` (422: no grid power sensor found), `no_data` (422: no grid data in
the recorder for that day), `partial_day` (422: the recorder history starts
after midnight), `ha_error` (502: Home Assistant unreachable), `internal`
(500).

## Running outside the Supervisor

The same image runs on Home Assistant Container or Core installations with a
long-lived access token:

```sh
docker run -d -p 8099:8099 -v /path/to/data:/data \
  -e HA_URL=http://homeassistant.local:8123 -e HA_TOKEN=<token> \
  <image>
```

Options are then read from `/data/options.json` (same keys as above).

## Limits

- Appliances shorter than two meter readings cannot be seen: a meter that
  reports every 10 seconds is much better than one every minute.
- The analysis needs the full history of the grid power sensor: do not
  exclude it from the recorder.
