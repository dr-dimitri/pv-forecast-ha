"""Bewusst aktivierte Dashboard-Seite über den nativen HA-Panelvertrag verwalten."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .frontend import CARD_URL

if TYPE_CHECKING:
    from homeassistant.components.frontend import Panel

CONF_DASHBOARD_ENABLED = "dashboard_enabled"
CONF_DASHBOARD_TITLE = "dashboard_title"
DASHBOARD_OPTIONS = {CONF_DASHBOARD_ENABLED, CONF_DASHBOARD_TITLE}
_LOGGER = logging.getLogger(__name__)


def dashboard_path(entry_id: str) -> str:
    """Namensunabhängige URL je Anlage, ohne Annahmen über das Entry-ID-Format."""

    return f"pv-forecast-{hashlib.sha256(entry_id.encode()).hexdigest()[:20]}"


def dashboard_title(options: dict[str, Any], name: str) -> str:
    """Gespeicherten Titel oder den vorgegebenen Anlagenbezug verwenden."""

    value = options.get(CONF_DASHBOARD_TITLE)
    return value.strip() if isinstance(value, str) and value.strip() else f"PV · {name}"


class DashboardManager:
    """Nur das eigene Panel verwalten; fachliche Optionen benötigen weiter Reload."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.path = dashboard_path(entry.entry_id)
        self.status = "off"
        self._original_data = deepcopy(dict(entry.data))
        self._original_options = deepcopy(
            {
                key: value
                for key, value in entry.options.items()
                if key not in DASHBOARD_OPTIONS
            }
        )
        self._lock = asyncio.Lock()
        self._cancel_listener: CALLBACK_TYPE | None = None
        self._panel: Panel | None = None
        self._stopped = False

    def only_dashboard_options_changed(self) -> bool:
        """Reine Dashboardänderungen ohne fachlichen Reload erkennen."""

        return self._original_data == dict(
            self.entry.data
        ) and self._original_options == {
            key: value
            for key, value in self.entry.options.items()
            if key not in DASHBOARD_OPTIONS
        }

    @callback
    def _clear(self) -> None:
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        if self._panel is not None:
            from homeassistant.components import frontend

            # Eine zwischenzeitlich fremd ersetzte URL gehört uns nicht mehr.
            if (
                self.hass.data.get(frontend.DATA_PANELS, {}).get(self.path)
                is self._panel
            ):
                frontend.async_remove_panel(self.hass, self.path)
            self._panel = None

    @callback
    def async_stop(self) -> None:
        """Auch noch wartende Frontend-Listener beim Entladen sicher entfernen."""

        self._stopped = True
        self._clear()
        self.status = "off"

    async def async_sync(self) -> None:
        """Bewusste Auswahl anwenden und gegebenenfalls auf das Frontend warten."""

        async with self._lock:
            if self._stopped:
                return
            self._clear()
            if self.entry.options.get(CONF_DASHBOARD_ENABLED) is not True:
                self.status = "off"
                return
            if "frontend" not in self.hass.config.components or self.hass.http is None:
                self.status = "waiting"

                async def loaded(event: Event) -> None:
                    if event.data.get("component") in ("frontend", "http"):
                        await self.async_sync()

                self._cancel_listener = self.hass.bus.async_listen(
                    EVENT_COMPONENT_LOADED, loaded
                )
                return
            from homeassistant.components import frontend, panel_custom

            if frontend.async_panel_exists(self.hass, self.path):
                self.status = "conflict"
                _LOGGER.warning(
                    "Die PV-Dashboard-Adresse ist durch ein fremdes Panel belegt"
                )
                return
            name = str(
                self.entry.data.get(
                    "plant_name", self.entry.data.get("location_name", self.entry.title)
                )
            )
            try:
                texts = await async_get_translations(
                    self.hass, "de", "common", integrations={DOMAIN}
                )
                if self._stopped:
                    return
                await panel_custom.async_register_panel(
                    self.hass,
                    frontend_url_path=self.path,
                    webcomponent_name="pv-forecast-panel",
                    sidebar_title=dashboard_title(dict(self.entry.options), name),
                    sidebar_icon="mdi:solar-power-variant",
                    module_url=f"{CARD_URL}?v=2",
                    config={
                        "config_entry_id": self.entry.entry_id,
                        "menu_label": texts[
                            f"component.{DOMAIN}.common.dashboard_menu"
                        ],
                    },
                    require_admin=False,
                )
                self._panel = self.hass.data.get(frontend.DATA_PANELS, {}).get(
                    self.path
                )
                if self._stopped:
                    self._clear()
                    return
                self.status = "ready"
            except (HomeAssistantError, ValueError):
                self.status = "error"
                _LOGGER.exception(
                    "Die optionale PV-Dashboard-Seite konnte nicht eingerichtet werden"
                )
