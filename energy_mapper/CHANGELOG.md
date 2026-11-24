# Changelog

## 0.19.0

- House meter (#97): the new option `house_entity` (also in the
  integration's options) takes a meter of the house itself, downstream of
  the battery and the solar inverter. The house curve, its steps and its
  peaks then come from it, and the residual is that meter minus the
  sub-meters: grid, solar and battery sensors read at different instants
  (a cloud integration polled every few seconds) no longer make false steps
  when the battery starts or stops or a cloud passes. They are still read
  for the day totals and the night threshold. The late solar readings are
  not set aside with such a meter, and `slow_solar_meter` is not reported.
  The live view follows the same meter. A sensor missing from Home
  Assistant is reported as unavailable, and the analysis goes back to grid
  + solar + battery. Off by default: nothing changes without it.
- Integration 0.16.0: the *House power* field in the options, English and
  French.

## 0.18.2

- Labelled examples: "too small" is decided from the measured noise of the
  curve (#100), as learning from a measured device and the self-test do
  since 0.17.0. An example the analysis did not find and whose power is
  under the step threshold gets the new reason `small_untracked` in
  `GET /api/examples` when its power reaches the smallest step the curve can
  show (`quality.meters.visible_w` over the last 14 analysed days): seen on
  the whole-house curve, too small to be tracked yet. Under it, and while no
  report has the figure, it stays `too_small`.
- With `adaptive_threshold`, such an example is also looked for in the day's
  small uses, with the overlap rule of the activations: when one is the
  example, the reason is `small_untracked` and the item carries it in the
  new field `small_use`. The example stays pending: small uses are not
  tracked.
- Integration 0.15.1: the appliance's card tells the two cases of its
  labelled examples apart, English and French.

## 0.18.1

- Adaptive threshold: the low steps a switched load's transition makes are
  explained by that load (#99). A load under the step threshold behind a
  configured switch (a relay driving a 100 W pump, a towel rail on its low
  setting) left a low step each time it switched, and those steps could be
  paired into a small use, or learned as a cold appliance, although the
  installation already knew what they were. They are now matched to the
  switch's transitions with the rule of the other steps (within 30 s, the
  right sign, a size that agrees with the load's power, either polarity) and
  left out of the small uses and of the cold appliances.
- The switched load itself is unchanged: its power and its energy are still
  measured from the steps of the fixed thresholds only (a load seen only in
  the low steps stays at 0 W). No report field changes, and with
  `adaptive_threshold` off the reports are the ones of 0.18.0.

## 0.18.0

- Cold appliances (#89): fridges and freezers, behind the new option
  `cold_appliances`, **off by default** (experimental; it needs
  `adaptive_threshold` and does nothing without it). The low steps of the
  adaptive threshold that come back with the same sizes, the same run length
  and a regular pause are learned in the quiet hours (no solar production,
  no larger appliance switching; at least 5 runs) as one appliance: its
  on-step, its off-step (a compressor starts higher than it stops, which is
  what tells two such appliances apart), its usual run lengths and its usual
  pause.
- The appliance is then followed through the whole day with that model: a
  detection that spanned two runs is split, a run with one step missing is
  completed (the other edge is looked for in the curve where the run length
  says it is due), one or two runs missing between two others are filled.
  Where more is missing (busy hours) no run is reported: the energy is
  **estimated** from the duty learned in the quiet hours and given apart
  (`estimated_kwh`), an estimate, not a detection.
- A new kind of signature, `cold` (`cold-80w`), in the daily report
  (`cold`: the model, the runs with how each was found, the estimated spans,
  the energy) and in the registry, which keeps the model from day to day: on
  a day whose night taught nothing the appliance is still followed, and one
  that is gone is no longer reported. It is reviewed, named and counted in
  the hourly statistics like the others; its energy leaves the unexplained
  share of the day. Only these regular groups reach the review: the other
  small uses stay where they were.
- With either option off, the reports are the ones of 0.17.0. The live view
  is unchanged (cold appliances are a nightly analysis).
- Integration 0.15.0: *Cold appliances (fridge, freezer)* in the options
  (*Thresholds*), English and French; the panel names such a signature
  ("Runs in cycles all day, about 80 W, every 43 min"), shows its cycle in
  the review and says which part of its energy is estimated.
- `tools/benchmark.py --cold`: the days are analysed in order with the models
  learned before each, and a hidden device gets the score of the cold
  appliance its absence adds: runs found and right, by night and by day, and
  daily energy against the device's own meter.

## 0.17.0

- Step threshold adapted to the noise of the curve (#88), behind the new
  option `adaptive_threshold`, **off by default** (experimental). The fixed
  thresholds were chosen for the noisiest hour of the curve and apply at
  3 a.m. too, where a freezer's 80 W steps are twenty times the noise and
  were never found. With the option, the noise is measured along the curve
  (the robust spread, over the last 30 minutes, of the changes the step
  detector looks at) and a change smaller than the fixed thresholds is also
  kept when it reaches `max(40 W, 4 × noise)`: about 40 W in a quiet night,
  100 to 200 W under a changing sky, nothing where the curve is noisier.
- Such **small uses** are kept apart: listed in the daily report
  (`small_uses`: `start`, `end`, `power_w`, `on_w`, `off_w`), never in the
  activations, the signatures, the registry, the review, the breakdown of the
  curve nor the statistics. Everything else is found exactly as without the
  option (same steps, activations, signatures and energies), and the night
  threshold still decides which uses are normal ones. Grouping the small uses
  that come back regularly (a fridge, a freezer) and proposing those is the
  next step (#89).
- The live view applies the same rule and gives the same steps as the night;
  its feed does not show the small uses.
- "Too low for the whole-house curve" is now decided from the measured noise,
  not from the thresholds. Every report carries the meters' quality
  (`quality.meters`: reporting intervals of the grid and solar meters, noise
  of the curve apart from and during the solar hours, and `visible_w`, the
  smallest step the curve can show), whatever the option. Learning from a
  measured device and the self-test add `visible` to the learned signature:
  an appliance under the thresholds but above the noise is "seen on the
  whole-house curve, too small to be tracked yet" (integration 0.14.0 for
  the text); it is still not taught nor tested, since small uses are not
  tracked.
- Integration 0.14.0: *Threshold adapted to the noise of the curve* in the
  options (*Thresholds*), English and French.
- `tools/benchmark.py`: the meters' quality comes from the engine
  (`nilm_core/quality.py`); `--adaptive`; and a second score that needs no
  signature registry (a use of a hidden device is found when uses of similar
  power cover half of it), overall and apart from / during the solar hours,
  in the table, the JSON result and `--compare`.
- On the reference installation (8 days), with the option: 52 % of a hidden
  freezer's 110 cycles are found (56 % without solar production, 45 % with)
  and 82 % of the detections are right (92 % and 50 %); 42 small uses a day
  are listed; signatures, steps, energy shares and the other devices' scores
  are unchanged.

## 0.16.0

- The regular bursts of a thermostat are one cycling use (#87). A heater
  regulating on its own thermostat switches on for some tens of seconds every
  few minutes: its steps are too far apart for a cycling session, so each burst
  was a short use of its own (a "40 s appliance" used hundreds of times a
  month, hiding the appliances worth naming in the review). After the pairing,
  short uses (under 5 minutes) and cycling sessions of at least 600 W and of
  the same power (within 20 %) are grouped when they come back regularly: bursts
  less than 2 minutes apart are one block (a radiator bursting while its
  pilot-wire relay allows it), a block starts at most 15 minutes after the
  previous one and within 25 % of the usual period, and a series needs 4
  blocks (it may then miss one). Two kettles twenty minutes apart, a microwave
  used several times around a meal, hob sessions and the heating phases of
  multi-phase cycles are unchanged. The series is a cycling activation (power,
  duty, on-time, period) of the `cyc-…` signature, with `blocks`: when it was
  on.
- A cycling use is counted in the minutes it is on (its own on/off pattern)
  instead of its mean power all along: the curve and the hourly statistics of
  a thermostat, and of a hob, are exact. The energy of a use is unchanged.
- Entity suggestions compare each block of a series, so a relay or a
  thermostat that follows the bursts is still found.
- Live tab: a thermostat is one line per burst until its fourth block, then
  one session line (the lines of its bursts leave the feed; each new burst
  shows while it is on, then joins the session). A short use of at least 600 W
  is also compared with the cycling signatures.
- Solar: late readings no longer make false steps in the daytime (#86). The
  house load is grid + solar, two meters that do not report together: when a
  cloud passes, the grid reading moves at once and the solar reading comes
  later, so a step appeared, then cancelled itself (about half of the daytime
  steps of 300 to 600 W on the reference installation). With a PV meter, the
  samples whose solar value is about to move (by half the step threshold or
  more, within 60 s) are now set aside before the step detection, which gets
  the level before the doubt instead (`steps.SolarHold`): a cloud gives no
  step, and an appliance that switched meanwhile gives one step with its real
  size, up to a minute late. No setting; nothing changes without a PV meter
  nor while the panels produce nothing, and the curve and the energies are
  unchanged.
- The live view applies the same rule (same steps as the night): while the
  panels produce, the steps come once the solar value has changed, a minute
  later at most.
- New finding `slow_solar_meter` (*Things to check*, integration 0.13.0 for
  the translated text): the solar power sensor takes more than 60 s to report
  a change of the production, so clouds cannot be told from appliances in the
  daytime. The report's `quality` gets `solar_interval_s` and `solar_held`
  (only with a PV meter).
- Sub-meters that do not report with the grid meter no longer leave false
  steps (#85). A sub-meter is subtracted reading by reading, but a smart plug
  announces a change tens of seconds before or after the grid meter, or only
  every 40 s, and an appliance that modulates every few seconds is read at
  different instants by its plug and by the grid meter: the subtraction left
  pulses and holes of the appliance's power, so phantom steps, activations
  and signatures. The energy (curve, totals) keeps the subtraction; the
  steps it leaves are now marked as explained by the sub-meter and ignored
  by the pairing: a step where the sub-meter's own reading changes the other
  way while the house does not follow (its report), the house's step of the
  same size within 45 s of it, and the steps around a sub-meter that
  modulates. An installation whose sub-meters report with the grid meter is
  analysed exactly as before; estimated sub-meters explain nothing; a device
  hidden by the self-test keeps its steps. The report's `quality` gains
  `submeters`: per measured sub-meter, the steps explained and the measured
  lag of its reports (`lag_s`, `lag_min_s`, `lag_max_s`); `quality.steps`
  leaves the explained steps out. Live tab: with measured sub-meters a step
  is confirmed once none of them can explain it any more (about a minute
  after it, as with the night threshold), and a step made by a late report
  is cancelled; the live steps stay those of the night.
- Learn from a connected appliance (integration 0.13.0): an alert or event
  state (all its options among `off`, `on`, `present`, `confirmed`, `none`,
  `true`, `false`: "salt nearly empty", "programme finished"...) is no longer
  proposed as the phase or the programme; it stays listed for a manual
  choice. Between two programmes, the active one wins over the one selected
  for the next start (Home Connect `active_program` / `selected_program`).

## 0.15.0

- Containment suggestions require specificity, not just inclusion (#82). In
  winter, thermostats of water radiators, heating "comfort" sensors, a pool
  pump's schedule or a cover were suggested for any daytime signature: they
  contained its uses because both follow the clock. A containment candidate
  now also needs `lift` (`contained / time_share`) of at least 2 (an entity
  on half of the day or more is never suggested) and `shifted_chance` of at
  most 10⁻⁴: the chance of that many sessions inside if each session were
  moved to the same hours of the other compared days (Poisson-binomial,
  `baseline` the mean of those probabilities). `used` is kept but its
  threshold is lowered from 0.3 to 0.1 (a radiator whose relay is a switched
  load leaves only some of its cycles to its signature). Containment
  candidates are ranked by `lift`. Known limit: an entity on at the same
  hours every day cannot be told from a coincidence by containment.

## 0.14.0

- Connected appliances as ground truth (#79). An appliance can learn from its
  operation state (a `sensor` such as `run` / `stop`) instead of a power
  sensor: teacher `{"mode": "state", "on_states", "phase_entity"?,
  "energy_entity"?, "program_entity"?}`. Each run of the state is measured on
  the house curve (residual load above its baseline around the run, tolerant to
  a gradual start), classified like a measured run (steady, cycling,
  multi-phase), its phases named by the phase state, its programme noted, and
  its energy checked and scaled against a coarse energy counter once the runs
  counted 1 kWh. A multi-phase appliance learns one template per recorded
  programme; `POST /api/teaching` returns the other programmes' signatures in
  `programs`. The report's `ground_truth` and `GET /api/teaching` carry the new
  fields (`mode`, `program_recorded`, `named_phases`, `counter_kwh`,
  `counter_ratio`, `programs`...).
- Switched loads driven by an operation state (a `sensor` entity) are measured
  over each run (`known_loads[].mode: "run"`, `runs`) instead of from the power
  step at their transitions: a ramping start or a multi-phase cycle now gets
  its energy. Such a load marks no step and claims no power from the
  multi-phase candidates (its own signature can still be learned); it takes the
  room first in the curve breakdown. Relays (`switch`, `input_boolean`...)
  are unchanged.
- Flat power sensors: a device of the Energy dashboard whose power sensor stays
  constant all day while its energy counter increases is `energy_only` that
  day (not subtracted, not used by the self-test), with the new finding
  `flat_power_sensor` (`{name, power_entity, power_w, energy_kwh}`).
- Integration (0.12.0): websocket `energy_mapper/connected_devices`
  (administrators: the devices with an operation state and the entities
  proposed for each, with whether the recorder keeps them) and
  `energy_mapper/teach` with `mode: "state"` (on an appliance, or a new one
  named `name`, optionally tracked by its state); *Finish* keeps the state as a
  source and adds the learned signatures. The panel's *Appliances* tab gets
  *Learn from a connected appliance*, the learning card its energy, phases and
  programmes, and the finding *Power sensor stuck*.

## 0.13.0

- Night threshold (#77): the new option `night_step_threshold_w` (empty by
  default: off) lowers the step threshold only while the solar panels produce
  nothing. A step of at least `step_threshold_w` is kept as before; a smaller
  one, of at least `night_step_threshold_w`, only when the PV production is
  known and its median stays below 20 W in the minute before and after the
  step. It removes the false midday steps that come from the PV and grid
  meters drifting apart, and still reveals small evening appliances. No
  effect without a PV sensor; between 50 W and `step_threshold_w`, checked by
  the settings (`PUT /api/settings`).
- The nightly analysis (`detect_steps`, `report.find_steps`) and the live view
  (`StepStream`, `LiveDetector`) apply the same rule, so a replayed day still
  gives exactly the steps of the night. With the option, a live step is
  confirmed once the minute of PV after it is known.
- `GET /api/live`: `night_threshold_w` (null when off) and `now.pv_quiet`
  (whether it applies now), for the panel's guided mode ("too weak").
- Teaching, self-test and labelled examples call an appliance "too low for
  the whole-house curve" below the smallest threshold that applies (the night
  one when it applies).

## 0.12.0

- Live view, core (#17, part 1 of 4): the engine can now run on a stream of
  readings, for the coming *Live* tab of the panel. Nothing changes for the
  user yet: no new API, option or behaviour.
- `nilm_core/signal.py`: `SampleBuilder` builds the samples of the night one
  reading at a time (same hold rules for PV, battery and sub-meters, shared
  through `sample_at`), up to a time bound so that meters reported at
  different times are combined as the night combines them.
- `nilm_core/steps.py`: `StepStream` is the step detector of the night,
  streamed. Fed with the same points and meter interval, its confirmed steps
  are exactly those of `detect_steps`; a provisional step comes after 3
  readings (or 10 s), then is confirmed or cancelled (a spike).
- `nilm_core/live.py`: live events (a switch-on completed by its off-step with
  the night's pairing rule, alternating steps grouped into a session with the
  night's cycling rule, an off-step whose switch-on was not seen), the 3 best
  candidates of each event among the registry signatures and the switches that
  just changed, with a confidence score from 0 to 1, a "solar moved at the
  same time" flag, and `replay`, which streams a stored day.
- `Matcher.near` lists the registry signatures near a power, with the
  registry's tolerance, for the candidates.
- Live view, on-demand service (#17, part 2 of 4): `GET /api/live?since=<seq>`
  is a long-poll (25 s at most) with sequence numbers: a snapshot of the last
  15 minutes (curve, events, "now" summary), then only the new samples and
  the events that appeared or changed. `GET /api/live/stats` gives the state,
  readings per second, processing times (median, 99th percentile), lag and
  buffer sizes.
- The first request starts a `live` thread: it subscribes to the state
  changes of the analysed meters and switched loads only
  (`subscribe_entities` over the add-on's websocket client), replays the last
  15 minutes of history, reconnects and fills the gap after an outage, ranks
  the candidates again after a registry update or review decision, and
  starts again when the settings or the appliances' sources change. It stops
  after 2 minutes without a request (no cost when nobody looks) and, for 5
  minutes, when processing falls more than 30 s behind (self-protection). The
  nightly analysis is never affected.
- New option `live_view` (on by default): the kill switch of the live view.
- The websocket client waits for a message with a timeout without losing a
  frame received in part.
- Labelled examples (#17, part 3 of 4): an appliance pushed with `examples`
  (at most 20: `{"t_on", "t_off" or null, "power_w", "source": "live" |
  "guided", "created", "signature_id"?, "live"?}`) is a small ground truth.
  `GET /api/examples` finds each example again in the analysed days (the
  activation that overlaps it by half of the shorter, or that starts within
  30 s of an open one), proposes the signature most examples were found as
  for an appliance that has none seen yet (`link`, replacing its expected
  ones), reports a signature of another appliance (`conflict`) and
  contradicting examples (`disagreement`), and counts `found` of `total`,
  with the reason of each pending example.
- `GET /api/live/event?id=&session=`: one event of the running live session,
  to label it (404 `not_found`, 409 `session_ended`).
- Integration (0.10.0): websocket `energy_mapper/live/label` (administrators)
  labels a live event or a measured use: names an unnamed signature (the
  review's validate decision, creating the appliance), adds an example to an
  appliance (merging an unnamed signature into it), creates a new appliance
  tracked in the curve (its expected signature at once when the stop is
  known, else when the stop arrives or at night), by a switch, a power sensor
  or a teaching sensor, or never proposes a signature again. The examples are
  stored in the appliances' subentries (so in every backup), follow merges,
  and the night's `link` is applied automatically. `energy_mapper/appliances`
  shows each appliance's examples and what the night found of them.
- Panel: the *Live* tab (#17, part 4 of 4), for every user, after *To review*,
  with a pulsing dot while it streams. It subscribes only while it is shown
  and the page is visible. The house curve of the last 15 minutes with a
  marker on each switch-on ("?" to label, "✓" known; select one to label
  it); *Now*: the current power, what probably runs, the plugs and switches
  on, the unidentified rest and a split bar; *What just happened*: one line
  per event, completed by its stop, with its best candidate, a tag (new, seen
  before, known, labelled by you), the reasons of the candidates in words and
  the filter *To label*. Administrators label a line inline (candidates with
  score bars, a name with frequent names for that power, how to track it,
  the effect before and after saving), hide it (*Ignore*) or never see that
  signature again (*⋯*), and use the guided mode *I'm switching an appliance
  on* (two steps at once, nothing detected, too weak for the curve). Every
  status of the stream has its message. English and French, light and dark,
  from 360 px wide, keyboard accessible. The dev harness simulates a live
  stream.

## 0.11.0

- Findings (#34): each finding keeps its stable `kind` and gets structured
  `params` (the device `name`, the `source` of a missing power sensor:
  `grid`, `solar` or `device`, the `declared_kwh` and `measured_kwh` of a
  tracking gap), so the panel (integration 0.9.0) writes it in the user's
  language. `detail` stays, in English, as the fallback for older panels.
- Learn an appliance from a measured device (#14): an appliance pushed with a
  `teacher` (`{"entity_id": <power sensor>, "since"}`, a temporary smart plug)
  is learned from it. The sensor is never subtracted while it teaches (the
  appliance stays in the whole-house curve, as after the plug is gone; an
  Energy dashboard device gets the role `teacher`), and each analysed day keeps
  its runs in a new report field `ground_truth`: start, end, energy, power,
  kind (`steady`, `cycling` or `multiphase`, with the phases) and the periods
  the sensor reported.
- From these runs the add-on learns one signature of the existing kinds
  (steady power and duration band, cycling on-time and period, or the phase
  template of the most frequent programme), finds the registry signature it is
  and measures the recognition from the curve against the plug over the
  learning days: precision, recall, F1 and energy ratio, per day and in all.
  New `GET /api/teaching` (status) and `POST /api/teaching` (the signature to
  link, created as an expected signature when the registry has none; a
  multi-phase one with the learned template, so its cycles are recognised at
  once).
- The ground truth and the evaluation are a pure module of the engine
  (`nilm_core/groundtruth.py`), for the nightly self-test (#15) too.
- Nightly self-test (#15): after a pass, the newest day analysed is analysed
  again in memory with one measured device (a sub-meter that measures, not an
  estimate) **not subtracted**, from the histories already read. Its own
  readings are the truth; the signature learned from them is looked for in
  the hidden day (precision, recall, energy). The devices take turns (the
  oldest test first). The real report, the registry and the statistics never
  change; a failure only costs the test. Results in `/data/self_test.json`
  (60 days), summarised by the new `GET /api/self_test`: per device the
  success rate (the share of the true uses found again, from 5 uses; "not
  enough data" before, never 0 %), precision, energy ratio and trend.
- New setting and option `self_test_devices` (0 to 10, default 1; 0 disables
  the self-test).

## 0.10.0

- Settings (#33): `submeters_mode` `manual` makes `submeters` the exact list
  of sub-meters, possibly empty: "no sub-meters" now subtracts nothing
  instead of falling back to the Energy dashboard (it also overrides the
  add-on option). A `submeters` list alone is stored with the mode.
- Settings (#33): new `excluded_submeters`, automatic sub-meters (Energy
  dashboard or add-on option) that are not subtracted; their power stays in
  the residual and the tracked device gets the role `excluded`. Not accepted
  together with manual sub-meters.
- Integration 0.8.0 uses them: the options show which sub-meters and switched
  loads come from the Energy dashboard and which are user changes, and
  removing an automatic one from a list excludes it.
- Multi-phase appliances, first version (#26): dishwashers, washing machines
  and tumble dryers. Around each heating-like activation (steady, at least
  1 kW, 5 min to 2 h), the minutes where the load stays at least 50 W above
  the base (a local threshold far below the step threshold) are grouped into
  a candidate cycle and split into phases (heating, motor). Reports list them
  in a new `cycles` field.
- Signature registry: recurring candidate cycles (at least 3, clustered on a
  distance between their phase sequences) become signatures of kind
  `multiphase` (band `cycle`, id `cycle-<heating power>w`) with their
  template in `phases` and the typical energy of one cycle in `cycle_kwh`.
  They are proposed in the review like the other signatures.
- Once such a signature belongs to an appliance (named, or validated in the
  review), the nightly analysis recognises its cycles: each one becomes one
  activation (`multiphase: true`, its phases, its exact per-minute power)
  that replaces the activations inside it (its heating phases), so its
  energy, low-power phases included, is counted once. Before that, nothing
  changes in the attribution.
- An appliance can declare a `multiphase` signature as a source.

## 0.9.0

- Signature registry: each signature keeps its `occurrences` of the last 60
  days, `[start, duration_s, power_w]` (unix time, seconds, watts, integers),
  sorted by start, at most 600 (the most recent ones). They feed the day
  raster of the To review tab of the panel (#65) and come with each
  signature in `GET /api/review` and `GET /api/signatures`. Merged clusters
  union their occurrences; a signature carried forward without stored
  activations keeps its own, pruned to the window.
- A registry written by an older version is updated at the next start,
  from the stored day summaries (about 200 days are kept), so the existing
  signatures get their occurrences at once.

## 0.8.1

- Reports: new `estimated`, the part of `submetered` that comes from
  estimated devices (an estimation integration's power sensor driven by a
  light or another entity), in the curve points, the day totals and the peaks
  (part `estimated`). They are still subtracted before the analysis like the
  sub-meters; the panel shows them apart (#44).

## 0.8.0

- Entity suggestions compare attributes too (#43): `hvac_action` of climate
  entities and `action` of humidifiers, read as if they were states (a
  thermostat stays `heat` all winter; `hvac_action` tells when it heats).
  Their history is read in a separate, bounded request with attributes.
- Containment scoring (#43): an entity that stays in a state for long
  periods while the load cycles inside them is suggested when the signature
  only runs in that state (`match: "containment"`: share of its on-time
  inside, sessions inside, chance level, share of the entity's on periods
  used). Random entities and entities "on" most of the time stay rejected.
- The switched loads and the entities linked to an appliance are candidates:
  a suggestion that belongs to an appliance (one of its sources, or the same
  Home Assistant device) carries `appliance`, and the panel of the
  integration 0.6.0 proposes to merge the signature into it.
- Changes files version 2 (with the state at midnight and the attributes);
  the files of the last 30 days written before are read again once.
- `GET /api/review`: `attribute_candidates` in `suggest`; suggestions have
  `match`, `attribute` and `appliance`.

## 0.7.0

- Entity suggestions for the signatures to review (#41), with the integration
  0.5.0: every night the signatures proposed (and waiting) are compared with
  the state changes of the Home Assistant entities that may tell when an
  appliance runs (lights, switches, input booleans, fans, climate, water
  heaters, media players, binary sensors, `enum` sensors). An entity whose
  state changes at 80 % of a signature's starts and stops (at least 5 times on
  3 days), and whose own changes mostly happen then, is suggested, with the
  states in which the appliance runs. `GET /api/review` gives `suggestions`
  per signature and `suggest` (with the candidate domains that are not
  recorded).
- The changes of the candidate entities are read once per day from the
  recorder and kept 31 days in `/data/changes`; the suggestions in
  `/data/suggestions.json`.
- New setting `review_suggest_score` (80).
- History requests for many entities are split, so that a request stays
  short.

## 0.6.0

- Review of the signatures in the Energy Mapper panel (#10), with the
  integration 0.4.0: new endpoints `GET /api/review` (the signatures seen
  often enough to be an appliance, waiting and ignored, with their
  `review_state`) and `POST /api/review` (`validate`, `wait`, `ignore`,
  `restore`). Decisions are stored in the registry (`review` of a signature)
  and follow its id; a waiting signature comes back after twice as many uses
  or 14 days; an ignored one is kept after it fades out (the 100 most recent)
  so that it is not proposed again.
- New settings `review_min_uses` (5), `review_min_days` (3),
  `review_wait_days` (14) and `review_wait_factor` (2).

## 0.5.0

- The user's appliances, managed in Home Assistant by the Energy Mapper
  integration 0.3.0 (one config subentry each, #11): new endpoints
  `GET /api/appliances` and `PUT /api/appliances`, stored in
  `/data/appliances.json`. Signature sources give their signature its name
  (`label`, and the appliance's key as `appliance`; labels no appliance
  declares are cleared, and a declared signature lost from the registry is
  recreated); switched-load sources are analysed as switched loads named after
  the appliance; sub-meter sources are subtracted before the analysis.
- New endpoint `POST /api/signatures`: an appliance declared before it is seen
  ("Kettle, 2000 W"): an *expected* signature, named and never forgotten, whose
  id is taken by the first cluster of its group within 12 % of its power.
- New setting `excluded_switched_loads` (#33): switches of the Energy dashboard
  that are not measured as switched loads (their estimate is subtracted like
  any other). The integration adds the switch of a deleted appliance.
- Tracking gaps compare a switched load by entity, so a renamed one is still
  checked.

## 0.4.1

- Automatic configuration: Energy Mapper's own statistics
  (`energy_mapper:load_…` / `energy_mapper:signature_…`) in the Energy
  dashboard are recognised (role `energy_mapper`). An appliance whose
  estimate was replaced by its Energy Mapper statistic is no longer reported
  as unavailable, is never subtracted, and its relay stays a switched load
  with its learned polarity, without manual settings (#36). The mapping
  statistic id -> appliance is kept in `/data/statistic_ids.json`; a
  statistic the add-on cannot map gets the finding `unknown_statistic`.

## 0.4.0

- New endpoint `GET /api/hourly/YYYY-MM-DD`: the energy of each appliance per
  UTC hour of a day (switched loads by entity id, signatures by registry id),
  computed from the report's per-minute curve. The Energy Mapper integration
  0.2.0 imports it into Home Assistant's long-term statistics.
- `GET /api/days` also answers `generated`: when each full report was made,
  so the integration sees new and re-analysed days.

## 0.3.0

- Settings pushed by the Energy Mapper integration: new endpoints
  `GET /api/settings` and `PUT /api/settings`, stored in
  `/data/settings.json`. They take precedence over the add-on options; with
  no settings pushed, the add-on options work as before.

## 0.2.0

- Signature registry learned across days, with progressive forgetting
  (`half_life_days`, 30 by default) and stable signature ids; new endpoint
  `GET /api/signatures`; daily reports served with registry ids.
- Switched loads: learned power and polarity kept across days.
- Peaks: every report lists the day's top episodes (relative to the day's
  maximum) and flags as `notable` those above 80 % of the maximum of the last
  `peak_window_days` days (or above `peak_threshold_w` when it is set).
- Bounded storage: full reports gzip-compressed and kept
  `report_retention_days` (90) days, day summaries kept while they still
  count; existing reports are compressed on start.

## 0.1.0

- First version: nightly analysis of the previous day, configured from the
  Energy dashboard (manual overrides in the options).
- Backfill of the past days still held by the recorder on first start.
- Reports stored as JSON in `/data/reports` (included in Home Assistant backups).
- Internal HTTP API for the Energy Mapper integration, announced through
  Supervisor discovery.
