# Energy Mapper

Identify the appliances of a home from its whole-house power meter (non-intrusive load monitoring, NILM), and bring their consumption into the Home Assistant Energy dashboard step by step.

Work in progress: see the [milestones](https://github.com/sylvaing/ha-energy-mapper/milestones) and [issues](https://github.com/sylvaing/ha-energy-mapper/issues).

## Add-on

The `energy_mapper` add-on analyses the previous day every night and stores a
report (see [its documentation](energy_mapper/DOCS.md)). To install it, add
`https://github.com/sylvaing/ha-energy-mapper` to the repositories of the
add-on store (apps store) of Home Assistant.

## Development

```sh
python3 -m unittest discover -s tests
ruff check . && ruff format --check .
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
