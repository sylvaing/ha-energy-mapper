"""Sensors: one device per appliance under the Energy Mapper service device.

Values come from the nightly analysis (the last analysed day); the hourly
energy itself is in the long-term statistics (see statistics.py), not in
these sensors, so the Energy dashboard gets the right hours. Live power and
on/off states come with real-time detection (#17).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .appliances import KIND_LOAD, Appliance
from .const import DOMAIN
from .coordinator import EnergyMapperConfigEntry, EnergyMapperCoordinator

PARALLEL_UPDATES = 0
MANUFACTURER = "Energy Mapper"


def _ts(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromtimestamp(float(value), dt.UTC)


def _latest(coordinator: EnergyMapperCoordinator, section: str, appliance: Appliance, default: Any) -> Any:
    """A value of ``appliance`` on the last analysed day, None when no day is analysed."""
    if not coordinator.latest.get("day"):
        return None
    values = (coordinator.latest.get(section) or {}).get(appliance.hourly_kind) or {}
    return values.get(appliance.source_id, default)


@dataclass(frozen=True, kw_only=True)
class ApplianceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EnergyMapperCoordinator, Appliance], Any]
    with_day: bool = False


@dataclass(frozen=True, kw_only=True)
class ServiceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EnergyMapperCoordinator], Any]
    with_day: bool = False


APPLIANCE_SENSORS: tuple[ApplianceSensorDescription, ...] = (
    ApplianceSensorDescription(
        key="energy_yesterday",
        translation_key="energy_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda c, a: _latest(c, "energy_kwh", a, 0.0),
        with_day=True,
    ),
    ApplianceSensorDescription(
        key="uses_yesterday",
        translation_key="uses_yesterday",
        value_fn=lambda c, a: _latest(c, "uses", a, 0),
        with_day=True,
    ),
    ApplianceSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c, a: _ts(c.last_seen.get(a.key)),
    ),
    ApplianceSensorDescription(
        key="power",
        translation_key="typical_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, a: a.power_w,
    ),
)

SERVICE_SENSORS: tuple[ServiceSensorDescription, ...] = (
    ServiceSensorDescription(
        key="unexplained_energy_yesterday",
        translation_key="unexplained_energy_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda c: (c.latest.get("energy_kwh") or {}).get("other"),
        with_day=True,
    ),
    ServiceSensorDescription(
        key="base_load",
        translation_key="base_load",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda c: (c.latest.get("energy_kwh") or {}).get("base_w"),
        with_day=True,
    ),
    ServiceSensorDescription(
        key="last_analysis",
        translation_key="last_analysis",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _ts((c.data or {}).get("last_run")),
    ),
)


def service_device(coordinator: EnergyMapperCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        name="Energy Mapper",
        manufacturer=MANUFACTURER,
        model="Add-on",
        sw_version=(coordinator.data or {}).get("version"),
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    # the service device first: the appliance devices are linked to it
    service = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **service_device(coordinator)
    )
    via: dict[str, Any] = (
        {"via_device_id": service.id}
        if "via_device_id" in DeviceInfo.__optional_keys__  # Home Assistant 2026.8+
        else {"via_device": (DOMAIN, entry.entry_id)}
    )
    async_add_entities(ServiceSensor(coordinator, d) for d in SERVICE_SENSORS)

    added: set[str] = set()

    @callback
    def add_appliances() -> None:
        added.intersection_update(coordinator.appliances)  # removed ones may come back
        new = [a for key, a in coordinator.appliances.items() if key not in added]
        if not new:
            return
        added.update(a.key for a in new)
        async_add_entities(ApplianceSensor(coordinator, a, d, via) for a in new for d in APPLIANCE_SENSORS)

    add_appliances()
    entry.async_on_unload(coordinator.async_add_listener(add_appliances))


class _Base(CoordinatorEntity[EnergyMapperCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _with_day = False

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._with_day:
            return None
        return {"day": self.coordinator.latest.get("day")}


class ServiceSensor(_Base):
    """A sensor of the whole analysis, on the service device."""

    entity_description: ServiceSensorDescription

    def __init__(self, coordinator: EnergyMapperCoordinator, description: ServiceSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._with_day = description.with_day
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_device_info = service_device(coordinator)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)


class ApplianceSensor(_Base):
    """A sensor of one appliance, on the appliance's device."""

    entity_description: ApplianceSensorDescription

    def __init__(
        self,
        coordinator: EnergyMapperCoordinator,
        appliance: Appliance,
        description: ApplianceSensorDescription,
        via: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._with_day = description.with_day
        self._key = appliance.key
        self._attr_unique_id = f"{appliance.key}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, appliance.key)},
            name=appliance.name,
            manufacturer=MANUFACTURER,
            model="Switched appliance" if appliance.kind == KIND_LOAD else "Recognised appliance",
            **via,
        )

    @property
    def _appliance(self) -> Appliance | None:
        return self.coordinator.appliances.get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._appliance is not None

    @property
    def native_value(self) -> Any:
        appliance = self._appliance
        if appliance is None:
            return None
        return self.entity_description.value_fn(self.coordinator, appliance)
