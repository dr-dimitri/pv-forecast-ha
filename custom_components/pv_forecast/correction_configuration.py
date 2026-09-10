"""Administrative Eingabe bestätigter Tageserträge im nativen Options Flow."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .history_corrections import (
    correction_group,
    correction_groups,
    correction_revision,
)

if TYPE_CHECKING:
    from .history import ArchiveRecord, Assessment
    from .history_runtime import ArchiveManager


class CorrectionFlowMixin:
    """Archivdaten unmittelbar speichern, ohne die Anlagenoptionen zu verändern."""

    _correction_record_id: str | None = None
    _correction_revision: str | None = None
    _correction_manager: ArchiveManager | None = None
    _correction_date: str | None = None

    def _day_correction_manager(self) -> ArchiveManager:
        self._check_options_unchanged()
        manager = getattr(
            getattr(self.config_entry, "runtime_data", None), "history", None
        )
        if (
            manager is None
            or not manager.loaded
            or manager._storage_error
            or manager._stopped
        ):
            raise HomeAssistantError("Das Prognosearchiv ist nicht verfügbar")
        return manager

    async def async_step_daily_correction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Einen abgeschlossenen lokalen Archivtag bewusst auswählen."""
        errors = {}
        if user_input is not None:
            try:
                self._correction_manager = self._day_correction_manager()
                self._correction_date = date.fromisoformat(
                    user_input["date"]
                ).isoformat()
                groups = [
                    g
                    for g in correction_groups(
                        self._correction_manager._archive, dt_util.utcnow()
                    )
                    if g[0].target_date.isoformat() == self._correction_date
                ]
                if not groups:
                    errors["base"] = "daily_correction_unavailable"
                elif len(groups) == 1:
                    self._select_correction(groups[0])
                    return await self.async_step_daily_correction_edit()
                else:
                    return await self.async_step_daily_correction_context()
            except HomeAssistantError:
                errors["base"] = "history_unavailable"
            except (ValueError, TypeError):
                errors["base"] = "daily_correction_invalid"
        today = (
            dt_util.utcnow()
            .astimezone(ZoneInfo(self.config_entry.data["time_zone"]))
            .date()
        )
        return self.async_show_form(
            step_id="daily_correction",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "date",
                        default=(user_input or {}).get(
                            "date", (today - timedelta(days=1)).isoformat()
                        ),
                    ): DateSelector()
                }
            ),
        )

    def _select_correction(self, group: tuple[ArchiveRecord, ...]) -> None:
        self._correction_record_id = group[0].record_id
        self._correction_revision = correction_revision(group)

    async def async_step_daily_correction_context(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Alte physische Messgrenzen bei mehrdeutigem Datum ausdrücklich auswählen."""
        try:
            manager = self._day_correction_manager()
        except HomeAssistantError:
            return self.async_abort(reason="history_unavailable")
        groups = [
            g
            for g in correction_groups(manager._archive, dt_util.utcnow())
            if g[0].target_date.isoformat() == self._correction_date
        ]
        if not groups or manager is not self._correction_manager:
            return self.async_abort(reason="daily_correction_changed")
        if user_input is not None:
            selected = next(
                (g for g in groups if g[0].record_id == user_input.get("context")), None
            )
            if selected is None:
                return self.async_abort(reason="daily_correction_changed")
            self._select_correction(selected)
            return await self.async_step_daily_correction_edit()
        return self.async_show_form(
            step_id="daily_correction_context",
            data_schema=vol.Schema(
                {
                    vol.Required("context"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=g[0].record_id,
                                    label=(
                                        f"{g[0].timezone} · "
                                        f"{g[0].configuration_id[:12]} · "
                                        + ", ".join(
                                            s.scope for s in g[0].measurement_sources
                                        )
                                    ),
                                )
                                for g in groups
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_daily_correction_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Original und Korrektur zeigen; erst die Bestätigung schreibt."""
        from .config_flow import _async_ui_translations

        errors = {}
        try:
            manager = self._day_correction_manager()
            if (
                manager is not self._correction_manager
                or self._correction_record_id is None
                or self._correction_revision is None
            ):
                return self.async_abort(reason="daily_correction_changed")
            group = correction_group(
                manager._archive, self._correction_record_id, dt_util.utcnow()
            )
            if correction_revision(group) != self._correction_revision:
                return self.async_abort(reason="daily_correction_changed")
        except (HomeAssistantError, ValueError):
            return self.async_abort(reason="history_unavailable")
        if user_input is not None:
            if user_input.get("confirm") is not True:
                errors["base"] = "daily_correction_confirmation_required"
            elif not user_input.get("remove") and "energy_kwh" not in user_input:
                errors["base"] = "daily_correction_invalid"
            else:
                try:
                    await manager.async_correct_day(
                        self._correction_record_id,
                        None if user_input.get("remove") else user_input["energy_kwh"],
                        expected_revision=self._correction_revision,
                    )
                except HomeAssistantError:
                    errors["base"] = "daily_correction_save_failed"
                    try:
                        group = correction_group(
                            manager._archive,
                            self._correction_record_id,
                            dt_util.utcnow(),
                        )
                    except ValueError:
                        return self.async_abort(reason="daily_correction_changed")
                    self._correction_revision = correction_revision(group)
                except ValueError as err:
                    errors["base"] = str(err)
                else:
                    return self.async_show_form(
                        step_id="daily_correction_saved", data_schema=vol.Schema({})
                    )
        record = group[0]
        assessment = record.assessment
        manual = bool(assessment and assessment.manual)
        measured = record.measured_assessment if manual else assessment
        translations = await _async_ui_translations(self.hass)
        missing = translations["common.daily_correction_missing"]

        def value(item: Assessment | None) -> str:
            return (
                str(item.actual_energy_kwh) + " kWh" if item and item.valid else missing
            )

        suggested = dict(user_input or {})
        if "energy_kwh" not in suggested and assessment and assessment.valid:
            suggested["energy_kwh"] = assessment.actual_energy_kwh
        return self.async_show_form(
            step_id="daily_correction_edit",
            errors=errors,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional("energy_kwh"): NumberSelector(
                            NumberSelectorConfig(
                                min=0,
                                step="any",
                                mode=NumberSelectorMode.BOX,
                                unit_of_measurement="kWh",
                            )
                        ),
                        vol.Required("remove", default=False): BooleanSelector(),
                        vol.Required("confirm", default=False): BooleanSelector(),
                    }
                ),
                suggested,
            ),
            description_placeholders={
                "date": record.target_date.isoformat(),
                "timezone": record.timezone,
                "scope": ", ".join(s.scope for s in record.measurement_sources),
                "measured": value(measured),
                "corrected": value(assessment) if manual else missing,
            },
        )

    async def async_step_daily_correction_saved(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Weitere Korrekturen erlauben, ohne einen fachlichen Reload auszulösen."""
        return await self.async_step_init()
