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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .frontend import CARD_URL, async_get_card_revision

if TYPE_CHECKING:
    from homeassistant.components.frontend import Panel

CONF_DASHBOARD_ENABLED = "dashboard_enabled"
CONF_DASHBOARD_TITLE = "dashboard_title"
CONF_DASHBOARD_REVISION = "dashboard_revision"
DASHBOARD_OPTIONS = {
    CONF_DASHBOARD_ENABLED,
    CONF_DASHBOARD_TITLE,
    CONF_DASHBOARD_REVISION,
}
_LOGGER = logging.getLogger(__name__)


def dashboard_path(entry_id: str) -> str:
    """Namensunabhängige URL je Anlage, ohne Annahmen über das Entry-ID-Format."""

    return f"pv-forecast-{hashlib.sha256(entry_id.encode()).hexdigest()[:20]}"


def dashboard_title(options: dict[str, Any], name: str) -> str:
    """Gespeicherten Titel oder den vorgegebenen Anlagenbezug verwenden."""

    value = options.get(CONF_DASHBOARD_TITLE)
    return value.strip() if isinstance(value, str) and value.strip() else f"PV · {name}"


def dashboard_issue_id(entry_id: str) -> str:
    """Eine stabile Reparaturmeldung je Anlage statt einer Meldung je Neustart."""

    return f"dashboard_update_{entry_id}"


class DashboardManager:
    """Nur das eigene Panel verwalten; fachliche Optionen benötigen weiter Reload."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.path = dashboard_path(entry.entry_id)
        self.status = "off"
        self.revision: str | None = None
        self._display_options: tuple[Any, Any] | None = None
        self._original_data = deepcopy(dict(entry.data))
        self._original_options = deepcopy(
            {
                key: value
                for key, value in entry.options.items()
                if key not in DASHBOARD_OPTIONS | {"morning_mode"}
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
            if key not in DASHBOARD_OPTIONS | {"morning_mode"}
        }

    def display_options_changed(self) -> bool:
        """Änderungen an Schalter oder Titel erkennen."""

        return self._display_options != (
            self.entry.options.get(CONF_DASHBOARD_ENABLED),
            self.entry.options.get(CONF_DASHBOARD_TITLE),
        )

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

    @callback
    def _update_issue(self) -> None:
        """Inhaltsänderungen melden; Ignorieren derselben Fassung erhalten."""

        issue_id = dashboard_issue_id(self.entry.entry_id)
        previous = self.entry.options.get(CONF_DASHBOARD_REVISION)
        if previous is None:
            # Ohne früheren Vergleichsstand wird keine Änderung behauptet.
            self.hass.config_entries.async_update_entry(
                self.entry,
                options=dict(self.entry.options)
                | {CONF_DASHBOARD_REVISION: self.revision},
            )
        if previous is None or previous == self.revision:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
        if issue is not None and (issue.data or {}).get("revision") != self.revision:
            # Eine weitere Änderung darf nicht durch eine alte Ignorierung verschwinden.
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="dashboard_update",
            translation_placeholders={
                "dashboard": dashboard_title(dict(self.entry.options), self.entry.title)
            },
            data={"entry_id": self.entry.entry_id, "revision": self.revision},
        )

    async def async_apply_update(self, expected_revision: str) -> str:
        """Die Version erst nach erfolgreicher Panelregistrierung übernehmen."""

        await self.async_sync()
        if self._stopped or self.status != "ready":
            raise HomeAssistantError("Das PV-Dashboard ist derzeit nicht verfügbar")
        if self.revision != expected_revision:
            raise ValueError(
                "Das Dashboard wurde während der Reparatur erneut geändert"
            )
        self.hass.config_entries.async_update_entry(
            self.entry,
            options=dict(self.entry.options) | {CONF_DASHBOARD_REVISION: self.revision},
        )
        ir.async_delete_issue(
            self.hass, DOMAIN, dashboard_issue_id(self.entry.entry_id)
        )
        return f"/{self.path}?pv_revision={self.revision}"

    async def async_sync(self) -> None:
        """Bewusste Auswahl anwenden und gegebenenfalls auf das Frontend warten."""

        async with self._lock:
            if self._stopped:
                return
            self._display_options = (
                self.entry.options.get(CONF_DASHBOARD_ENABLED),
                self.entry.options.get(CONF_DASHBOARD_TITLE),
            )
            self._clear()
            if self.entry.options.get(CONF_DASHBOARD_ENABLED) is not True:
                self.status = "off"
                ir.async_delete_issue(
                    self.hass, DOMAIN, dashboard_issue_id(self.entry.entry_id)
                )
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
                self.revision = await async_get_card_revision(self.hass)
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
                    module_url=f"{CARD_URL}?v={self.revision}",
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
                self._update_issue()
            except (HomeAssistantError, OSError, ValueError):
                self.status = "error"
                _LOGGER.exception(
                    "Die optionale PV-Dashboard-Seite konnte nicht eingerichtet werden"
                )
