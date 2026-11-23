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
6. The newest day analysed by a pass is also used for the **self-test**: a
   measured device is hidden in a copy of the analysis, to check that the
   curve finds it again (see [Self-test](#self-test)).

Days run from midnight to midnight in the time zone of Home Assistant.

## Configuration

Nothing to configure if the **Energy dashboard** is set up: the add-on reads
the grid meter, the solar production, the battery and the individual devices
from it, and finds the power sensor (W) that goes with each energy sensor
(kWh) of the dashboard.

An appliance whose estimate you replaced, in the Energy dashboard, by its
Energy Mapper statistic (`energy_mapper:load_…_energy`) stays measured: the
add-on maps the statistic back to its relay, with the polarity it learned.
The integration does that replacement for you (*Take over tracking* on the
appliance's card, after a confirmation); the replaced sensor is then no longer
read, so it is no longer subtracted from the house curve.

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
| `self_test_devices` | `1` | Measured devices (sub-meters) hidden per night by the [self-test](#self-test); `0` disables it. |
| `grid_entity` | Energy dashboard | Grid power sensor (W or kW), positive when importing. |
| `grid_export_entity` | Energy dashboard | Export power sensor, for meters that report import and export separately. |
| `grid_invert` | `false` | The grid sensor is positive when exporting. |
| `pv_entity` | Energy dashboard | Solar power sensor. |
| `battery_entity` | Energy dashboard | Battery power sensor, positive when discharging. |
| `battery_invert` | `false` | The battery sensor is positive when charging. |
| `submeters` | Energy dashboard | Power sensors of measured appliances, subtracted before the analysis. |
| `switched_loads` | Energy dashboard | Appliances driven by a switch. |
| `step_threshold_w` | `300` | Smallest power change considered as an appliance switching. |
| `night_step_threshold_w` | empty (off) | Smaller steps, down to this value, are also kept while the solar panels produce nothing: see [Night threshold](#night-threshold). Only with a PV sensor. |
| `adaptive_threshold` | `false` | Experimental: smaller power changes are also kept where they stand out of the measured noise of the curve, and listed apart: see [Threshold adapted to the noise](#threshold-adapted-to-the-noise). |
| `cold_appliances` | `false` | Experimental, needs `adaptive_threshold`: the small uses that come back regularly (fridge, freezer) are learned, followed and proposed for review: see [Cold appliances](#cold-appliances). |
| `peak_threshold_w` | 80 % of the recent maximum | Fixed threshold of the notable peaks (for example the subscribed power). |
| `live_view` | `true` | The [live view](#live-view) of the panel's *Live* tab. It runs only while the tab is open; `false` turns it off entirely (kill switch). Add-on option only. |
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

A relay (`switch`, `input_boolean`, any entity but a `sensor`) is measured
from the power step at its transitions. An **operation state** (a `sensor`,
typically of the `enum` device class with `run` / `stop`, add-on 0.14.0+) is
measured over each run instead: what the house draws above its usual level
around the run (the mean of the 20 minutes before and after, without their
highest 20 %), minute by minute. A connected appliance that starts slowly
(a heat-pump dryer), or whose power changes during its cycle (a dishwasher),
gets its energy; a run that draws less than 50 W on average (a delayed start)
does not count; a pause shorter than 10 minutes stays inside the run. Such a
load does not hide the appliance's own steps from the analysis, so its
signature can still be learned. Give its `on_states` (for example `run`).

A device of the Energy dashboard whose power sensor stays constant (often at
0 W) all day while its own energy counter increases is not a sub-meter: it is
not subtracted that day (role `energy_only`) and the finding
`flat_power_sensor` suggests learning it from its state.

A sub-meter does not report at the same instant as the grid meter: a smart
plug announces a change tens of seconds before or after, or only every 40 s.
Its energy is subtracted as measured, but in between the subtraction is wrong
by the appliance's whole power and would leave false steps (a phantom use of
the appliance's power lasting the lag, or a hole when a start-up peak is
held for 40 s by the plug). Since add-on 0.16.0 those steps are recognised
(the sub-meter's own reading changes while the house does not follow) and
left out of the pairing and of the signatures, also for an appliance on a
plug that modulates faster than the meters report (a hob). An installation
whose sub-meters report with the grid meter is analysed as before, and
estimated sub-meters explain nothing. The report gives, per sub-meter, the
steps explained and the measured lag (`quality.submeters`).

### Night threshold

Lowering `step_threshold_w` reveals small appliances (a TV, lighting, small
kitchen appliances). On a solar installation it also creates false steps in
the daytime: the PV and grid meters drift slightly apart while the sun is up.
`night_step_threshold_w` (add-on 0.13.0+, empty by default) lowers the
threshold only where that cannot happen:

- a step of at least `step_threshold_w` is always kept, as before;
- a smaller step, of at least `night_step_threshold_w`, is kept only when the
  solar production is known and stays below 20 W (median) in the minute
  before and in the minute after it.

It takes a value between 50 W and `step_threshold_w` (150 W is a good start)
and applies to the nightly analysis and to the live view alike. **It only
applies with a PV sensor** (Energy dashboard or `pv_entity`): without one it
has no effect, so lower `step_threshold_w` instead.

### Threshold adapted to the noise

The thresholds above are fixed: they were chosen for the noisiest hour of the
curve and apply in the middle of the night too, when the curve moves by a few
watts and a 70 W compressor stands out clearly. `adaptive_threshold` (add-on
0.17.0+, `false` by default, experimental) adds a threshold that follows the
measured noise:

- the noise is measured all along the curve, over the last 30 minutes (the
  robust spread of the changes the step detector looks at);
- a power change smaller than the thresholds above is also kept when it is at
  least 40 W and 4 times that noise: about 40 W in a quiet night, 100 to
  200 W under a changing sky, nothing more where the curve is noisier than
  that;
- these **small uses** are listed apart in the daily report (`small_uses`).
  They are not signatures: they are not proposed for review, not tracked as
  appliances and not counted in the breakdown of the curve. A real curve has
  tens of them a day; those that come back regularly (a fridge, a freezer)
  are grouped and proposed by the [cold appliances](#cold-appliances) option.

Everything else is analysed exactly as without the option: the same steps,
activations, signatures and energies, in the nightly analysis and in the live
view (whose feed does not show the small uses). The night threshold keeps its
meaning: what it keeps is a normal use, proposed for review as before.

The report also gives, whatever the option, how well the meters let the curve
be read (`quality.meters`): the noise of the curve apart from and during the
solar hours, and the smallest step it can show (`visible_w`). An appliance
measured by a plug is said *too low for the whole-house curve* from that
figure, no longer from the thresholds: under the thresholds but above the
noise, the panel says that the curve shows it and that appliances this small
are not tracked yet.

### Cold appliances

A fridge or a freezer runs a few minutes, pauses and starts again, day and
night. `cold_appliances` (add-on 0.18.0+, `false` by default, experimental)
finds such appliances among the low steps of the adaptive threshold: it does
nothing without `adaptive_threshold`.

- **Learned in the quiet hours.** Where the solar panels produce nothing and
  no larger appliance switches, the on-steps and off-steps that come back
  with the same sizes, the same run length and a regular pause are one
  appliance (at least 5 runs). Two appliances are told apart by their steps,
  which differ (a compressor starts higher than it stops).
- **Followed all day.** The appliance is then followed through the low steps
  of the whole day: a run with one step missing is completed (the other edge
  is looked for in the curve, where the usual run length says it is due), one
  or two runs missing between two others are filled.
- **Estimated where it cannot be followed.** In the busy hours its energy is
  estimated from the rhythm learned in the quiet hours. This part is an
  estimate, not a detection: the report gives it apart (`estimated_kwh`) and
  the panel shows its share.
- **A signature of its own kind** (`cold`, id `cold-80w`): "runs in cycles
  all day, about 80 W, every 43 min". It is proposed for review and named like
  the others; its energy leaves the unexplained share of the day. The model
  is kept by the registry: on a day whose night taught nothing, the appliance
  is still followed; when it is gone, it is no longer reported.

Everything else is analysed as without the option; the small uses stay
listed as before, and the live view is unchanged.

### Solar readings that come late

The house load is the grid power plus the solar production, read from two
meters that do not report at the same moment. When a cloud passes, the grid
power moves at once and the solar value comes a few seconds later: the house
load shows a step that is no appliance, then the opposite step. Since add-on
0.16.0, with a PV sensor, the readings whose solar value is about to move by
half of `step_threshold_w` or more within the next minute are set aside
before the steps are looked for; the level before them is kept meanwhile. A
cloud then shows no step, and an appliance switched on during a cloud is
found with its real power, up to a minute late. There is nothing to set. In
the live view, while the panels produce, a step shows once the solar value
has changed (a minute later at most); at night nothing changes.

This needs a solar power sensor that reports a change of the production
within a minute. Otherwise the finding `slow_solar_meter` says how long it
takes: prefer a local reading of the inverter (a few seconds) to a cloud
service that updates every few minutes.

## Reports

Each report is a JSON document with:

- `day`, `start`, `end`, `time_zone`: the analysed period;
- `energy_kwh`: house, grid import and export, solar, sub-metered, base load,
  known loads, signatures and the unexplained rest;
- `max_house_w`, `peak_threshold_w`: the highest house power of the day and
  the threshold of the notable peaks;
- `curve`: the stacked breakdown of the house load, one point per minute;
- `known_loads`: switch-driven appliances with their measured power;
  an operation-state load adds `mode: "run"` and its `runs`;
- `signatures`, `activations`: the appliances found in the curve that day.
  Their `signature` is a daily cluster id; the API adds `registry_id`, the
  long-lived id of the signature registry. A heater regulating on its own
  thermostat (regular bursts of the same power, at least 4, up to 15 minutes
  apart; add-on 0.16.0+) is one `cycling` activation with its `duty`, `on_s`
  and `period_s`, and `blocks`: `[[start, end]]`, when it was on;
- `cycles`: the candidate cycles of multi-phase appliances (dishwasher,
  washing machine, dryer): `start`, `end`, `power_w` (the heating power),
  `energy_kwh`, `phases` (`kind` `heat` or `motor`, `offset_s`,
  `duration_s`, `power_w`), and `template` when it was recognised as a
  cycle of a `multiphase` signature. A recognised cycle is also one
  activation with `multiphase: true` and its `phases` (its `power_w` is its
  mean power); the activations inside it (its heating phases) are not
  listed again;
- `small_uses` (add-on 0.17.0+, only with `adaptive_threshold`): the uses
  under the thresholds that stand out of the noise of the curve, each with
  `start`, `end`, `power_w`, and the size of its two steps `on_w` and
  `off_w`. They are in no other field of the report;
- `cold` (add-on 0.18.0+, only with `cold_appliances`): the cold appliances
  of the day, each with its model (`on_w`, `off_w`, `power_w`, `on_s`,
  `period_s`, `duty`), its runs (`cycles`: `start`, `end` and `how`:
  `detected`, `completed`, `placed` or `filled`), where it was not followed
  (`estimated`), and `energy_kwh` with its estimated part `estimated_kwh`.
  Each one is also in `signatures` with kind `cold`;
- `peaks`: the day's top episodes (up to 5, above 80 % of the day's own
  maximum, so a normal day always has some) and what was running
  (`registry_id` on the signature parts). `notable` is `true` when the
  episode reaches `peak_threshold_w`;
- `quality`: number of samples, steps (without those the sub-meters'
  reports made), longest gap, computing time; `submeters` (add-on 0.16.0+):
  per measured sub-meter with something to say, `explained_steps` and the lag
  of its isolated changes against the grid meter (`changes`, `located`,
  `lag_s` the median, `lag_min_s`, `lag_max_s`; 0 when it reports between the
  same two grid readings, positive when late); with a PV sensor (add-on
  0.16.0+), `solar_held` (the readings set aside because their solar value
  was about to move) and `solar_interval_s` (how long the solar sensor takes
  to report a change of the production, `null` on a day without enough
  changes); `meters` (add-on 0.17.0+): the reporting intervals of the grid
  meter and of the solar meter while it produces (`grid`, `solar`:
  `median_s`, `p90_s`), the step detector's window (`window_s`), the noise of
  the curve apart from the solar hours and during them (`noise_w.no_solar`,
  `noise_w.solar`, in W; `null` with too few readings) and the smallest step
  the curve can show at its quietest (`visible_w`); with `adaptive_threshold`,
  `low_steps` (the steps kept under the thresholds);
- `ground_truth` (add-on 0.11.0+, only while an appliance learns from a
  sensor): per taught appliance, `key`, `entity_id`, its `runs` that day
  (`start`, `end`, `energy_kwh`, `power_w`, `max_w`, `kind`, `duty`, and
  `on_s`/`period_s` for a cycling run, `phases` for a multi-phase one),
  `covered` (the periods the sensor reported), `covered_s` and `energy_kwh`.
  A connected appliance (add-on 0.14.0+) adds `mode: "state"`,
  `program_recorded` when it has a programme state, and per run `source:
  "state"`, `baseline_w`, `program`, `counter_kwh` and `named_phases`
  (`state`, `offset_s`, `duration_s`, `power_w`);
- `config`: the sensors used, derived from the Energy dashboard;
- `findings`: what is worth checking (sensor without power sensor,
  unavailable device, estimated device far from what was measured...):
  `{kind, entity_id, detail, params}`. `kind` is stable and `params` holds
  what the sentence is made of (add-on 0.11.0+), so the panel writes it in
  the user's language; `detail` is the English sentence.

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
| `id` | Stable identity, for example `short-2000w` (steady, under 10 min), `medium-...` (10 to 60 min), `long-...` (over 1 h), `cyc-1400w` (cycling session), `cycle-2000w` (multi-phase cycle, named after its heating power) or `cold-80w` (cold appliance, add-on 0.18.0+). |
| `kind`, `band` | `steady`, `cycling`, `multiphase` or `cold`; `short`, `medium`, `long`, `session`, `cycle` or `cold`. A `cold` signature also has its model (`on_w`, `off_w`, `on_modes_s`, `run_s`, `pause_s`) and `estimated_kwh`, the part of its energy estimated where it was not followed; one use is one run of the compressor. |
| `label`, `appliance` | Name and key of the Home Assistant appliance this signature belongs to (`null` when none). A named signature is never forgotten. |
| `power_w`, `duration_s` | Typical power and duration (weighted medians). |
| `on_s`, `period_s`, `duty` | Typical cycle of a cycling session or of a thermostat's bursts (`null` for steady loads). |
| `weight` | Weighted number of uses (each use counts 1 when new, 0.5 after one half-life). |
| `count`, `days_seen` | Raw number of uses and of days with a use, over the stored history. |
| `per_week` | Weighted uses per week. |
| `first_seen`, `last_seen` | First and last day with a use. |
| `hours` | Weighted uses per local hour (24 values). |
| `energy_kwh`, `weighted_energy_kwh` | Energy of the stored uses, raw and weighted. |
| `examples` | The most recent uses: `day`, `start`, `end` (unix time), `power_w`. |
| `occurrences` | Every use of the last 60 days as `[start, duration_s, power_w]` (unix time, seconds, watts), sorted by start, at most 600 (the most recent ones): the day raster of the panel (add-on 0.9.0+). |
| `phases`, `cycle_kwh` | `multiphase` only: the template, its phases in order (`kind` `heat` or `motor`, `offset_s`, `duration_s`, `power_w`), and the typical energy of one cycle. |
| `created_at` | When the id was given (unix time). |
| `expected` | `true` for a signature declared by the user before it is seen ("Kettle, 2000 W"): no use yet; the first cluster of its group within 12 % of its power takes its id and name. |
| `review` | The user's decision in the review of the panel, when there is one (see [Review](#review)). |

The id stays the same from one night to the next as long as the cluster is
still there: the clusters of the night are matched to the signatures of the
previous registry by kind, duration band and power (within 12 %). A new
cluster gets a new id. The daily reports keep their own daily cluster ids
(`signature`); the API adds the matching registry id (`registry_id`).

**Multi-phase appliances** (add-on 0.10.0+): the candidate cycles of the
stored days are clustered on their sequence of phases; a pattern seen at
least 3 times becomes a `multiphase` signature, proposed in the review like
the others. Its energy only moves to it once you name it (or validate it in
the review): from the next night, each cycle that matches its template is
one use of it, low-power phases included, and the activations inside the
cycle (its heating phases) no longer count for their own signatures.

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
| Self-test results, one entry per device tested per night (`self_test.json`) | 60 days | a few KB |

Pruning runs after each nightly analysis. The registry is rebuilt from the
day summaries if it is missing or unreadable. Reports stored as plain JSON
by version 0.1 are compressed on start.

## Internal API

The Energy Mapper integration reads the reports from the add-on on port 8099
of the internal network (the port is not exposed on the host). The add-on
announces itself to Home Assistant through Supervisor discovery.

| Request | Answer |
|---|---|
| `GET /api/health` | `{"status": "ok", "version": "0.13.0", "last_run": <unix time or null>, "days": <number of full reports>, "signatures": <registry size>, "storage_bytes": <size of /data>}` |
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
| `GET /api/teaching` | The learning status of the appliances taught by a sensor (see [Learning from a measured device](#learning-from-a-measured-device)) |
| `POST /api/teaching` | `{"appliance": <key>}`: the signature learned for it, `{"signature", "created", "learned"}` |
| `GET /api/self_test` | The success rate of each measured device hidden by the nightly self-test (see [Self-test](#self-test)) |
| `GET /api/live?since=<seq>&wait=<s>` | The [live view](#live-view), a long-poll: a snapshot without `since`, else what changed after it |
| `GET /api/live/stats` | The live view's state and timings (does not start it) |
| `GET /api/live/event?id=<id>&session=<session>` | One event of the running live session, to label it: `{"session", "event"}`; 404 `not_found` (cancelled, or gone 15 minutes after it ended), 409 `session_ended` |
| `GET /api/examples` | The [labelled examples](#labelled-examples) of each appliance, found again in the analysed days |

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
      "occurrences": [[1809740000, 140, 2016], [1809913000, 155, 1998], [1810000000, 150, 2004]],
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
| `submeters_mode` | `automatic` (default) or `manual`: `manual` makes `submeters` the exact list, possibly empty ("no sub-meters", even over the add-on option) |
| `submeters` | List of `sensor.` entity ids (replaces those of the Energy dashboard; stored with `submeters_mode: manual`) |
| `excluded_submeters` | List of `sensor.` entity ids: automatic sub-meters (Energy dashboard or add-on option) not subtracted; not with `manual` |
| `switched_loads` | List of `{"entity_id", "name"?, "on_states"?: [...]}` (added to, or replacing, those of the Energy dashboard) |
| `excluded_switched_loads` | List of entity ids: switches of the Energy dashboard not measured as switched loads (their estimate is subtracted like any other); a switched load of `switched_loads` or of an appliance is still measured |
| `step_threshold_w` | 50 to 10000 |
| `night_step_threshold_w` | 50 to the step threshold (that of the settings, else of the add-on options, else 300) |
| `adaptive_threshold` | `true` / `false` (add-on 0.17.0+) |
| `cold_appliances` | `true` / `false` (add-on 0.18.0+; acts only with `adaptive_threshold`) |
| `peak_threshold_w` | 100 to 100000 |
| `analysis_time` | `HH:MM` (or `HH:MM:SS`, stored as `HH:MM`); applies after the next analysis |
| `backfill_days` | 0 to 365 |
| `half_life_days`, `peak_window_days` | 1 to 365 |
| `report_retention_days` | 7 to 3650 |
| `review_min_uses`, `review_min_days` | 2 to 1000 and 1 to 365: a signature is proposed for review once seen this many times on this many different days (5 and 3) |
| `review_wait_days`, `review_wait_factor` | 1 to 365 and 1.2 to 10: a signature put on hold comes back after this many days, or once its uses reached this multiple of the number it had (14 and 2) |
| `review_suggest_score` | 50 to 100 (%): an entity is suggested for a signature when its state changes at this share of the signature's starts and stops (80) |
| `self_test_devices` | 0 to 10: measured devices hidden per night by the self-test, 0 disables it (1) |

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
               "review_suggest_score": 80, "self_test_devices": 1},
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

An appliance may also have a `teacher`, `{"entity_id": "sensor.oven_plug_power",
"since": <unix time>}` (add-on 0.11.0+, #14): a power sensor that measures it
for a while, a temporary smart plug. While it teaches, the sensor is never
subtracted, whatever else declares it (a sub-meter source of the appliance, a
device of the Energy dashboard, the settings): the appliance stays in the
whole-house curve exactly as it will once the plug is gone. Each analysed day
keeps the sensor's runs in `ground_truth` (see [Reports](#reports)). A sensor
teaches one appliance only.

A **connected appliance** teaches from its operation state instead (add-on
0.14.0+, #79): `{"entity_id": "sensor.dryer_operation_state", "since": ...,
"mode": "state", "on_states": ["run"], "phase_entity"?, "energy_entity"?,
"program_entity"?}`. Nothing is hidden from the analysis; these entities are
read with the other histories and each day keeps the runs of the state,
measured on the house curve (see [Learning from a measured
device](#learning-from-a-measured-device)).

An appliance may also have `examples` (add-on 0.12.0+, #17), at most 20: the
uses labelled in the panel's *Live* tab, see [Labelled examples](#labelled-examples).
They do not change the analysis options (labelling never restarts the live
view).

### Labelled examples

When you label a step of the *Live* tab ("this is the toaster"), the
integration keeps a **labelled example** in the appliance's subentry (so in
every Home Assistant backup) and pushes it with the appliance:

```json
{"t_on": 1800000000.0, "t_off": 1800000130.0, "power_w": 850,
 "source": "live", "created": 1800000200.0, "signature_id": "short-850w"}
```

`t_off` is `null` while the stop is not known (the integration completes it
when the live event stops); `source` is `live` (a line of the feed) or
`guided` (the "I switch an appliance on" mode); `signature_id` is the
signature named at the time; `live` (`{"id", "session"}`) is the live event
while the example is open.

`GET /api/examples` says what the analysed days make of them, for each
appliance with examples:

```json
{"last_day": "2027-03-09", "appliances": [
  {"key": "appliance:toaster", "name": "Toaster", "measured": false,
   "total": 3, "found": 2, "other": 0, "pending": 1,
   "signature": null,
   "link": {"id": "short-850w", "kind": "steady", "band": "short", "power_w": 850,
            "count": 23, "label": null},
   "replace": [], "conflict": null, "disagreement": [],
   "examples": [
     {"t_on": 1800000000.0, "t_off": 1800000130.0, "power_w": 850, "source": "live",
      "status": "found", "reason": null, "signature_id": "short-850w",
      "activation": {"day": "2027-03-09", "start": 1800000001, "end": 1800000128, "power_w": 846}},
     {"t_on": 1800090000.0, "t_off": null, "power_w": 850, "source": "guided",
      "status": "pending", "reason": "day_not_analysed", "signature_id": null, "activation": null}
   ]}
]}
```

1. **Find**: the activation of the day that overlaps the example by at least
   half of the shorter of the two (the rule of the teaching evaluation, #14);
   for an open example, the activation that starts within 30 s of `t_on`. Its
   registry signature is the one the analysis recognises.
2. **Link**: when the appliance has no signature the analysis has seen yet
   (none, or only expected ones), the signature most examples were found as
   is proposed in `link` (with its `count` of past uses), replacing the
   expected ones in `replace`; the integration makes it a source of the
   appliance and records the review's *validate* decision. When that
   signature belongs to another appliance: `conflict` (`{"id", "appliance"}`).
3. **Refine**: once linked, the signature gets the appliance's name as its
   label (as any signature source).
4. **Measure**: each example is `found` (recognised as one of the
   appliance's signatures), `other` (as another signature, `owner` when it
   belongs to another appliance; listed in `disagreement`) or `pending` with
   a `reason`: `day_not_analysed` (today), `day_missing` (no stored summary:
   the analysis failed or the day was pruned), `no_activation`, `too_small`
   (below the step threshold), `not_in_registry`, or `measured` (the
   appliance is measured by a switch or a power sensor: its uses are not in
   the curve). `found` of `total` is the recognition shown on the
   appliance's card.

### Learning from a measured device

`GET /api/teaching` gives, for each appliance with a teacher, what was learned
from the stored days taught by that sensor:

```json
{"teachers": [{"key": "submeter:sensor.oven_plug_power", "name": "Oven",
  "entity_id": "sensor.oven_plug_power", "since": 1800000000, "days": 6,
  "first_day": "2027-03-04", "last_day": "2027-03-09", "uses": 5, "energy_kwh": 7.9,
  "learned": {"kind": "steady", "band": "medium", "power_w": 2400, "duration_s": 2700,
              "uses": 5, "runs": 5, "recognisable": true, "phases": [], ...},
  "signature": {"id": "medium-2400w", "in_registry": true, "linked": false, ...},
  "conflict": null,
  "evaluation": {"uses": 5, "found": 5, "missed": 0, "detections": 5, "correct": 5,
                 "false": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0,
                 "truth_kwh": 7.9, "detected_kwh": 7.6, "energy_ratio": 0.962},
  "per_day": [{"day": "2027-03-04", ...}]}]}
```

- **Runs**: the sensor's readings hold until the next one (`unavailable` is no
  data). The appliance runs from 20 W; pauses under 10 minutes stay in one
  run; runs under a minute or never reaching 50 W are standby noise. A run is
  `multiphase` with heating (800 W or more, at least 5 min), at least 10 min
  between 50 and 800 W (a pump, a motor) and the length of a cycle (30 min to
  4 h); `cycling` when its high level is crossed 6 times or more with a duty
  of at most 85 %; `steady` otherwise.
- **Learned signature**: the most frequent kind; its median power while on and
  duration band (steady), on-time, period and duty (cycling), or the template
  of its most frequent programme (multi-phase). `recognisable` is false below
  the step threshold: the analysis does not track it. `visible` (add-on
  0.17.0+) tells whether the curve can show it at all, from the noise
  measured on the last 14 analysed days (`quality.meters.visible_w`): false
  is "too low for the whole-house curve"; before any report has the figure it
  equals `recognisable`.
- **Signature**: the registry signature it is: the one its uses are matched to
  (same group, power within 12 %), or for a multi-phase one the nearest
  template within the cycle distance. `in_registry: false` when there is none
  yet (it is then evaluated as the expected signature it would be);
  `conflict` names another appliance that already owns it.
- **Evaluation**: the curve-based detections of that signature on the taught
  days (its activations, and for a multi-phase one the candidate cycles within
  the template distance) against the runs. A detection and a run match when
  they overlap by at least half of the shorter; detections outside the
  periods the sensor reported are left out. `precision` = correct / detections,
  `recall` = found / uses.

**Connected appliance** (add-on 0.14.0+, #79): the teacher is an operation
state (`mode: "state"`). Each period in one of `on_states` (pauses under 10
minutes inside it) is a run, measured on the day's curve: the residual load
(the house without the sub-meters and the other switched loads) above its
baseline around the run (the mean of the 20 minutes before and after, without
their highest 20 %). No power step is needed: a slow start or phases of very
different powers are measured as they are; the kind and phases of a run are
then found as for a sensor. With a phase state, each run lists its
`named_phases` with their mean power, and `learned.named_phases` their medians
in cycle order. With a programme state, each run has its `program`, and
`learned.programs` lists them (uses, kind, power, duration, energy and the
template of a multi-phase one); a multi-phase appliance learns the template of
its most used programme (`learned.program`). `program_recorded: false` says the
recorder keeps no history of the programme state (an excluded domain): one
signature is then learned for all programmes. With an energy counter
(`energy_entity`, kWh or Wh), each run has the `counter_kwh` counted from its
start to 20 minutes after its end (a coarse counter reports late); once the
runs have counted at least 1 kWh, `learned.counter_ratio` = counted / measured
and the energy of one use (`learned.energy_kwh`, `cycle_kwh`) is the measured
one times that ratio (`learned.measured_kwh` keeps the curve's). The status
adds `mode`, `on_states`, `phase_entity`, `energy_entity`, `program_entity`
and `program_recorded`.

`POST /api/teaching` with `{"appliance": <key>}` returns the signature to link
to the appliance, `{"signature", "created", "learned", "programs"}`: the registry's own,
or a new expected signature named after the appliance (with the learned
template for a multi-phase one, so the analysis recognises its cycles at
once). `programs` (add-on 0.14.0+): the signatures of the other programmes of a
multi-phase connected appliance (used at least twice, with heating, and whose
template differs from the learned one), `[{"signature", "created"}]`. Errors: `404 not_found` (no appliance with a teacher),
`422 not_learned` (no use seen yet), `422 not_recognisable` (below the step
threshold), `409 conflict` (another appliance owns it; `id`).

### Self-test

After a pass, the newest day it analysed is analysed once more, in memory,
for up to `self_test_devices` measured devices (1 by default): each time with
that device **not subtracted**, so that its power stays hidden in the curve
as if it had no sensor. The candidates are the subtracted sub-meters that
measure (not the estimates of the Energy dashboard); they take turns, the
device whose last test is the oldest first (never tested first, then by
entity id). The histories already read are reused: no extra request to Home
Assistant. The hidden report is never stored, and the registry, the real
report and the statistics never change; a failure only costs the test.

For each test, the device's own readings give its runs that day (as for
[learning from a measured device](#learning-from-a-measured-device)). The
signature to look for is learned from its runs of the day and of its
previous tests, then resolved in the registry, or, since a subtracted device
never reaches the registry, evaluated as the expected signature it would be.
Its detections in the hidden day are compared with the runs (precision,
recall, energy). A signature below the step threshold is not looked for (no
hidden analysis): the device is reported as too low for the curve, or, when
the measured noise of the curve lets it show (`learned.visible`, add-on
0.17.0+), as seen on the curve but too small to be tracked yet.

`GET /api/self_test` answers, over the last 60 days:

```json
{"enabled": true, "devices_per_night": 1, "window_days": 60, "min_uses": 5,
 "last_day": "2027-03-09",
 "devices": [{"entity_id": "sensor.water_heater_power", "name": "Water heater",
   "appliance": null, "state": "ok", "tests": 6, "nights": 6,
   "first_day": "2027-02-01", "last_day": "2027-03-09",
   "uses": 6, "found": 5, "detections": 5, "correct": 5,
   "recall": 0.833, "precision": 1.0, "truth_kwh": 15.1, "detected_kwh": 14.2,
   "energy_ratio": 0.94, "rate": 0.833, "trend": "stable",
   "learned": {"kind": "steady", "band": "long", "power_w": 2500, ...},
   "signature": {"id": "long-2500w", "in_registry": false, ...},
   "history": [{"day": "2027-02-01", "uses": 1, "found": 1, "detections": 1, "correct": 1}]}]}
```

- `state`: `ok` (a success rate), `not_enough_data` (fewer than `min_uses`
  true uses tested: `rate` is null, never 0 %), `not_recognisable` (below the
  step threshold) or `no_use` (the device never ran yet).
- `rate`: the share of the true uses found again (the recall over its tests).
- `trend`: `up`, `down` or `stable`: the recall of the newer half of the tests
  against the older half (10 points or more), null while either half has
  fewer than 3 uses.
- `tests`: nights the hidden day was evaluated; `nights`: all the nights the
  device was picked.

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
                                   "time_share": 0.3, "lift": 3.23, "baseline": 0.4, "used": 0.8,
                                   "matches": 40, "sessions": 41, "days": 20, "days_compared": 20,
                                   "chance": 1e-20, "shifted_chance": 1e-12,
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
  in them, `time_share` the share of the entity's time in them, `lift`
  their ratio, `sessions` the signature's uses grouped when less than 30
  minutes apart, `matches` those spent mostly inside, `chance` the
  probability of that many sessions inside by chance (binomial, with
  `time_share`), `shifted_chance` the same if the sessions followed the clock
  but ignored the entity (each session moved to the same hours of the other
  days; `baseline` is the mean share of those days on which it would be
  inside), and `used` the share of the entity's on periods with a use.
  Suggested when `contained` is at least `review_suggest_score`, at least 5
  sessions are inside on 3 different days, `lift` is at least 2, `used` at
  least 10 %, and both `chance` and `shifted_chance` at most 10⁻⁴ (add-on
  0.15.0+, #82): an entity on half of the time or more, or whose periods
  come back at the same hours every day (a heating or filtration schedule),
  is never suggested on containment.

The edge matches come first, by score, then the containment ones by `lift`; the best 3 are kept. A
suggested entity that belongs to an appliance (one of its sources, or in the
same Home Assistant device as one) has `appliance`: the panel proposes to
merge the signature into it. `suggest.unrecorded_domains` lists the
candidate domains none of whose entities is recorded (recorder exclusions).

### Live view

The panel's *Live* tab shows the house power of the last 15 minutes and every
step as it happens, so that you can say which appliance it was. The add-on
runs the nightly detector on a stream of readings: what you see live is what
the night sees.

It costs nothing while nobody looks. The first `GET /api/live` starts a
`live` thread that reads the configuration of the night (meters, sub-meters,
switched loads), subscribes to the state changes of those entities only, and
replays the last 15 minutes of the recorder history (an appliance switched on
just before is seen). Readings are turned into samples 5 seconds behind the
clock (PV and grid are not reported together). It reconnects when Home
Assistant goes away and fills the gap from the history, ranks the candidates
again when the signature registry changes (nightly update, review decision),
and starts again when the settings or the appliances' sources change. After
**2 minutes without a request** it unsubscribes and frees its buffers. If
processing falls more than **30 seconds behind** it stops, logs it, and
refuses to start again for 5 minutes (`overloaded`). The nightly analysis
runs in its own thread and is never affected. The option `live_view: false`
disables it.

`GET /api/live` takes `since` (the `seq` of the previous answer; none for a
snapshot) and `wait` (seconds, at most 25, the default). It answers as soon
as something is new, or after `wait` with nothing new (same `seq`, empty
lists); it is also the "someone is watching" signal. A `since` from before
the running session (the view restarted, or the add-on did) gets a snapshot:
`reset: true`. `seq` is opaque and only increases, across restarts too.

```json
{
  "seq": 1800000000123, "session": 1800000000101, "available": true, "reset": false,
  "history_s": 900, "threshold_w": 300, "night_threshold_w": null, "time_zone": "Europe/Paris",
  "curve": [[1800000000.0, 2310, 2310], [1800000002.0, 2305, 2305]],
  "events": [
    {
      "id": 7, "seq": 1800000000123, "t_on": 1799999950.0, "delta_w": 2010,
      "state": "confirmed", "running": true, "t_off": null, "off_w": null,
      "duration_s": null, "power_w": 2010, "session": null, "pv_moved": false,
      "confidence": "likely", "steps": [[1799999950.0, 2010]],
      "candidates": [
        {
          "source": "signature", "signature_id": "short-2000w", "label": "Kettle",
          "appliance": "signature:short-2000w", "kind": "steady", "band": "short",
          "power_w": 2000, "score": 0.97, "confidence": "likely",
          "reason": {"power_w": 2000, "distance": 0.04, "usual_hour": true,
                     "per_week": 6.9, "expected": false, "pv_moved": false}
        }
      ]
    }
  ],
  "now": {
    "ts": 1800000002.0, "house_w": 2305, "residual_w": 2305, "grid_w": 2305,
    "pv_w": 0, "battery_w": 0, "submetered_w": 0, "submeters": [],
    "running": [7], "unidentified_w": 295, "pv_quiet": null,
    "switches": [{"entity_id": "switch.water_heater", "name": "Water heater",
                  "appliance": null, "on": false}]
  }
}
```

- `curve`: `[time, house W, residual W]` per sample (the residual is the
  house minus the sub-meters: what the detector looks at); `null` when the
  solar production is unknown. The snapshot holds the last 15 minutes, a
  later answer only the new samples.
- `events`: the lines of the feed. A snapshot holds the events of the last
  15 minutes and those still running (not the cancelled ones); a later answer
  the events that appeared or changed, each whole, with the `seq` of its last
  change: a provisional step confirmed or `cancelled` (a spike: remove the
  line), an off-step that completes it (`t_off`, `off_w`, `duration_s`), a
  new cycle of a `session`, new candidates (the registry changed, or the
  elapsed time ruled a duration band out). The bursts of a thermostat are
  one event each until the fourth regular one; they then become one
  `session` (the earliest event, `confirmed`) and the events of the other
  bursts are `cancelled`, as is each later burst once it has joined the
  session (add-on 0.16.0+). `id` stays the same for the whole
  life of an event and is never reused by the add-on process. `delta_w` is
  negative for an off-step whose switch-on was not seen. `candidates`: the 3
  best, from registry signatures (`signature_id`) or switched loads that
  changed within 30 s (`source: "switch"`, `entity_id`); `confidence` is
  `likely` (score ≥ 0.6), `maybe` (≥ 0.3) or `unknown`.
- `now`: the last sample, the sub-meters (`appliance`: the key of the
  appliance it is a source of), the switched loads and their state, the
  running events (newest first) and the power they do not explain
  (`unidentified_w`). `null` before the first sample.
- `threshold_w`: the step threshold; smaller steps are not seen.
- `night_threshold_w`: the [night threshold](#night-threshold), `null` when it
  does not apply (option empty, or no PV sensor). `now.pv_quiet` tells
  whether it applies now (the solar production of the last minute is below
  20 W; `null` when it is off or unknown). With it, a step is confirmed once
  the minute of solar production after it is known (about a minute later).
  With sub-meters that measure (add-on 0.16.0+), a step is likewise confirmed
  only once none of them can explain it any more (about a minute after it),
  and a step a sub-meter's late report made is `cancelled`.

When the view cannot run: `{"seq", "available": false, "reason"}` with
`reason` `disabled` (option off), `starting` (the first snapshot is not ready
within `wait`: ask again), `overloaded` (with `retry_at`) or `error` (with
`error`: `{"code", "message"}`, codes `ha_error`, `no_grid`, `internal`, and
`retry_at`, 30 s later).

`GET /api/live/stats` answers `{"enabled", "state"` (`idle`, `starting`,
`running`, `overloaded`, `error`, `disabled`), `"error", "retry_at", "seq",
"session", "started_at", "last_request", "entities", "interval_s",
"readings", "readings_per_s", "samples", "steps", "events", "reconnects",
"lag_s", "max_lag_s", "process_ms": {"median", "p99", "max"}` (per processing
round, about once a second), `"buffers": {"curve", "events", "readings"}}`.

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
- Appliances below `step_threshold_w` (or, while the solar panels produce
  nothing, below `night_step_threshold_w`) and loads whose power varies
  continuously (EV chargers, inverter heat pumps) are not recognised well yet.
  With `adaptive_threshold` the smaller uses are found where the curve is
  quiet and listed apart (`small_uses`); only the regular ones (a fridge, a
  freezer) are named and tracked, with `cold_appliances`.
  Multi-phase appliances have a first version: two of them running at once,
  or a low load running next to a cycle, blur their cycles. See the
  [README](https://github.com/sylvaing/ha-energy-mapper#limits) for the full
  list and the roadmap.
