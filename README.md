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
options, and serves the reports to the Energy Mapper panel. It creates no
entity yet. Install it with [HACS](https://hacs.xyz) as a custom repository
(category *Integration*), or copy `custom_components/energy_mapper` into the
`custom_components` folder of the configuration.

## Development

```sh
python3 -m unittest discover -s tests          # core and add-on (standard library only)
ruff check . && ruff format --check .
pip install -r requirements_test.txt && python -m pytest   # integration (Python 3.14)
```

The add-on service also runs outside Home Assistant, against any instance:

```sh
cd energy_mapper/app
HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  DATA_DIR=/tmp/energy_mapper PORT=8099 python3 main.py
```

*Version française : à venir.*

## License

MIT
