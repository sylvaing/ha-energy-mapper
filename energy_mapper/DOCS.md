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
3. Each report is stored, gzip-compressed, in `/data/reports`. The folder is
   part of the add-on backups.
4. After each analysis the **signature registry** is updated: the
   activations of all the stored days are clustered together to learn the
   appliances of the home across days (see below), and old data is pruned.

Days run from midnight to midnight in the time zone of Home Assistant.

## Configuration

Nothing to configure if the **Energy dashboard** is set up: the add-on reads
the grid meter, the solar production, the battery and the individual devices
from it, and finds the power sensor (W) that goes with each energy sensor
(kWh) of the dashboard.

All other options are manual overrides, for installations without an Energy
dashboard or when the automatic choice is wrong. With the **Energy Mapper
integration**, set them in its options instead (Settings → Devices & services
→ Energy Mapper → Configure): the integration keeps them in Home Assistant and
sends them to the add-on, where they take precedence over the add-on options
below (see `PUT /api/settings`).

| Option | Default | Description |
|---|---|---|
| `analysis_time` | `04:30` | Local time of the nightly analysis. |
| `backfill_days` | `30` | Past days analysed when they have no report yet. |
| `half_life_days` | `30` | Age at which a past use counts half in the signature registry. |
| `report_retention_days` | `90` | How long the full daily reports (with the curve) are kept. |
| `peak_window_days` | `30` | A peak is notable above 80 % of the highest power of this many past days. |
| `grid_entity` | Energy dashboard | Grid power sensor (W or kW), positive when importing. |
| `grid_export_entity` | Energy dashboard | Export power sensor, for meters that report import and export separately. |
| `grid_invert` | `false` | The grid sensor is positive when exporting. |
| `pv_entity` | Energy dashboard | Solar power sensor. |
| `battery_entity` | Energy dashboard | Battery power sensor, positive when discharging. |
| `battery_invert` | `false` | The battery sensor is positive when charging. |
| `submeters` | Energy dashboard | Power sensors of measured appliances, subtracted before the analysis. |
| `switched_loads` | Energy dashboard | Appliances driven by a switch. |
| `step_threshold_w` | `300` | Smallest power change considered as an appliance switching. |
| `peak_threshold_w` | 80 % of the recent maximum | Fixed threshold of the notable peaks (for example the subscribed power). |
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
- `max_house_w`, `peak_threshold_w`: the highest house power of the day and
  the threshold of the notable peaks;
- `curve`: the stacked breakdown of the house load, one point per minute;
- `known_loads`: switch-driven appliances with their measured power;
- `signatures`, `activations`: the appliances found in the curve that day.
  Their `signature` is a daily cluster id; the API adds `registry_id`, the
  long-lived id of the signature registry;
- `peaks`: the day's top episodes (up to 5, above 80 % of the day's own
  maximum, so a normal day always has some) and what was running
  (`registry_id` on the signature parts). `notable` is `true` when the
  episode reaches `peak_threshold_w`;
- `quality`: number of samples, longest gap, computing time;
- `config`: the sensors used, derived from the Energy dashboard;
- `findings`: what is worth checking (sensor without power sensor,
  unavailable device, estimated device far from what was measured...).

## Signature registry

An appliance used once a day is an isolated case in each daily report: it
only becomes recognisable across days. The registry clusters the activations
of all the stored days together, each one weighted by its age with an
exponential decay: with the default half-life of 30 days, a use from 30 days
ago counts half, 60 days a quarter, and so on. Recent habits dominate, a
replaced appliance fades out within weeks, and a rare appliance (used once a
week or a few times a year) stays known as long as its weight is meaningful.

Each signature keeps:

| Field | Description |
|---|---|
| `id` | Stable identity, for example `short-2000w` (steady, under 10 min), `medium-...` (10 to 60 min), `long-...` (over 1 h) or `cyc-1400w` (cycling session). |
| `kind`, `band` | `steady` or `cycling`; `short`, `medium`, `long` or `session`. |
| `label` | Name given by the user (`null` for now). A named signature is never forgotten. |
| `power_w`, `duration_s` | Typical power and duration (weighted medians). |
| `on_s`, `period_s`, `duty` | Typical cycle of a cycling session (`null` for steady loads). |
| `weight` | Weighted number of uses (each use counts 1 when new, 0.5 after one half-life). |
| `count`, `days_seen` | Raw number of uses and of days with a use, over the stored history. |
| `per_week` | Weighted uses per week. |
| `first_seen`, `last_seen` | First and last day with a use. |
| `hours` | Weighted uses per local hour (24 values). |
| `energy_kwh`, `weighted_energy_kwh` | Energy of the stored uses, raw and weighted. |
| `examples` | The most recent uses: `day`, `start`, `end` (unix time), `power_w`. |
| `created_at` | When the id was given (unix time). |

The id stays the same from one night to the next as long as the cluster is
still there: the clusters of the night are matched to the signatures of the
previous registry by kind, duration band and power (within 12 %). A new
cluster gets a new id. The daily reports keep their own daily cluster ids
(`signature`); the API adds the matching registry id (`registry_id`).

The registry also remembers the **switched loads** (power and polarity
learned on the days with transitions, with the same decay; a day without
transitions keeps the last known values, and the learned power is used when
a day has nothing to measure it) and the threshold of the **notable peaks**:
80 % of the highest power of the last `peak_window_days` days, or of the day
itself when higher, to follow the seasons (a fixed `peak_threshold_w` replaces
it). Every daily report still lists the day's top episodes; only those that
reach this threshold are flagged `notable`. During the first days the
threshold is simply 80 % of the day's maximum; the days of the first
backfill, analysed newest first, only know the days stored before them.

## Storage

Everything is kept in `/data` (included in the add-on backups) and stays
bounded, whatever the age of the installation:

| Kept | Retention | Order of size |
|---|---|---|
| Full daily report, with the per-minute curve (`reports/`, gzip) | `report_retention_days` (90 days) | ~2 MB |
| Day summary: everything but the curve (`summaries/`, gzip) | until the weight of its activations falls below 1 % (~200 days with a 30-day half-life) | ~1 MB |
| Signature registry (`registry.json`) | while a signature's weight is above 1 %; forever once it is named | tens of KB |

Pruning runs after each nightly analysis. The registry is rebuilt from the
day summaries if it is missing or unreadable. Reports stored as plain JSON
by version 0.1 are compressed on start.

## Internal API

The Energy Mapper integration reads the reports from the add-on on port 8099
of the internal network (the port is not exposed on the host). The add-on
announces itself to Home Assistant through Supervisor discovery.

| Request | Answer |
|---|---|
| `GET /api/health` | `{"status": "ok", "version": "0.3.0", "last_run": <unix time or null>, "days": <number of full reports>, "signatures": <registry size>, "storage_bytes": <size of /data>}` |
| `GET /api/days` | `{"days": [...], "summary_days": [...]}`: days with a full report, and older days with only a summary, oldest first |
| `GET /api/report/YYYY-MM-DD` | The report with the registry ids, or 404. For a day with only a summary: `curve` is empty and `curve_pruned` is `true` |
| `GET /api/signatures` | The signature registry (below) |
| `POST /api/analyze?day=YYYY-MM-DD` | Analyses that day now: `{"day": "...", "ok": true}`, or `{"day": "...", "ok": false, "error": "<code>", "message": "..."}` |
| `GET /api/settings` | The settings pushed by the integration and what applies without them (below) |
| `PUT /api/settings` | Replaces the settings (JSON object, below): `{"settings": {...}}` as stored, or 400 `invalid_settings` |

`GET /api/signatures` answers:

```json
{
  "version": 1,
  "updated_at": 1800000000.0,
  "half_life_days": 30,
  "power_tol": 0.12,
  "days": 120,
  "first_day": "2027-01-10",
  "last_day": "2027-05-09",
  "signatures": [
    {
      "id": "short-2000w", "kind": "steady", "band": "short", "label": null,
      "power_w": 2010, "duration_s": 150, "on_s": null, "period_s": null, "duty": null,
      "weight": 21.4, "count": 118, "days_seen": 97, "per_week": 6.9,
      "first_seen": "2027-01-10", "last_seen": "2027-05-09",
      "hours": [0, 0, 0, 0, 0, 0, 0, 4.1, 3.2, 0.5, 0, 0, 0, 0, 0, 0, 0, 0, 1.2, 2.6, 0.8, 0, 0, 0],
      "energy_kwh": 9.9, "weighted_energy_kwh": 1.8,
      "examples": [{"day": "2027-05-09", "start": 1810000000.0, "end": 1810000150.0, "power_w": 2004}],
      "created_at": 1789000000.0
    }
  ],
  "switched_loads": [
    {
      "entity_id": "switch.water_heater", "name": "Water heater", "power_w": 2400,
      "inverted": false, "on_states": ["on"], "weight": 18.2, "days_seen": 95,
      "first_seen": "2027-01-10", "last_seen": "2027-05-09"
    }
  ],
  "peak": {"window_days": 30, "relative": 0.8, "reference_w": 7200, "threshold_w": 5760, "days": 30}
}
```

Signatures are sorted by decreasing weight. Before the first analysis the
lists are empty and `peak` is `null`.

### Settings

The integration keeps the user's settings in Home Assistant and pushes them
with `PUT /api/settings` (on save and when it starts). They are stored in
`/data/settings.json` and take precedence, key by key, over the add-on
options; a key that is absent falls back to the add-on option, then to the
default (or, for the sensors, to the Energy dashboard). `PUT {}` removes them
all, and the add-on then behaves exactly as configured by its options.

Accepted keys (all optional; `null`, `""` and `[]` mean "absent"):

| Key | Value |
|---|---|
| `grid_entity`, `grid_export_entity`, `pv_entity`, `battery_entity` | A `sensor.` entity id |
| `grid_invert`, `battery_invert` | `true` or `false` |
| `submeters` | List of `sensor.` entity ids (replaces those of the Energy dashboard) |
| `switched_loads` | List of `{"entity_id", "name"?, "on_states"?: [...]}` (added to, or replacing, those of the Energy dashboard) |
| `step_threshold_w` | 50 to 10000 |
| `peak_threshold_w` | 100 to 100000 |
| `analysis_time` | `HH:MM` (or `HH:MM:SS`, stored as `HH:MM`); applies after the next analysis |
| `backfill_days` | 0 to 365 |
| `half_life_days`, `peak_window_days` | 1 to 365 |
| `report_retention_days` | 7 to 3650 |

An unknown key or an invalid value refuses the whole request (400,
`{"error": "invalid_settings", "message": "<key>: ..."}`); the body is limited
to 64 KB. `GET /api/settings` answers:

```json
{
  "settings": {"pv_entity": "sensor.solar_power"},
  "options": {"backfill_days": 30},
  "defaults": {"analysis_time": "04:30", "backfill_days": 30, "half_life_days": 30,
               "report_retention_days": 90, "peak_window_days": 30, "grid_invert": false,
               "battery_invert": false, "step_threshold_w": 300.0, "peak_threshold_w": null},
  "automatic": {"grid_entity": "sensor.grid_power", "grid_export_entity": null,
                "pv_entity": null, "battery_entity": null, "submeters": [],
                "switched_loads": [{"entity_id": "switch.water_heater", "name": "Water heater", "on_states": ["on"]}]}
}
```

`options` are the add-on options among these keys, `defaults` the values of
the scalar keys without settings (add-on option, else default), and
`automatic` the sensors the analysis uses without settings (Energy dashboard
and add-on options), read live from Home Assistant (`null` when it cannot be
read).

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
