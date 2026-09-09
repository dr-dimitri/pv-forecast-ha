"""Bewusste Zuordnung realer AC-Wechselrichtergrenzen im Optionsmenü."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.helpers.translation import async_get_translations

from .calculations import InvalidConfigurationError
from .configuration import inverter_groups_from_options
from .const import (
    CONF_CONFIRM_REMOVE,
    CONF_GROUP_ID,
    CONF_GROUP_MAX_POWER_KW,
    CONF_GROUP_ROOF_IDS,
    CONF_INVERTER_GROUPS,
    CONF_NAME,
    CONF_ROOF_ID,
    CONF_ROOFS,
    DOMAIN,
)


def groups_for_remaining_roofs(
    groups: list[dict[str, Any]], roof_ids: set[str]
) -> list[dict[str, Any]]:
    """Bestätigte Dachlöschungen ohne verwaiste oder leere Gruppen übernehmen."""

    return [
        dict(group) | {CONF_GROUP_ROOF_IDS: remaining}
        for group in groups
        if (
            remaining := [
                roof_id for roof_id in group[CONF_GROUP_ROOF_IDS] if roof_id in roof_ids
            ]
        )
    ]


class InverterGroupFlowMixin:
    """Gruppen einzeln ändern, ohne Dächer oder die übrigen Optionen anzutasten."""

    _selected_inverter_group: str | None = None

    def _inverter_groups(self) -> list[dict[str, Any]]:
        return [
            dict(group) | {CONF_GROUP_ROOF_IDS: list(group[CONF_GROUP_ROOF_IDS])}
            for group in self.config_entry.options.get(CONF_INVERTER_GROUPS, [])
        ]

    async def _async_inverter_texts(self) -> dict[str, str]:
        resources = await async_get_translations(
            self.hass, "de", "common", integrations={DOMAIN}
        )
        prefix = f"component.{DOMAIN}.common."
        return {key.removeprefix(prefix): value for key, value in resources.items()}

    async def async_step_inverter_groups(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Vorhandene AC-Gruppen zeigen und gezielte Änderungen anbieten."""

        groups = self._inverter_groups()
        roofs = {
            roof[CONF_ROOF_ID]: roof[CONF_NAME]
            for roof in self.config_entry.options[CONF_ROOFS]
        }
        assigned = {
            roof_id for group in groups for roof_id in group[CONF_GROUP_ROOF_IDS]
        }
        menu = []
        if set(roofs) - assigned:
            menu.append("add_inverter_group")
        if groups:
            menu.extend(["edit_inverter_group", "remove_inverter_group"])
        menu.append("init")
        texts = await self._async_inverter_texts()
        summary = (
            "\n".join(
                f"- **{group[CONF_NAME]}:** {group[CONF_GROUP_MAX_POWER_KW]} kW · "
                + ", ".join(
                    str(roofs[roof_id]) for roof_id in group[CONF_GROUP_ROOF_IDS]
                )
                for group in groups
            )
            or texts["inverter_groups_none"]
        )
        return self.async_show_menu(
            step_id="inverter_groups",
            menu_options=menu,
            description_placeholders={"groups": summary},
        )

    async def async_step_add_inverter_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._selected_inverter_group = None
        return await self.async_step_inverter_group_details(user_input)

    def _inverter_group_selection(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_GROUP_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=group[CONF_GROUP_ID], label=group[CONF_NAME]
                            )
                            for group in self._inverter_groups()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

    async def async_step_edit_inverter_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._selected_inverter_group = user_input[CONF_GROUP_ID]
            return await self.async_step_inverter_group_details()
        return self.async_show_form(
            step_id="edit_inverter_group",
            data_schema=self._inverter_group_selection(),
        )

    async def async_step_inverter_group_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name, echte AC-Grenze und disjunkte Dachzuordnung gemeinsam prüfen."""

        groups = self._inverter_groups()
        existing = next(
            (
                group
                for group in groups
                if group[CONF_GROUP_ID] == self._selected_inverter_group
            ),
            None,
        )
        if self._selected_inverter_group is not None and existing is None:
            return self.async_abort(reason="reconfigure_entry_changed")
        errors: dict[str, str] = {}
        if user_input is not None:
            updated = {
                CONF_GROUP_ID: self._selected_inverter_group or uuid4().hex,
                CONF_NAME: str(user_input[CONF_NAME]).strip(),
                CONF_GROUP_MAX_POWER_KW: user_input[CONF_GROUP_MAX_POWER_KW],
                CONF_GROUP_ROOF_IDS: list(user_input[CONF_GROUP_ROOF_IDS]),
            }
            new_groups = [
                (
                    group
                    if group[CONF_GROUP_ID] != self._selected_inverter_group
                    else updated
                )
                for group in groups
            ]
            if existing is None:
                new_groups.append(updated)
            options = dict(self.config_entry.options) | {
                CONF_INVERTER_GROUPS: new_groups
            }
            try:
                inverter_groups_from_options(options)
                if (
                    sum(
                        group[CONF_NAME].strip().casefold()
                        == updated[CONF_NAME].casefold()
                        for group in new_groups
                    )
                    != 1
                ):
                    raise InvalidConfigurationError(
                        "Gruppennamen müssen eindeutig sein"
                    )
            except (InvalidConfigurationError, KeyError, ValueError, TypeError):
                errors["base"] = "invalid_inverter_group"
            else:
                return self.async_create_entry(title="", data=options)
        texts = await self._async_inverter_texts()
        defaults = (
            existing
            or {
                CONF_NAME: texts["default_inverter_group_name"].format(
                    number=len(groups) + 1
                ),
                CONF_GROUP_MAX_POWER_KW: 5.0,
                CONF_GROUP_ROOF_IDS: [],
            }
        ) | (user_input or {})
        assigned_elsewhere = {
            roof_id
            for group in groups
            if group[CONF_GROUP_ID] != self._selected_inverter_group
            for roof_id in group[CONF_GROUP_ROOF_IDS]
        }
        choices = [
            SelectOptionDict(value=roof[CONF_ROOF_ID], label=roof[CONF_NAME])
            for roof in self.config_entry.options[CONF_ROOFS]
            if roof[CONF_ROOF_ID] not in assigned_elsewhere
        ]
        return self.async_show_form(
            step_id="inverter_group_details",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=defaults[CONF_NAME]
                    ): TextSelector(),
                    vol.Required(
                        CONF_GROUP_MAX_POWER_KW,
                        default=defaults[CONF_GROUP_MAX_POWER_KW],
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            step="any",
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="kW",
                        )
                    ),
                    vol.Required(
                        CONF_GROUP_ROOF_IDS, default=defaults[CONF_GROUP_ROOF_IDS]
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=choices, multiple=True, mode=SelectSelectorMode.LIST
                        )
                    ),
                }
            ),
        )

    async def async_step_remove_inverter_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._selected_inverter_group = user_input[CONF_GROUP_ID]
            return await self.async_step_confirm_remove_inverter_group()
        return self.async_show_form(
            step_id="remove_inverter_group",
            data_schema=self._inverter_group_selection(),
        )

    async def async_step_confirm_remove_inverter_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erst nach Bestätigung eine AC-Gruppe lösen; Dachwerte bleiben erhalten."""

        groups = self._inverter_groups()
        selected = next(
            (
                group
                for group in groups
                if group[CONF_GROUP_ID] == self._selected_inverter_group
            ),
            None,
        )
        if selected is None:
            return self.async_abort(reason="reconfigure_entry_changed")
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM_REMOVE):
                return await self.async_step_inverter_groups()
            return self.async_create_entry(
                title="",
                data=dict(self.config_entry.options)
                | {
                    CONF_INVERTER_GROUPS: [
                        group
                        for group in groups
                        if group[CONF_GROUP_ID] != self._selected_inverter_group
                    ],
                },
            )
        return self.async_show_form(
            step_id="confirm_remove_inverter_group",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRM_REMOVE, default=False): BooleanSelector()}
            ),
            description_placeholders={"name": str(selected[CONF_NAME])},
        )
