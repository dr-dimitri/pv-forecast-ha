"""Experimentelle Horizontprofile bewusst je Dachfläche bearbeiten."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import CONF_NAME, CONF_ROOF_ID, CONF_ROOFS
from .shading import CONF_HORIZON_PROFILES, parse_profile


class ShadingFlowMixin:
    """Profile unabhängig von Namen und anderen Dachoptionen verwalten."""

    _horizon_roof_id: str | None = None

    async def async_step_horizon_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine vorhandene Dachfläche über ihre stabile ID auswählen."""

        roofs = self.config_entry.options[CONF_ROOFS]
        errors = {}
        if user_input is not None:
            selected = user_input.get(CONF_ROOF_ID)
            if selected in {roof[CONF_ROOF_ID] for roof in roofs}:
                self._horizon_roof_id = selected
                return await self.async_step_horizon_details()
            errors["base"] = "invalid_horizon_roof"
        return self.async_show_form(
            step_id="horizon_profile",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROOF_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=roof[CONF_ROOF_ID], label=roof[CONF_NAME]
                                )
                                for roof in roofs
                            ]
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_horizon_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ein Profil speichern oder mit leerem Eingabefeld gezielt entfernen."""

        roofs = {
            roof[CONF_ROOF_ID]: roof for roof in self.config_entry.options[CONF_ROOFS]
        }
        if self._horizon_roof_id not in roofs:
            return await self.async_step_horizon_profile()
        profiles = dict(self.config_entry.options.get(CONF_HORIZON_PROFILES, {}))
        text = ", ".join(
            str(value) for value in profiles.get(self._horizon_roof_id, ())
        )
        errors = {}
        if user_input is not None:
            text = user_input.get("profile", "")
            try:
                profile = parse_profile(text)
            except (ValueError, OverflowError):
                errors["base"] = "invalid_horizon_profile"
            else:
                if profile and user_input.get("confirm_horizon") is not True:
                    errors["base"] = "confirm_horizon_required"
                else:
                    if profile:
                        profiles[self._horizon_roof_id] = list(profile)
                    else:
                        profiles.pop(self._horizon_roof_id, None)
                    options = dict(self.config_entry.options)
                    if profiles:
                        options[CONF_HORIZON_PROFILES] = profiles
                    else:
                        options.pop(CONF_HORIZON_PROFILES, None)
                    return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="horizon_details",
            description_placeholders={
                "roof": str(roofs[self._horizon_roof_id][CONF_NAME])
            },
            data_schema=vol.Schema(
                {
                    vol.Optional("profile", default=text): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Required("confirm_horizon", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )
