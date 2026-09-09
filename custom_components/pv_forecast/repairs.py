"""Die verwaltete PV-Seite direkt aus Home Assistants Reparaturdialog aktualisieren."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .dashboard import CONF_DASHBOARD_ENABLED, dashboard_issue_id


class DashboardUpdateRepairFlow(RepairsFlow):
    """Version bewusst übernehmen und einen vollständigen Browser-Neuaufruf anbieten."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Die Aktion zunächst mit einer verständlichen Bestätigung anbieten."""

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Den lebenden Eintrag erneut prüfen, bevor dessen Optionen geändert werden."""

        entry_id = (self.data or {}).get("entry_id")
        entry = (
            self.hass.config_entries.async_get_entry(entry_id)
            if isinstance(entry_id, str)
            else None
        )
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.options.get(CONF_DASHBOARD_ENABLED) is not True
        ):
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_abort(reason="no_longer_needed")
        manager = getattr(getattr(entry, "runtime_data", None), "dashboard", None)
        if manager is None or manager.status == "off":
            return self.async_abort(reason="not_loaded")
        errors = {}
        if user_input is not None:
            revision = (self.data or {}).get("revision")
            if not isinstance(revision, str):
                return self.async_abort(reason="changed")
            try:
                url = await manager.async_apply_update(revision)
            except ValueError:
                return self.async_abort(reason="changed")
            except HomeAssistantError:
                errors["base"] = "dashboard_unavailable"
            else:
                # Ein gewöhnlicher Link lädt ein neues Dokument. Ein SPA-Wechsel
                # allein ersetzt bereits registrierte Custom Elements nicht.
                return self.async_abort(
                    reason="updated", description_placeholders={"dashboard_url": url}
                )
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}), errors=errors
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Nur den eigenen, anlagenspezifischen Reparaturvertrag bedienen."""

    entry_id = (data or {}).get("entry_id")
    if not isinstance(entry_id, str) or issue_id != dashboard_issue_id(entry_id):
        raise ValueError("Unbekannte PV-Dashboard-Reparatur")
    return DashboardUpdateRepairFlow()
