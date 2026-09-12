"""Administrative Morgenoptionen mit bewusstem Opt-in und lesbarem Prüfstatus."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .calibration_configuration import _has_energy_source


class MorningFlowMixin:
    """Einrichtungsentwürfe wirken erst beim ausdrücklichen Speichern."""

    async def async_step_morning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._check_options_unchanged()
        return self.async_show_menu(
            step_id="morning",
            menu_options=[
                "morning_settings",
                "morning_status",
                "morning_reset",
                "advanced_options",
            ],
        )

    async def async_step_morning_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._check_options_unchanged()
        options = dict(self.config_entry.options)
        errors = {}
        if user_input is not None:
            mode = user_input["morning_mode"]
            if mode != "off" and options.get("history_enabled") is not True:
                errors["base"] = "calibration_history_required"
            elif mode != "off" and not _has_energy_source(options):
                errors["base"] = "calibration_energy_required"
            else:
                return self.async_create_entry(title="", data={**options, **user_input})
        return self.async_show_form(
            step_id="morning_settings",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "morning_mode",
                        default=(user_input or options).get("morning_mode", "off"),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=["off", "observe", "auto"],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="morning_mode",
                        )
                    ),
                    vol.Required(
                        "morning_threshold_kw",
                        default=(user_input or options).get(
                            "morning_threshold_kw", 0.5
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0.05,
                            max=100,
                            step=0.05,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="kW",
                        )
                    ),
                }
            ),
        )

    async def async_step_morning_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._check_options_unchanged()
        if user_input is not None:
            return await self.async_step_morning()
        manager = getattr(
            getattr(self.config_entry, "runtime_data", None), "morning", None
        )
        report = manager.snapshot() if manager else {"status": "off"}
        from .config_flow import _async_ui_translations

        texts = await _async_ui_translations(self.hass)
        status = texts.get(
            f"common.morning_status_{report['status']}",
            texts["common.morning_status_unknown"],
        )
        base = report.get("baseline", {})
        candidate = report.get("candidate", {})

        def metric(values: dict[str, Any], key: str) -> str:
            value = values.get(key)
            return f"{value:.3f}" if isinstance(value, int | float) else "—"

        return self.async_show_form(
            step_id="morning_status",
            data_schema=vol.Schema({}),
            description_placeholders={
                "status": status,
                "applied": texts[
                    (
                        "common.morning_yes"
                        if report.get("applied")
                        else "common.morning_no"
                    )
                ],
                "training": str(report.get("training_days", 0)),
                "validation": str(report.get("validation_days", 0)),
                "baseline_time": metric(base, "time_mae_minutes"),
                "candidate_time": metric(candidate, "time_mae_minutes"),
                "baseline_energy": metric(base, "morning_mae_kwh"),
                "candidate_energy": metric(candidate, "morning_mae_kwh"),
                "false_rise": metric(candidate, "false_rise"),
                "missing_rise": metric(candidate, "missed_rise"),
            },
        )

    async def async_step_morning_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._check_options_unchanged()
        errors = {}
        if user_input is not None and user_input.get("confirm"):
            from .morning_runtime import async_delete_morning_data

            try:
                await async_delete_morning_data(self.hass, self.config_entry)
            except HomeAssistantError:
                errors["base"] = "calibration_reset_failed"
            else:
                return await self.async_step_morning()
        return self.async_show_form(
            step_id="morning_reset",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
        )
