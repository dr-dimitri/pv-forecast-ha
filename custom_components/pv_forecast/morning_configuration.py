"""Freiwilligen Morgenvergleich in den administrativen Optionen verwalten."""

from __future__ import annotations

from math import isfinite
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .calibration_configuration import _has_energy_source
from .const import CONF_MORNING_MODE

_MODES = ("off", "observe", "auto")


def _format_morning_status(report: dict[str, Any], translations: dict[str, str]) -> str:
    """Nur berechnete Nachweise ausgeben; fehlende Metriken bleiben unbekannt."""

    def translated(group: str, value: object) -> str:
        return translations.get(
            f"common.morning_{group}_{value}",
            translations[f"common.morning_{group}_unknown"],
        )

    def number(value: object, digits: int = 3) -> str:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return translations["common.morning_missing"]
        try:
            if isfinite(value):
                return f"{value:.{digits}f}"
        except OverflowError:
            pass
        return translations["common.morning_missing"]

    metrics = report.get("metrics") or {}
    lines = [
        translations["common.morning_status_report"].format(
            mode=translations.get(
                f"selector.morning_mode.options.{report.get('mode', 'off')}",
                translations["common.morning_missing"],
            ),
            status=translated("status", report.get("status")),
            effective_factor=number(report.get("effective_factor")),
            candidate_factor=number(report.get("candidate_factor")),
            training_days=report.get("training_days", 0),
            validation_days=report.get("validation_days", 0),
        ),
        translations["common.morning_metrics_report"].format(
            **{
                key: number(metrics.get(key))
                for variant in ("raw", "baseline", "candidate")
                for metric in (
                    "morning_mae_kwh",
                    "rise_mae_minutes",
                    "day_mae_kwh",
                    "false_rise_rate",
                    "early_p90_minutes",
                )
                for key in (f"{variant}_{metric}",)
            }
        ),
        translations["common.morning_cases_report"].format(
            sample_days=number(metrics.get("sample_days"), 0),
            paired_rise_days=number(metrics.get("paired_rise_days"), 0),
            **{
                key: number(metrics.get(key), 0)
                for variant in ("raw", "baseline", "candidate")
                for metric in ("false_rises", "missed_rises", "neither_rises")
                for key in (f"{variant}_{metric}",)
            },
        ),
    ]
    if reasons := report.get("reasons"):
        lines.append(
            translations["common.morning_reasons"].format(
                reasons="; ".join(translated("reason", reason) for reason in reasons)
            )
        )
    if exclusions := report.get("exclusion_reasons"):
        lines.append(
            translations["common.morning_exclusions"].format(
                exclusions="; ".join(
                    f"{translated('reason', reason)}: {count}"
                    for reason, count in exclusions.items()
                )
            )
        )
    if report.get("retention_truncated"):
        lines.append(translations["common.morning_retention_truncated"])
    return "\n\n".join(lines)


class MorningFlowMixin:
    """Eigenständige Morgenfreigabe ohne automatische Aktivierung anbieten."""

    async def async_step_morning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Moduswahl, Status und bewusstes Rücksetzen gemeinsam anbieten."""
        from .config_flow import _async_ui_translations

        options = self._measurement_options()
        translations = await _async_ui_translations(self.hass)
        return self.async_show_menu(
            step_id="morning",
            menu_options=[
                "morning_settings",
                "morning_status",
                "reset_morning",
                "morning_done",
            ],
            description_placeholders={
                "mode": translations.get(
                    "selector.morning_mode.options."
                    + str(options.get(CONF_MORNING_MODE, "off")),
                    translations["selector.morning_mode.options.off"],
                ),
                "timezone": self._measurement_timezone(),
            },
        )

    async def async_step_morning_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nur bestätigte AC-Energiequellen und ein aktives Archiv zulassen."""
        options = self._measurement_options()
        errors = {}
        if user_input is not None:
            mode = user_input[CONF_MORNING_MODE]
            if mode != "off" and options.get("history_enabled") is not True:
                errors["base"] = "morning_history_required"
            elif mode != "off" and not _has_energy_source(options):
                errors["base"] = "morning_energy_required"
            else:
                options[CONF_MORNING_MODE] = mode
                return await self.async_step_morning()
        return self.async_show_form(
            step_id="morning_settings",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MORNING_MODE,
                        default=(user_input or options).get(CONF_MORNING_MODE, "off"),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=list(_MODES),
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="morning_mode",
                        )
                    )
                }
            ),
        )

    async def async_step_morning_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Vorhandenen Morgenstatus lesen, ohne zu lernen oder Wetter abzurufen."""
        from .config_flow import _async_ui_translations

        self._check_options_unchanged()
        if user_input is not None:
            return await self.async_step_morning()
        manager = getattr(
            getattr(self.config_entry, "runtime_data", None), "history", None
        )
        translations = await _async_ui_translations(self.hass)
        return self.async_show_form(
            step_id="morning_status",
            data_schema=vol.Schema({}),
            errors={} if manager is not None else {"base": "morning_unavailable"},
            description_placeholders={
                "report": (
                    _format_morning_status(
                        manager.morning_snapshot(dt_util.utcnow()), translations
                    )
                    if manager is not None
                    else translations["common.morning_unavailable"]
                )
            },
        )

    async def async_step_reset_morning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nur ausdrückliche Bestätigung setzt den eigenen Morgenversuch zurück."""
        self._check_options_unchanged()
        errors = {}
        if user_input is not None:
            if user_input.get("confirm_reset") is not True:
                return await self.async_step_morning()
            manager = getattr(
                getattr(self.config_entry, "runtime_data", None), "history", None
            )
            if manager is None:
                errors["base"] = "morning_unavailable"
            else:
                try:
                    await manager.async_reset_morning()
                except HomeAssistantError:
                    errors["base"] = "morning_reset_failed"
                else:
                    return await self.async_step_morning()
        return self.async_show_form(
            step_id="reset_morning",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("confirm_reset", default=False): BooleanSelector()}
            ),
        )

    async def async_step_morning_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Morgenmodus gemeinsam mit allen unabhängigen Anwenderwerten speichern."""
        return self.async_create_entry(title="", data=self._measurement_options())
