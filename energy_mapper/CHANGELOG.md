# Changelog

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
