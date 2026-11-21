# Energy Mapper

Energy Mapper finds the appliances of the home in the curve of the
whole-house power meter (non-intrusive load monitoring). Every night it
analyses the previous day from the recorder history and stores a report:
house consumption, base load, appliances driven by a switch, recurring
signatures (appliances not identified yet) and the biggest peaks with what
was running.

The add-on only **reads** Home Assistant: states, entity registry, Energy
dashboard settings and recorder history. It never calls a service and never
changes your configuration. Everything stays local: no cloud service, no
telemetry.

The reports are shown by the **Energy Mapper integration** (installed with
HACS), which finds this add-on automatically and adds the Energy Mapper panel
to the sidebar. The integration also turns the switched loads and the named
signatures into devices with sensors and **hourly energy statistics** that
can be added to the Energy dashboard. Installation, configuration and how to read the panel: see
the [README](https://github.com/sylvaing/ha-energy-mapper#readme). How the
analysis works: see the
[design document](https://github.com/sylvaing/ha-energy-mapper/blob/main/docs/design.md).

Results are **estimates** from one meter, not measurements: only the
sub-meters are measured.

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
5. Then the signatures to review are compared with the state changes of the
   Home Assistant entities that may tell when an appliance runs (see
   [Entity suggestions](#entity-suggestions)).

Days run from midnight to midnight in the time zone of Home Assistant.

## Configuration

Nothing to configure if the **Energy dashboard** is set up: the add-on reads
the grid meter, the solar production, the battery and the individual devices
from it, and finds the power sensor (W) that goes with each energy sensor
(kWh) of the dashboard.

An appliance whose estimate you replaced, in the Energy dashboard, by its
Energy Mapper statistic (`energy_mapper:load_…_energy`) stays measured: the
add-on maps the statistic back to its relay, with the polarity it learned.

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
| `label`, `appliance` | Name and key of the Home Assistant appliance this signature belongs to (`null` when none). A named signature is never forgotten. |
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
| `expected` | `true` for a signature declared by the user before it is seen ("Kettle, 2000 W"): no use yet; the first cluster of its group within 12 % of its power takes its id and name. |
| `review` | The user's decision in the review of the panel, when there is one (see [Review](#review)). |

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
| Signature registry (`registry.json`) | while a signature's weight is above 1 %; forever once it is named; the 100 most recently ignored ones after they fade out | tens of KB |
| Energy Mapper statistic id of each published appliance (`statistic_ids.json`) | forever | a few KB |
| The user's appliances, pushed by the integration (`appliances.json`) | until replaced | a few KB |
| State changes of the candidate entities, per day (`changes/`, gzip) | 31 days | ~10–100 KB per day |
| Entity suggestions of the signatures to review (`suggestions.json`) | replaced every night | a few KB |

Pruning runs after each nightly analysis. The registry is rebuilt from the
day summaries if it is missing or unreadable. Reports stored as plain JSON
by version 0.1 are compressed on start.

## Internal API

The Energy Mapper integration reads the reports from the add-on on port 8099
of the internal network (the port is not exposed on the host). The add-on
announces itself to Home Assistant through Supervisor discovery.

| Request | Answer |
|---|---|
| `GET /api/health` | `{"status": "ok", "version": "0.8.0", "last_run": <unix time or null>, "days": <number of full reports>, "signatures": <registry size>, "storage_bytes": <size of /data>}` |
| `GET /api/days` | `{"days": [...], "summary_days": [...], "generated": {"YYYY-MM-DD": <unix time>}}`: days with a full report, and older days with only a summary, oldest first; `generated` tells when each full report was made |
| `GET /api/report/YYYY-MM-DD` | The report with the registry ids, or 404. For a day with only a summary: `curve` is empty and `curve_pruned` is `true` |
| `GET /api/hourly/YYYY-MM-DD` | The energy of each appliance per hour of that day (below), or 404 when the day has no per-minute curve |
| `GET /api/signatures` | The signature registry (below) |
| `POST /api/analyze?day=YYYY-MM-DD` | Analyses that day now: `{"day": "...", "ok": true}`, or `{"day": "...", "ok": false, "error": "<code>", "message": "..."}` |
| `GET /api/settings` | The settings pushed by the integration and what applies without them (below) |
| `PUT /api/settings` | Replaces the settings (JSON object, below): `{"settings": {...}}` as stored, or 400 `invalid_settings` |
| `GET /api/appliances` | The user's appliances: `{"appliances": [...]}` (below) |
| `PUT /api/appliances` | Replaces them: `{"appliances": [...]}` as stored, or 400 `invalid_appliances` |
| `GET /api/review` | The signatures to review, waiting and ignored, with their entity suggestions (below) |
| `POST /api/review` | `{"id", "action": "validate", "wait", "ignore" or "restore", "appliance"?}`: records a decision: `{"signature": {...}}` with its `review_state`; 404 `not_found`, 400 `invalid_review` |
| `POST /api/signatures` | `{"kind": "steady" or "cycling", "band": "short", "medium" or "long", "power_w", "label"}`: the signature of an appliance not seen yet: `{"signature": {...}, "created": true}`; an existing unnamed signature at that power is returned as is (`created: false`); 409 `conflict` when it already belongs to another appliance |

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

### Hourly energy

`GET /api/hourly/YYYY-MM-DD` splits the day's per-minute curve into UTC hours,
the way Home Assistant stores long-term statistics (24 hours, 23 or 25 on DST
days; with a half-hour time zone offset the first and last hours are shared
with the neighbouring days, each day counting only its own minutes):

```json
{
  "day": "2027-05-09", "start": 1809900000.0, "end": 1809986400.0,
  "time_zone": "Europe/Paris", "generated_at": 1809990000.0, "resolution_s": 60,
  "hours": [
    {"start": 1809900000, "house": 0.41, "base": 0.21, "other": 0.05,
     "known": {"switch.water_heater": 0.15}, "signatures": {"short-2000w": 0.0333}}
  ],
  "energy_kwh": {"house": 9.8, "base": 4.9, "other": 1.2, "base_w": 205,
                 "known": {"switch.water_heater": 3.6}, "signatures": {"short-2000w": 0.21}},
  "last_active": {"known": {"switch.water_heater": 1809930000.0}, "signatures": {"short-2000w": 1809980000.0}},
  "uses": {"known": {"switch.water_heater": 1}, "signatures": {"short-2000w": 3}},
  "names": {"switch.water_heater": "Water heater"}
}
```

Energies are in kWh. Switched loads are keyed by entity id and signatures by
**registry id**: each minute of a daily signature is shared among its
activations running in that minute, in proportion of their energy, and each
share goes to the registry signature of its activation (`?` when it matches
none). `last_active` is the end of the last minute with some energy, `uses`
the number of activations (signatures) or on-periods (switched loads).

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
| `excluded_switched_loads` | List of entity ids: switches of the Energy dashboard not measured as switched loads (their estimate is subtracted like any other); a switched load of `switched_loads` or of an appliance is still measured |
| `step_threshold_w` | 50 to 10000 |
| `peak_threshold_w` | 100 to 100000 |
| `analysis_time` | `HH:MM` (or `HH:MM:SS`, stored as `HH:MM`); applies after the next analysis |
| `backfill_days` | 0 to 365 |
| `half_life_days`, `peak_window_days` | 1 to 365 |
| `report_retention_days` | 7 to 3650 |
| `review_min_uses`, `review_min_days` | 2 to 1000 and 1 to 365: a signature is proposed for review once seen this many times on this many different days (5 and 3) |
| `review_wait_days`, `review_wait_factor` | 1 to 365 and 1.2 to 10: a signature put on hold comes back after this many days, or once its uses reached this multiple of the number it had (14 and 2) |
| `review_suggest_score` | 50 to 100 (%): an entity is suggested for a signature when its state changes at this share of the signature's starts and stops (80) |

An unknown key or an invalid value refuses the whole request (400,
`{"error": "invalid_settings", "message": "<key>: ..."}`); the body is limited
to 64 KB. `GET /api/settings` answers:

```json
{
  "settings": {"pv_entity": "sensor.solar_power"},
  "options": {"backfill_days": 30},
  "defaults": {"analysis_time": "04:30", "backfill_days": 30, "half_life_days": 30,
               "report_retention_days": 90, "peak_window_days": 30, "grid_invert": false,
               "battery_invert": false, "step_threshold_w": 300.0, "peak_threshold_w": null,
               "review_min_uses": 5, "review_min_days": 3, "review_wait_days": 14, "review_wait_factor": 2.0,
               "review_suggest_score": 80},
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

### Appliances

The appliances are managed in Home Assistant (one config subentry each) and
the integration pushes the whole list after every change, with
`PUT /api/appliances`; the add-on stores it in `/data/appliances.json` and
applies it:

```json
{"appliances": [
  {"key": "load:switch.water_heater", "name": "Water heater",
   "statistic_id": "energy_mapper:load_switch_water_heater_energy",
   "sources": [{"type": "switched_load", "entity_id": "switch.water_heater", "on_states": ["on"]}]},
  {"key": "signature:short-2000w", "name": "Kettle",
   "sources": [{"type": "signature", "id": "short-2000w", "kind": "steady", "band": "short", "power_w": 2000}]},
  {"key": "signature:long-500w", "name": "Dryer",
   "sources": [{"type": "submeter", "entity_id": "sensor.dryer_plug_power"}]}
]}
```

- a **signature** source gets the appliance's name as `label` in the registry
  (labels no appliance declares are cleared: deleting an appliance in Home
  Assistant forgets its name). A declared signature missing from the registry
  is recreated from its `kind`, `band` and `power_w`;
- a **switched load** source is analysed as a switched load named after the
  appliance (on-states: the declared ones, else those of the settings, else
  `on`; the polarity is still detected automatically);
- a **sub-meter** source is subtracted before the analysis, in addition to the
  sub-meters of the settings or of the Energy dashboard.

An appliance with several sources is the result of a merge: the integration
adds their energy. A source belongs to one appliance only; the keys are
unique; at most 200 appliances of 20 sources.

### Review

The Energy Mapper panel asks the user about the signatures seen often enough
to be an appliance (`review_min_uses` uses on `review_min_days` different
days). Each signature of the registry is in one review state:

| `review_state` | When |
|---|---|
| `proposed` | Seen often enough, no decision: shown in the *To review* section |
| `waiting` | Put on hold: until its uses reach `review_wait_factor` times the number at the decision, or `review_wait_days` days later |
| `ignored` | Never proposed again, until restored. Also a signature validated for an appliance that was deleted since |
| `validated` | Named (a signature source of an appliance), or validated by linking it to a switch or a power sensor of an appliance that still exists |
| `immature` | Not seen enough yet |

The decision is stored in the registry signature as `review`
(`{"state": "waiting", "at", "count", "until_count", "until"}`,
`{"state": "ignored", "at"}` or `{"state": "validated", "at", "appliance"}`)
and follows its id from night to night. An ignored signature is kept after it
fades out (the 100 most recently ignored), so that the same appliance seen
again later keeps the decision. `GET /api/review` answers the signatures
`proposed` (most energy first), `waiting` and `ignored`, each with its
`review_state`:

```json
{
  "criteria": {"min_uses": 5, "min_days": 3, "wait_days": 14, "wait_factor": 2.0},
  "now": 1810000000.0,
  "counts": {"proposed": 2, "waiting": 1, "ignored": 0, "validated": 4},
  "signatures": [{"id": "medium-2100w", "review_state": "proposed", "...": "registry fields",
                  "suggestions": [{"entity_id": "fan.attic", "score": 0.92, "on_share": 1.0,
                                   "off_share": 0.83, "explained": 0.9, "matches": 11, "steps": 12,
                                   "days": 5, "days_compared": 6, "on_states": ["on"],
                                   "inverted": false, "match": "edge"},
                                  {"entity_id": "climate.room", "attribute": "hvac_action",
                                   "match": "containment", "score": 0.97, "contained": 0.97,
                                   "time_share": 0.3, "used": 0.8, "matches": 40, "sessions": 41,
                                   "days": 20, "days_compared": 20, "chance": 1e-20,
                                   "on_states": ["heating"], "inverted": false,
                                   "appliance": {"key": "load:switch.radiator", "name": "Radiator",
                                                 "via": "device"}}]}],
  "suggest": {"computed_at": 1810000000.0, "days": 30, "candidates": 42, "attribute_candidates": 3,
              "min_score": 0.8,
              "unrecorded_domains": ["media_player"], "unrecorded_entities": 3}
}
```

`POST /api/review` with `"action": "validate"` needs the key of the
appliance (`appliance`) the integration created for it: the add-on only
records the decision, the appliance itself is pushed with
`PUT /api/appliances`.

### Entity suggestions

Every night, after the registry update, the add-on compares the signatures
`proposed` and `waiting` (at most 50) with the state changes of the
**candidate entities**: lights, switches, input booleans, fans, climate,
humidifier and water heater entities (their state: the HVAC or operation
mode), media players, binary sensors, and sensors of the `enum` device class
(an operation state). Diagnostic and configuration entities, Energy Mapper's
own entities, the meters and sub-meters the analysis uses and the excluded
switched loads are left out (at most 500). The switched loads and the
entities linked to an appliance are compared too: a signature matching one
is probably a part of that appliance.

Two **attributes** are compared as if they were states, for their domain
only (at most 50 entities): `hvac_action` of `climate` entities (`heating`,
`idle`... while the state stays `heat`) and `action` of `humidifier`
entities. A suggestion of an attribute has `attribute`.

The changes of a day, and the state at midnight, are read once from the
recorder (right after the day is analysed; the days of the last 30 analysed
before, or before 0.8.0, are read at the next nightly run; attributes in a
separate request with attributes) and kept 31 days in `/data/changes`. An
entity changing more than 100 times a day (300 for an attribute) is not
compared that day.

For each signature and entity, over the last 30 days, two tests:

- **Edge** (`match: "edge"`): each start (*on* step) and end (*off* step) of
  the signature's activations is matched when the entity changes within
  twice the meter's sampling interval (60 to 300 s) into one of its *on
  states* (the states it enters more often at on steps than at off steps;
  *off* for a pilot-wire relay) for an on step, or into another state for an
  off step. `score` is the share of the steps matched, `on_share` and
  `off_share` the same per kind of step, and `explained` the share of the
  entity's own changes that happen near a step. Suggested when `score` is at
  least `review_suggest_score` (80 %), at least 5 steps are matched on 3
  different days, and `explained` is at least 30 % (an entity that changes
  all the time is never suggested).
- **Containment** (`match: "containment"`), for an entity that stays in a
  state for long periods while the load runs inside them (a thermostat
  heating for an hour while its radiator cycles): the on states are those
  holding a larger share of the signature's on-time than of the entity's
  time; `score` (`contained`) is the share of the signature's on-time spent
  in them, `time_share` the share of the entity's time in them, `sessions`
  the signature's uses grouped when less than 30 minutes apart, `matches`
  those spent mostly inside, `chance` the probability of that many sessions
  inside by chance (binomial, with `time_share`), and `used` the share of the
  entity's on periods with a use. Suggested when `contained` is at least
  `review_suggest_score`, at least 5 sessions are inside on 3 different days,
  `used` is at least 30 % and `chance` at most 10⁻⁴ (an entity that is on
  most of the time is never suggested).

The edge matches come first, then by score; the best 3 are kept. A
suggested entity that belongs to an appliance (one of its sources, or in the
same Home Assistant device as one) has `appliance`: the panel proposes to
merge the signature into it. `suggest.unrecorded_domains` lists the
candidate domains none of whose entities is recorded (recorder exclusions).

Error codes: `invalid_day` (400: not a date, or a day that is not over),
`no_grid` (422: no grid power sensor found), `no_data` (422: no grid data in
the recorder for that day), `partial_day` (422: the recorder history starts
after midnight), `ha_error` (502: Home Assistant unreachable), `internal`
(500).

## Running outside the Supervisor

Home Assistant Container and Core are not supported yet: no standalone image
is published (planned in
[#19](https://github.com/sylvaing/ha-energy-mapper/issues/19)). For
development, the service runs against any Home Assistant instance with a
long-lived access token, from a clone of the repository:

```sh
cd energy_mapper/app
HA_URL=http://homeassistant.local:8123 HA_TOKEN=<token> \
  DATA_DIR=/tmp/energy_mapper PORT=8099 python3 main.py
```

Options are then read from `$DATA_DIR/options.json` (same keys as above), and
the integration is set up with the address of the service.

## Limits

- Appliances shorter than two meter readings cannot be seen: a meter that
  reports every 10 seconds is much better than one every minute.
- The analysis needs the full history of the grid power sensor: do not
  exclude it from the recorder.
- Appliances below `step_threshold_w`, loads whose power varies continuously
  (EV chargers, inverter heat pumps) and multi-phase appliances (one signature
  per heating phase) are not recognised well yet. See the
  [README](https://github.com/sylvaing/ha-energy-mapper#limits) for the full
  list and the roadmap.
