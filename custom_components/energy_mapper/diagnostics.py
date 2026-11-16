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
    }
