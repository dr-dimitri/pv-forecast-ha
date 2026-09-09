"""Freiwillige Einrichtung einer PV-Seite im Config- und Options-Dialog."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import BooleanSelector, TextSelector
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .dashboard import CONF_DASHBOARD_ENABLED, CONF_DASHBOARD_TITLE, dashboard_title


class DashboardFlowMixin:
    """Eine Vorschau im Setup speichern oder die bestehenden Optionen aktualisieren."""

    async def async_step_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options, name, status = self._dashboard_context()
        errors = {}
        if user_input is not None:
            enabled = user_input.get(CONF_DASHBOARD_ENABLED)
            title = user_input.get(CONF_DASHBOARD_TITLE, "")
            if (
                not isinstance(enabled, bool)
                or not isinstance(title, str)
                or (enabled and not 1 <= len(title.strip()) <= 80)
            ):
                errors["base"] = "invalid_dashboard"
            else:
                options = dict(options) | {CONF_DASHBOARD_ENABLED: enabled}
                if title.strip():
                    options[CONF_DASHBOARD_TITLE] = title.strip()
                else:
                    options.pop(CONF_DASHBOARD_TITLE, None)
                return await self._async_dashboard_done(options)
        texts = await async_get_translations(
            self.hass, "de", "common", integrations={DOMAIN}
        )
        return self.async_show_form(
            step_id="dashboard",
            description_placeholders={
                "status": texts[f"component.{DOMAIN}.common.dashboard_{status}"]
            },
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DASHBOARD_ENABLED,
                        default=(user_input or {}).get(
                            CONF_DASHBOARD_ENABLED,
                            options.get(CONF_DASHBOARD_ENABLED) is True,
                        ),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_DASHBOARD_TITLE,
                        default=(user_input or {}).get(
                            CONF_DASHBOARD_TITLE,
                            dashboard_title(dict(options), name)[:80],
                        ),
                    ): TextSelector(),
                }
            ),
            errors=errors,
        )
