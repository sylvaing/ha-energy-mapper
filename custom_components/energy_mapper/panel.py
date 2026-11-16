"""Sidebar panel: serves the bundle of frontend/ and registers it (panel_custom)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "energy-mapper"
PANEL_ELEMENT = "energy-mapper-panel"
PANEL_TITLE = "Energy Mapper"
PANEL_ICON = "mdi:home-lightning-bolt-outline"

STATIC_URL = f"/{DOMAIN}_frontend"
BUNDLE_NAME = f"{PANEL_ELEMENT}.js"
BUNDLE_PATH = Path(__file__).parent / "frontend" / BUNDLE_NAME

# the static path cannot be unregistered: it is registered once per run
DATA_STATIC = f"{DOMAIN}_static_registered"


def _bundle_hash(path: Path) -> str | None:
    """Short hash of the bundle (cache busting), or None when it is missing."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


async def async_register_panel(hass: HomeAssistant) -> bool:
    """Serve the bundle and add the panel to the sidebar; False if the bundle is missing."""
    version = await hass.async_add_executor_job(_bundle_hash, BUNDLE_PATH)
    if version is None:
        _LOGGER.warning("Panel bundle %s not found: the Energy Mapper panel is not available", BUNDLE_PATH)
        return False
    if not hass.data.get(DATA_STATIC):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(BUNDLE_PATH.parent), cache_headers=True)]
        )
        hass.data[DATA_STATIC] = True
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/{BUNDLE_NAME}?v={version}",
        require_admin=False,
    )
    return True


@callback
def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the panel from the sidebar (the static path stays)."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
