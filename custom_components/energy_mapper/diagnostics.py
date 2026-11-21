"""Diagnostics: the entry, the settings and the last health of the add-on."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import EnergyMapperConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnergyMapperConfigEntry
) -> dict[str, Any]:
    """Nothing secret is stored: the add-on API has no credentials."""
    coordinator = entry.runtime_data.coordinator
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "addon": {
            "available": coordinator.last_update_success,
            "health": coordinator.data,
            "settings_synced": coordinator.settings_synced,
        },
        "appliances": {
            key: {**a.as_dict(), "statistic_id": a.statistic_id, "last_seen": coordinator.last_seen.get(key)}
            for key, a in coordinator.appliances.items()
        },
        "imported_days": coordinator.imported,
        "latest_day": coordinator.latest.get("day"),
    }
