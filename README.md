# Energy Mapper

Identify the appliances of a home from its whole-house power meter (non-intrusive load monitoring, NILM), and bring their consumption into the Home Assistant Energy dashboard step by step.

Work in progress: see the [milestones](https://github.com/sylvaing/ha-energy-mapper/milestones) and [issues](https://github.com/sylvaing/ha-energy-mapper/issues).

## Add-on

The `energy_mapper` add-on analyses the previous day every night and stores a
report (see [its documentation](energy_mapper/DOCS.md)). To install it, add
`https://github.com/sylvaing/ha-energy-mapper` to the repositories of the
add-on store (apps store) of Home Assistant.

## Integration

The `energy_mapper` integration (in `custom_components/`) connects Home
Assistant to the add-on: it is discovered automatically once the add-on runs
(or set up with the add-on address), keeps the analysis settings in its
options, and adds the **Energy Mapper panel** to the sidebar: the daily report
(day picker of Home Assistant with presets, consumption breakdown chart,
energy summary, peaks with what was running, things to check) and the signatures learned across days. The panel follows
the Home Assistant theme (light and dark), language (English, French) and
number and time formats. It creates no entity yet. Install it with [HACS](https://hacs.xyz) as a custom repository
(category *Integration*), or copy `custom_components/energy_mapper` into the
`custom_components` folder of the configuration.

## Development

```sh
python3 -m unittest discover -s tests          # core and add-on (standard library only)
ruff check . && ruff format --check .
pip install -r requirements_test.txt && python -m pytest   # integration (Python 3.14)
```

The panel is a TypeScript/Lit/ECharts project in `frontend/`, bundled into
`custom_components/energy_mapper/frontend/energy-mapper-panel.js` (committed;
the CI checks it is up to date):

```sh
cd frontend && npm ci
npm run typecheck && npm test && npm run build   # or: npm run dev (rebuild on change)
python3 dev/make_sample.py                         # regenerate the synthetic sample data
cd .. && python3 -m http.server 8765               # then open http://localhost:8765/frontend/dev/index.html
```

The dev page mounts the panel with a fake `hass` (light/dark theme, English/
French, number and time formats, add-on states) answering from the synthetic
data of `frontend/dev/sample/`. HA's own elements are not available there: the
panel shows its fallbacks (a native date input instead of HA's date picker).

The add-on service also runs outside Home Assistant, against any instance:

```sh
cd energy_mapper/app
HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  DATA_DIR=/tmp/energy_mapper PORT=8099 python3 main.py
```

*Version française : à venir.*

## License

MIT
