# Changelog

## 0.1.0

- First version: nightly analysis of the previous day, configured from the
  Energy dashboard (manual overrides in the options).
- Backfill of the past days still held by the recorder on first start.
- Reports stored as JSON in `/data/reports` (included in Home Assistant backups).
- Internal HTTP API for the Energy Mapper integration, announced through
  Supervisor discovery.
