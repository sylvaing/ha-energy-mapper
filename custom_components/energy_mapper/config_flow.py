"""Config flow (Supervisor discovery or manual address) and options flow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    ObjectSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import EnergyMapperApiError, EnergyMapperClient, EnergyMapperConnectionError
from .appliances import (
    KIND_LOAD,
    KIND_SUBMETER,
    SUBENTRY_TYPE,
    Appliance,
    Source,
    link_text,
    new_appliance,
    relay,
)
from .appliances import signature as signature_source
from .const import (
    ADAPTIVE_THRESHOLD,
    ADDON_PORT,
    ADDON_SLUG,
    ANALYSIS_KEYS,
    BOOL_KEYS,
    COLD_APPLIANCES,
    CONF_DISCOVERED,
    CONF_URL,
    DOMAIN,
    INVERT_KEYS,
    OPT_REVIEW_REPAIR,
    OPT_SETTINGS,
    REVIEW_KEYS,
    SENSOR_KEYS,
    SWITCH_DOMAINS,
    THRESHOLD_KEYS,
)
from .coordinator import EnergyMapperConfigEntry

_LOGGER = logging.getLogger(__name__)

TITLE = "Energy Mapper"
INT_KEYS = (
    "backfill_days",
    "half_life_days",
    "report_retention_days",
    "peak_window_days",
    "review_min_uses",
    "review_min_days",
    "review_wait_days",
    "review_suggest_score",
    "self_test_devices",
)


def _normalize_url(url: str) -> str | None:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url


def addon_url(slugs: list[str]) -> str | None:
    """Address of the Energy Mapper add-on among the slugs of the installed
    add-ons, whatever repository it comes from (#32)."""
    for slug in slugs:
        if slug == ADDON_SLUG or slug.endswith(f"_{ADDON_SLUG}"):
            return f"http://{slug.replace('_', '-')}:{ADDON_PORT}"
    return None


async def _installed_addon_url(hass) -> str | None:
    """The address of the installed add-on, asked to the Supervisor; None
    without a Supervisor (standalone container) or when it cannot tell."""
    if not is_hassio(hass):
        return None
    try:
        from homeassistant.components.hassio import get_supervisor_client

        addons = await get_supervisor_client(hass).addons.list()
    except Exception as err:  # only a suggestion: never fail the form
        _LOGGER.debug("Cannot list the add-ons: %s", err)
        return None
    return addon_url([a.slug for a in addons])


async def _validate(hass, url: str) -> dict[str, str]:
    """Errors of the form (empty when the add-on answers)."""
    try:
        await EnergyMapperClient(async_get_clientsession(hass), url).health()
    except EnergyMapperConnectionError:
        return {"base": "cannot_connect"}
    except EnergyMapperApiError:
        return {"base": "invalid_response"}
    return {}


class EnergyMapperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the connection to the add-on (single instance)."""

    VERSION = 1
    # 1.2: one config subentry per appliance (migrated in __init__.py)
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._url: str | None = None
        self._addon: str = TITLE

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: EnergyMapperConfigEntry) -> EnergyMapperOptionsFlow:
        return EnergyMapperOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE: ApplianceSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual address (an add-on not discovered, or a standalone container)."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            else:
                errors = await _validate(self.hass, url)
                if not errors:
                    return self.async_create_entry(title=TITLE, data={CONF_URL: url, CONF_DISCOVERED: False})
        if user_input is None:
            # the installed add-on when the Supervisor knows it, else empty
            found = await _installed_addon_url(self.hass)
            suggested = {CONF_URL: found} if found else {}
        else:
            suggested = user_input
        schema = vol.Schema(
            {vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL))}
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The add-on announced itself through the Supervisor."""
        config = discovery_info.config
        url = f"http://{config['host']}:{config['port']}"
        await self.async_set_unique_id(DOMAIN)
        current = self._async_current_entries(include_ignore=False)
        if current and current[0].data.get(CONF_DISCOVERED):
            # the add-on may come back under another host name: follow it
            self._abort_if_unique_id_configured(updates={CONF_URL: url})
        # a manual address is the user's choice: keep it
        self._abort_if_unique_id_configured()
        self._url = url
        self._addon = discovery_info.name or TITLE
        self.context["title_placeholders"] = {"addon": self._addon}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._url is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate(self.hass, self._url)
            if not errors:
                return self.async_create_entry(title=TITLE, data={CONF_URL: self._url, CONF_DISCOVERED: True})
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"addon": self._addon},
            errors=errors,
        )


# ---------------------------------------------------------------------------
# options: the settings of the add-on, kept in the entry and pushed to it
# ---------------------------------------------------------------------------
def _power_sensor(multiple: bool = False) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power", multiple=multiple))


def _number(low: float, high: float, step: float = 1, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


# sub-meters (#33): from the Energy dashboard minus those removed, the user's
# own list, or none at all
SUBMETERS_MODE = "submeters_mode"
MODE_AUTOMATIC, MODE_MANUAL, MODE_NONE = "automatic", "manual", "none"
SUBMETERS_MODES = [MODE_AUTOMATIC, MODE_MANUAL, MODE_NONE]

SWITCHED_LOADS = ObjectSelector(
    ObjectSelectorConfig(
        multiple=True,
        label_field="entity_id",
        description_field="name",
        translation_key="switched_loads",
        fields={
            "entity_id": {"required": True, "selector": {"entity": {"domain": SWITCH_DOMAINS}}},
            "name": {"selector": {"text": {}}},
            "on_states": {"selector": {"text": {"multiple": True}}},
        },
    )
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required("sensors"): section(
            vol.Schema(
                {
                    vol.Optional("grid_entity"): _power_sensor(),
                    vol.Optional("grid_invert", default=False): BooleanSelector(),
                    vol.Optional("grid_export_entity"): _power_sensor(),
                    vol.Optional("pv_entity"): _power_sensor(),
                    vol.Optional("battery_entity"): _power_sensor(),
                    vol.Optional("battery_invert", default=False): BooleanSelector(),
                    vol.Optional(SUBMETERS_MODE, default=MODE_AUTOMATIC): SelectSelector(
                        SelectSelectorConfig(
                            options=SUBMETERS_MODES,
                            translation_key=SUBMETERS_MODE,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional("submeters"): _power_sensor(multiple=True),
                }
            ),
            {"collapsed": False},
        ),
        vol.Required("loads"): section(
            vol.Schema(
                {
                    vol.Optional("switched_loads"): SWITCHED_LOADS,
                    vol.Optional("excluded_switched_loads"): EntitySelector(
                        EntitySelectorConfig(domain=SWITCH_DOMAINS, multiple=True)
                    ),
                }
            ),
            {"collapsed": False},
        ),
        vol.Required("thresholds"): section(
            vol.Schema(
                {
                    vol.Optional("step_threshold_w"): _number(50, 10000, 10, "W"),
                    vol.Optional("night_step_threshold_w"): _number(50, 10000, 10, "W"),
                    vol.Optional(ADAPTIVE_THRESHOLD, default=False): BooleanSelector(),
                    vol.Optional(COLD_APPLIANCES, default=False): BooleanSelector(),
                    vol.Optional("peak_threshold_w"): _number(100, 100000, 100, "W"),
                }
            ),
            {"collapsed": True},
        ),
        vol.Required("analysis"): section(
            vol.Schema(
                {
                    vol.Optional("analysis_time"): TimeSelector(),
                    vol.Optional("backfill_days"): _number(0, 365),
                    vol.Optional("half_life_days"): _number(1, 365),
                    vol.Optional("report_retention_days"): _number(7, 3650),
                    vol.Optional("peak_window_days"): _number(1, 365),
                    vol.Optional("self_test_devices"): _number(0, 10),
                }
            ),
            {"collapsed": True},
        ),
        vol.Required("review"): section(
            vol.Schema(
                {
                    vol.Optional(OPT_REVIEW_REPAIR, default=True): BooleanSelector(),
                    vol.Optional("review_min_uses"): _number(2, 1000),
                    vol.Optional("review_min_days"): _number(1, 365),
                    vol.Optional("review_wait_days"): _number(1, 365),
                    vol.Optional("review_wait_factor"): _number(1.2, 10, 0.1),
                    vol.Optional("review_suggest_score"): _number(50, 100),
                }
            ),
            {"collapsed": True},
        ),
    }
)
SECTIONS = {
    "sensors": (*SENSOR_KEYS, *INVERT_KEYS, SUBMETERS_MODE, "submeters"),
    "loads": ("switched_loads", "excluded_switched_loads"),
    "thresholds": (*THRESHOLD_KEYS, ADAPTIVE_THRESHOLD, COLD_APPLIANCES),
    "analysis": ANALYSIS_KEYS,
    "review": REVIEW_KEYS,
}


def _load(item: dict[str, Any]) -> dict[str, Any]:
    """A switched load in the add-on's form (defaults made explicit)."""
    out: dict[str, Any] = {"entity_id": item["entity_id"]}
    if (item.get("name") or "").strip():
        out["name"] = item["name"].strip()
    states = [s.strip() for s in item.get("on_states") or [] if s and s.strip()]
    out["on_states"] = states or ["on"]
    return out


def _manual_submeters(settings: dict[str, Any]) -> bool:
    return settings.get("submeters_mode") == MODE_MANUAL or bool(settings.get("submeters"))


def _excluded_loads(settings: dict[str, Any]) -> list[str]:
    return list(settings.get("excluded_switched_loads") or [])


def suggested_values(addon: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Values shown in the form: the settings, else what the add-on uses
    without them (Energy dashboard, add-on options, defaults). The lists show
    what the analysis uses: excluded automatic items are left out."""
    settings = addon.get("settings") or {}
    automatic = addon.get("automatic") or {}
    defaults = addon.get("defaults") or {}
    flat: dict[str, Any] = {}
    for key in SENSOR_KEYS:
        value = settings.get(key) or automatic.get(key)
        if value:
            flat[key] = value
    if _manual_submeters(settings):
        submeters = list(settings.get("submeters") or [])
        flat[SUBMETERS_MODE] = MODE_MANUAL if submeters else MODE_NONE
    else:
        flat[SUBMETERS_MODE] = MODE_AUTOMATIC
        excluded = set(settings.get("excluded_submeters") or [])
        submeters = [e for e in automatic.get("submeters") or [] if e not in excluded]
    if submeters:
        flat["submeters"] = submeters
    excluded_loads = _excluded_loads(settings)
    loads = {
        x["entity_id"]: _load(x)
        for x in automatic.get("switched_loads") or []
        if x["entity_id"] not in excluded_loads
    }
    loads.update({x["entity_id"]: _load(x) for x in settings.get("switched_loads") or []})
    if loads:
        flat["switched_loads"] = list(loads.values())
    if excluded_loads:
        flat["excluded_switched_loads"] = excluded_loads
    for key in (*BOOL_KEYS, *THRESHOLD_KEYS, *ANALYSIS_KEYS, *REVIEW_KEYS):
        value = settings.get(key, defaults.get(key))
        if value is not None:
            flat[key] = value
    if "analysis_time" in flat:
        flat["analysis_time"] = f"{flat['analysis_time'][:5]}:00"
    return {name: {k: flat[k] for k in keys if k in flat} for name, keys in SECTIONS.items()}


def _submeter_settings(flat: dict[str, Any], addon: dict[str, Any]) -> dict[str, Any]:
    """Sub-meters (#33). Automatic: an automatic sub-meter removed from the
    list is excluded, so that new ones of the Energy dashboard still come;
    adding a sensor the Energy dashboard does not list makes the list the
    user's own. Manual: the exact list. None: subtract nothing."""
    mode = flat.get(SUBMETERS_MODE) or MODE_AUTOMATIC
    submeters = list(dict.fromkeys(flat.get("submeters") or []))
    automatic = addon.get("automatic")
    auto = list((automatic or {}).get("submeters") or [])
    if mode == MODE_NONE or (mode == MODE_MANUAL and not submeters):
        return {"submeters_mode": MODE_MANUAL}
    if mode == MODE_MANUAL or set(submeters) - set(auto):
        return {"submeters_mode": MODE_MANUAL, "submeters": submeters}
    # automatic; when Home Assistant could not be read, the exclusions stay
    settings = addon.get("settings") or {}
    candidates = auto if automatic else list(settings.get("excluded_submeters") or [])
    excluded = [e for e in candidates if e not in submeters]
    return {"excluded_submeters": excluded} if excluded else {}


def _load_settings(flat: dict[str, Any], addon: dict[str, Any]) -> dict[str, Any]:
    """Switched loads: those of the Energy dashboard, plus or changed by the
    user's entries, minus the excluded ones. An automatic load removed from
    the list is excluded; one put back in the list is not any more."""
    settings = addon.get("settings") or {}
    previous = _excluded_loads(settings)
    known = [_load(x) for x in (addon.get("automatic") or {}).get("switched_loads") or []]
    loads = [_load(x) for x in flat.get("switched_loads") or [] if x.get("entity_id")]
    listed = {x["entity_id"] for x in loads}
    shown = [x["entity_id"] for x in known if x["entity_id"] not in previous]
    removed = [e for e in shown if e not in listed]
    excluded = list(dict.fromkeys([*(flat.get("excluded_switched_loads") or []), *removed]))
    excluded = [e for e in excluded if e not in listed]
    out: dict[str, Any] = {}
    own = [x for x in loads if x not in known]
    if own:
        out["switched_loads"] = list({x["entity_id"]: x for x in own}.values())
    # never measured nor proposed again (#33)
    if excluded:
        out["excluded_switched_loads"] = excluded
    return out


def settings_from_form(user_input: dict[str, Any], addon: dict[str, Any]) -> dict[str, Any]:
    """The settings to store: only what differs from what the add-on would
    use anyway, so that the rest keeps following the Energy dashboard and
    the add-on options."""
    flat = {k: v for name in SECTIONS for k, v in (user_input.get(name) or {}).items()}
    automatic = addon.get("automatic") or {}
    defaults = addon.get("defaults") or {}
    out: dict[str, Any] = {}
    for key in SENSOR_KEYS:
        value = flat.get(key)
        if value and value != automatic.get(key):
            out[key] = value
    out.update(_submeter_settings(flat, addon))
    out.update(_load_settings(flat, addon))
    for key in (*BOOL_KEYS, *THRESHOLD_KEYS, *ANALYSIS_KEYS, *REVIEW_KEYS):
        value = flat.get(key)
        if value is None or value == "":
            continue
        if key == "analysis_time":
            value = str(value)[:5]
        elif key in INT_KEYS:
            value = int(value)
        elif key in THRESHOLD_KEYS or key == "review_wait_factor":
            value = float(value)
        if value != defaults.get(key):
            out[key] = value
    return out


def origin(addon: dict[str, Any], name_of: Callable[[str], str]) -> dict[str, str]:
    """What comes from the Energy dashboard and what the user changed, for
    the description of the form (placeholders)."""
    settings = addon.get("settings") or {}
    automatic = addon.get("automatic")

    def text(names: list[str]) -> str:
        return ", ".join(dict.fromkeys(names)) or "—"

    if automatic is None:
        auto_submeters = auto_loads = "?"
    else:
        auto_submeters = text([name_of(e) for e in automatic.get("submeters") or []])
        loads = automatic.get("switched_loads") or []
        auto_loads = text([x.get("name") or name_of(x["entity_id"]) for x in loads])
    excluded = [*(settings.get("excluded_submeters") or []), *_excluded_loads(settings)]
    own = [name_of(e) for e in settings.get("submeters") or []]
    own += [x.get("name") or name_of(x["entity_id"]) for x in settings.get("switched_loads") or []]
    return {
        "auto_submeters": auto_submeters,
        "auto_loads": auto_loads,
        "excluded": text([name_of(e) for e in excluded]),
        "own": text(own),
    }


class EnergyMapperOptionsFlow(OptionsFlow):
    """The settings of the analysis, pushed to the add-on on save."""

    def __init__(self) -> None:
        self._addon: dict[str, Any] | None = None

    def _client(self) -> EnergyMapperClient:
        return EnergyMapperClient(async_get_clientsession(self.hass), self.config_entry.data[CONF_URL])

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        error = ""
        if self._addon is None:
            try:
                self._addon = await self._client().get_settings()
            except (EnergyMapperConnectionError, EnergyMapperApiError) as err:
                _LOGGER.warning("Cannot read the settings of the add-on: %s", err)
                return self.async_abort(reason="cannot_connect")
            # what the integration last saved wins over the add-on's copy
            if OPT_SETTINGS in self.config_entry.options:
                self._addon["settings"] = self.config_entry.options[OPT_SETTINGS]
        if user_input is not None:
            settings = settings_from_form(user_input, self._addon)
            try:
                await self._client().put_settings(settings)
            except EnergyMapperConnectionError:
                errors["base"] = "cannot_connect"
            except EnergyMapperApiError as err:
                errors["base"] = "invalid_settings"
                error = err.message
            else:
                if self.config_entry.state is ConfigEntryState.LOADED:
                    self.config_entry.runtime_data.coordinator.settings_synced = True
                repair = bool((user_input.get("review") or {}).get(OPT_REVIEW_REPAIR, True))
                return self.async_create_entry(
                    data={**self.config_entry.options, OPT_SETTINGS: settings, OPT_REVIEW_REPAIR: repair}
                )
        if user_input is None:
            values = suggested_values(self._addon)
            values["review"][OPT_REVIEW_REPAIR] = self.config_entry.options.get(OPT_REVIEW_REPAIR, True)
        else:
            values = user_input
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, values),
            errors=errors,
            description_placeholders={"error": error, **origin(self._addon, self._name_of)},
        )

    def _name_of(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.name if state else entity_id


# ---------------------------------------------------------------------------
# appliances: one config subentry each
# ---------------------------------------------------------------------------
USAGES = ["short", "medium", "long", "cycling"]


def _name_schema() -> dict:
    return {vol.Required("name"): TextSelector()}


def _name(user_input: dict[str, Any], errors: dict[str, str]) -> str:
    name = str(user_input.get("name") or "").strip()
    if not name or len(name) > 100:
        errors["name"] = "invalid_name"
    return name


def _relay_schema() -> dict:
    return {
        vol.Required("entity_id"): EntitySelector(EntitySelectorConfig(domain=SWITCH_DOMAINS)),
        vol.Optional("on_states"): TextSelector(TextSelectorConfig(multiple=True)),
    }


class ApplianceSubentryFlow(ConfigSubentryFlow):
    """Add an appliance by hand, or rename it, change its link, merge another
    one into it. Deleting is Home Assistant's own action on the subentry."""

    def _coordinator(self):
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        return entry.runtime_data.coordinator

    def _others(self, exclude: str | None = None) -> dict[str, Appliance]:
        coordinator = self._coordinator()
        return {
            sid: a
            for sid, a in (coordinator.appliances_by_subentry() if coordinator else {}).items()
            if sid != exclude
        }

    def _owner(self, source: Source, exclude: str | None = None) -> Appliance | None:
        """The other appliance that already has this source."""
        return next((a for a in self._others(exclude).values() if a.has(source)), None)

    def _create(self, name: str, sources: list[Source]) -> SubentryFlowResult:
        taken = {s.unique_id for s in self._get_entry().subentries.values() if s.unique_id}
        appliance = new_appliance(name, sources, taken)
        return self.async_create_entry(title=name, data=appliance.subentry_data(), unique_id=appliance.key)

    # -- add ------------------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(step_id="user", menu_options=["add_relay", "add_expected"])

    async def async_step_add_relay(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """An appliance driven by a switch (water heater on a relay...)."""
        errors: dict[str, str] = {}
        placeholders = {"other": ""}
        if user_input is not None:
            name = _name(user_input, errors)
            source = relay(user_input["entity_id"], user_input.get("on_states"))
            if owner := self._owner(source):
                errors["entity_id"] = "already_linked"
                placeholders["other"] = owner.name
            elif not errors:
                return self._create(name, [source])
        schema = vol.Schema({**_name_schema(), **_relay_schema()})
        return self.async_show_form(
            step_id="add_relay",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_add_expected(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """An appliance not seen yet, described by its power ("Kettle, 2000 W"):
        the add-on recognises it from its first use."""
        errors: dict[str, str] = {}
        placeholders = {"other": ""}
        if user_input is not None and (name := _name(user_input, errors)):
            coordinator = self._coordinator()
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            usage = user_input["usage"]
            kind, band = ("cycling", "session") if usage == "cycling" else ("steady", usage)
            try:
                answer = await coordinator.client.expect_signature(
                    kind, band, float(user_input["power_w"]), name
                )
            except EnergyMapperConnectionError:
                errors["base"] = "cannot_connect"
            except EnergyMapperApiError as err:
                if err.code == "conflict":
                    errors["power_w"] = "already_linked"
                    sid = err.message.rsplit(": ", 1)[-1]
                    other = next(
                        (a.name for a in self._others().values() for s in a.sources if s.id == sid), None
                    )
                    placeholders["other"] = other or sid
                else:
                    errors["base"] = "addon_too_old" if err.status in (404, 405) else "cannot_connect"
            else:
                source = signature_source(answer["signature"])
                if owner := self._owner(source):
                    errors["power_w"] = "already_linked"
                    placeholders["other"] = owner.name
                else:
                    return self._create(name, [source])
        schema = vol.Schema(
            {
                **_name_schema(),
                vol.Required("power_w"): _number(50, 100000, 10, "W"),
                vol.Required("usage", default="short"): SelectSelector(
                    SelectSelectorConfig(
                        options=USAGES, translation_key="usage", mode=SelectSelectorMode.LIST
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="add_expected",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- edit -----------------------------------------------------------------
    def _current(self) -> Appliance:
        sub = self._get_reconfigure_subentry()
        return Appliance.from_data(sub.data, sub.title, sub.subentry_id)

    def _placeholders(self) -> dict[str, str]:
        a = self._current()
        return {"name": a.name, "link": link_text(a), "other": ""}

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["rename", "link_relay", "link_sensor", "merge"],
            description_placeholders=self._placeholders(),
        )

    async def async_step_rename(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Device, entity and statistic names follow; their ids do not change."""
        errors: dict[str, str] = {}
        if user_input is not None and (name := _name(user_input, errors)) and not errors:
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=name
            )
        schema = vol.Schema(_name_schema())
        return self.async_show_form(
            step_id="rename",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or {"name": self._current().name}
            ),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    def _relink(self, source: Source) -> SubentryFlowResult:
        current = self._current()
        data = replace(current, sources=(source,)).subentry_data()
        return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(), data=data)

    async def async_step_link_relay(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Driven by this switch from now on (replaces the current link)."""
        current = self._current()
        errors: dict[str, str] = {}
        placeholders = self._placeholders()
        if user_input is not None:
            source = relay(user_input["entity_id"], user_input.get("on_states"))
            if owner := self._owner(source, current.subentry_id):
                errors["entity_id"] = "already_linked"
                placeholders["other"] = owner.name
            else:
                return self._relink(source)
        suggested = user_input
        if suggested is None:
            loads = [s for s in current.sources if s.type == KIND_LOAD]
            suggested = (
                {"entity_id": loads[0].id, "on_states": list(loads[0].on_states or [])} if loads else {}
            )
        return self.async_show_form(
            step_id="link_relay",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(_relay_schema()), suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_link_sensor(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Measured by this power sensor from now on: it becomes a sub-meter."""
        current = self._current()
        errors: dict[str, str] = {}
        placeholders = self._placeholders()
        if user_input is not None:
            source = Source(KIND_SUBMETER, user_input["entity_id"])
            if owner := self._owner(source, current.subentry_id):
                errors["entity_id"] = "already_linked"
                placeholders["other"] = owner.name
            else:
                return self._relink(source)
        schema = vol.Schema({vol.Required("entity_id"): _power_sensor()})
        meters = [s.id for s in current.sources if s.type == KIND_SUBMETER]
        return self.async_show_form(
            step_id="link_sensor",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or ({"entity_id": meters[0]} if meters else {})
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_merge(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Another appliance is the same one: it joins this one."""
        current = self._current()
        others = self._others(current.subentry_id)
        if not others:
            return self.async_abort(reason="nothing_to_merge")
        if user_input is not None:
            coordinator = self._coordinator()
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            await coordinator.async_merge(current.subentry_id, user_input["other"])
            return self.async_abort(
                reason="merged",
                description_placeholders={"name": current.name, "other": others[user_input["other"]].name},
            )
        options = [
            {"value": sid, "label": f"{a.name} ({link_text(a)})"}
            for sid, a in sorted(others.items(), key=lambda x: x[1].name.lower())
        ]
        schema = vol.Schema(
            {
                vol.Required("other"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(
            step_id="merge", data_schema=schema, description_placeholders=self._placeholders()
        )
