"""Optionale Selbstkalibrierung und ausdrücklich bekannte lokale Ausnahmen."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .measurements import SourceConfig

CONF_CALIBRATION_MODE = "calibration_mode"
CONF_CALIBRATION_EXCLUSIONS = "calibration_exclusions"

_MODES = {
    "off": "Aus",
    "observe": "Beobachten, ohne die Prognose zu verändern",
    "auto": "Nach erfolgreicher Prüfung automatisch anwenden",
}
_EXCLUSIONS = {"curtailment": "Bekannte Abregelung", "maintenance": "Wartung"}
_STATUSES = {
    "off": "Ausgeschaltet",
    "disabled": "Ausgeschaltet",
    "learning": "Lerntage werden gesammelt",
    "collecting": "Lerntage werden gesammelt",
    "validating": "Kandidat wird an späteren Tagen geprüft",
    "testing": "Kandidat wird an späteren Tagen geprüft",
    "approved": "Nutzenprüfung bestanden",
    "active": "Geprüfter Faktor wird angewendet",
    "rejected": "Kein ausreichender Nutzen belegt",
    "expired": "Prüfzeitraum ohne ausreichenden Nachweis beendet",
    "waiting": "Weitere geeignete Daten erforderlich",
    "invalidated": "Frühere Freigabe durch geänderte Daten aufgehoben",
    "blocked": "Voraussetzungen derzeit nicht erfüllt",
    "prerequisites_missing": "Voraussetzungen derzeit nicht erfüllt",
    "storage_unavailable": "Lernspeicher derzeit nicht verfügbar",
}
_REASONS = {
    "history_disabled": "Das Prognosearchiv ist nicht aktiviert",
    "no_measurement_sources": "Keine bestätigte Energiemessquelle zugeordnet",
    "no_energy_sources": "Keine bestätigte Energiemessquelle zugeordnet",
    "insufficient_training_days": "Noch keine 30 geeigneten Lerntage vorhanden",
    "insufficient_validation_days": "Noch keine 14 späteren Prüftage vorhanden",
    "measurement_incomplete": "Die Messung deckt den Tag nicht vollständig ab",
    "measurement_boundary_changed": "Die bestätigte Messgrenze wurde geändert",
    "configuration_changed": "Die physische Anlagenkonfiguration wurde geändert",
    "measurement_source_deleted": "Benötigte Messbelege wurden gelöscht",
    "assessment_changed": "Ein verwendeter Messnachweis wurde korrigiert",
    "training_evidence_changed": "Ein verwendeter Lernnachweis wurde geändert",
    "validation_evidence_changed": "Ein verwendeter Prüfnachweis wurde geändert",
    "basis_missing": "Für ältere Prognosen fehlt die ungekürzte Lernbasis",
    "forecast_missing": "Eine rechtzeitig gespeicherte Prognose fehlt",
    "candidate_forecast_missing": "Eine rechtzeitige Kandidatenprognose fehlt",
    "low_energy": "Die ungekürzte Tagesenergie ist für das Lernen zu klein",
    "strong_clipping": "Die bekannte AC-Begrenzung kürzt den Lerntag zu stark",
    "curtailment": "Der Tag ist als bekannte Abregelung markiert",
    "maintenance": "Der Tag ist als Wartung markiert",
    "insufficient_improvement": "Die MAE-Verbesserung erreicht nicht 5 Prozent",
    "large_errors_worsened": "Große Tagesfehler haben sich zu stark verschlechtert",
    "validation_expired": "Der Prüfzeitraum ist ohne ausreichenden Nachweis abgelaufen",
    "raw_mae_zero": "Das Rohmodell hat in dieser Stichprobe keinen MAE-Fehler",
    "cooldown": "Der nächste Lernversuch wartet die vorgeschriebene Pause ab",
    "storage_error": "Der lokale Lernspeicher ist nicht lesbar",
    "missing_basis": "Für ältere Prognosen fehlt die ungekürzte Lernbasis",
    "measurements_incomplete": "Die Messung deckt den Tag nicht vollständig ab",
    "weather_quality": "Die Wetterdaten sind für das Lernen nicht ausreichend belegt",
    "known_curtailment": "Der Tag ist als bekannte Abregelung oder Wartung markiert",
    "insufficient_energy": "Die ungekürzte Tagesenergie ist für das Lernen zu klein",
    "extreme_residual": "Starke ungeklärte Abweichung; dadurch allein kein Ausschluss",
    "large_errors_worse": "Große Tagesfehler haben sich zu stark verschlechtert",
    "evidence_changed": "Ein verwendeter Messnachweis wurde geändert",
    "prerequisites_missing": "Archiv oder bestätigte Energiequelle fehlt",
    "storage_unavailable": "Der lokale Lernspeicher ist nicht lesbar",
}


def _selector(labels: dict[str, str]) -> SelectSelector:
    """Kleine Auswahllisten mit verständlichen deutschen Bezeichnungen erzeugen."""

    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=value, label=label)
                for value, label in labels.items()
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _has_energy_source(options: dict[str, Any]) -> bool:
    """Nur vollständig bestätigte Energiezuordnungen erfüllen die Voraussetzung."""

    for item in options.get("measurement_sources", ()):
        try:
            source = SourceConfig.from_dict(item)
        except (ValueError, TypeError):
            continue
        if source.kind in ("total", "daily"):
            return True
    return False


def _number(value: object, unit: str = "") -> str:
    """Fehlende Nachweise nicht als Nullwert darstellen."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return "noch kein Nachweis"
    try:
        if not isfinite(value):
            return "noch kein Nachweis"
    except OverflowError:
        return "noch kein Nachweis"
    return f"{value:.3f}{unit}"


def _format_status(report: dict[str, Any], timezone: str) -> str:
    """Ausschließlich vom Lernmanager berechnete Werte verständlich darstellen."""

    learned = report.get("last_learned_at")
    try:
        instant = (
            learned
            if isinstance(learned, datetime)
            else datetime.fromisoformat(learned)
        )
        learned_text = (
            instant.astimezone(ZoneInfo(timezone)).isoformat()
            if instant.utcoffset() is not None
            else "noch kein Lernzeitpunkt"
        )
    except (TypeError, ValueError):
        learned_text = "noch kein Lernzeitpunkt"
    lines = [
        f"**Laufender Modus:** {_MODES.get(report.get('mode'), 'Unbekannt')}",
        "**Zustand:** "
        + _STATUSES.get(report.get("status"), "Weitere geeignete Daten erforderlich"),
        f"**Wirksamer Faktor:** {_number(report.get('effective_factor'))}",
        f"**Kandidatenfaktor:** {_number(report.get('candidate_factor'))}",
        f"**Lerntage:** {report.get('training_days', 0)} / 30 · "
        f"**Prüftage:** {report.get('validation_days', 0)} / 14",
        f"**Letzter Lernzeitpunkt:** {learned_text}",
        f"**Tages-MAE des Rohmodells:** {_number(report.get('raw_mae_kwh'), ' kWh')}",
        "**Tages-MAE des Kandidaten:** "
        + _number(report.get("calibrated_mae_kwh"), " kWh"),
        "**MAE-Verbesserung in derselben Prüfstichprobe:** "
        + _number(report.get("improvement_percent"), " %"),
    ]
    if report.get("storage_error"):
        lines.append(
            "**Lernspeicher nicht lesbar:** Es liegt keine verlässliche Freigabe vor."
        )
    if reasons := report.get("reasons"):
        lines.append(
            "**Gründe:** "
            + "; ".join(
                _REASONS.get(
                    reason, "Weitere dokumentierte Voraussetzung nicht erfüllt"
                )
                for reason in reasons
            )
        )
    if exclusions := report.get("exclusion_reasons"):
        lines.append(
            "**Datenbasis und Markierungen:** "
            + "; ".join(
                f"{_REASONS.get(reason, 'Weitere dokumentierte Datenlücke')}: {count}"
                for reason, count in exclusions.items()
            )
        )
    return "\n\n".join(lines)


class CalibrationFlowMixin:
    """Selbstkalibrierung ausschließlich in den administrativen Optionen verwalten."""

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Modus, vorhandenen Status und bewusste Ausnahmen anbieten."""

        options = self._measurement_options()
        menu = {
            "calibration_settings": "Modus auswählen",
            "calibration_status": "Lern- und Prüfstatus ansehen",
            "calibration_exclusion": "Bekannte Abregelung oder Wartung markieren",
        }
        if self._calibration_exclusions():
            menu["calibration_remove_exclusion"] = "Tagesmarkierung zurücknehmen"
        menu["reset_calibration"] = "Lernzustand zurücksetzen"
        menu["calibration_done"] = "Fertig"
        return self.async_show_menu(
            step_id="calibration",
            menu_options=menu,
            description_placeholders={
                "mode": _MODES.get(options.get(CONF_CALIBRATION_MODE, "off"), "Aus"),
                "timezone": self._measurement_timezone(),
                "exclusions": str(len(self._calibration_exclusions())),
            },
        )

    async def async_step_calibration_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Beobachtung und Automatik brauchen ausdrücklich eingerichtete Grundlagen."""

        options = self._measurement_options()
        errors: dict[str, str] = {}
        if user_input is not None:
            mode = user_input[CONF_CALIBRATION_MODE]
            if mode != "off" and options.get("history_enabled") is not True:
                errors["base"] = "calibration_history_required"
            elif mode != "off" and not _has_energy_source(options):
                errors["base"] = "calibration_energy_required"
            else:
                options[CONF_CALIBRATION_MODE] = mode
                return await self.async_step_calibration()
        return self.async_show_form(
            step_id="calibration_settings",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CALIBRATION_MODE,
                        default=(user_input or options).get(
                            CONF_CALIBRATION_MODE, "off"
                        ),
                    ): _selector(_MODES)
                }
            ),
        )

    async def async_step_calibration_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Einen unveränderlichen Statusstand lesen; keine Lernberechnung auslösen."""

        if user_input is not None:
            return await self.async_step_calibration()
        manager = getattr(
            getattr(self.config_entry, "runtime_data", None), "calibration", None
        )
        return self.async_show_form(
            step_id="calibration_status",
            data_schema=vol.Schema({}),
            errors={} if manager is not None else {"base": "calibration_unavailable"},
            description_placeholders={
                "timezone": self._measurement_timezone(),
                "report": (
                    _format_status(manager.snapshot(), self._measurement_timezone())
                    if manager is not None
                    else "Für diese Anlage ist derzeit kein Lernstatus geladen."
                ),
            },
        )

    def _calibration_today(self) -> date:
        """Tagesmarkierungen an der gespeicherten Anlagenzeitzone ausrichten."""

        return (
            dt_util.utcnow().astimezone(ZoneInfo(self._measurement_timezone())).date()
        )

    def _calibration_exclusions(self) -> list[dict[str, str]]:
        """Nur gültige Markierungen des zurückliegenden lokalen Jahres anbieten."""

        today = self._calibration_today()
        first = today - timedelta(days=364)
        result = []
        for item in self._measurement_options().get(CONF_CALIBRATION_EXCLUSIONS, ()):
            try:
                day = date.fromisoformat(item["date"])
                if first <= day <= today and item["reason"] in _EXCLUSIONS:
                    result.append(dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(result, key=lambda item: item["date"])

    async def async_step_calibration_exclusion(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine ausdrücklich bekannte Ursache für genau einen lokalen Tag speichern."""

        errors: dict[str, str] = {}
        today = self._calibration_today()
        if user_input is not None:
            day = date.fromisoformat(user_input["date"])
            items = self._calibration_exclusions()
            same_day = [item for item in items if item["date"] == day.isoformat()]
            if not today - timedelta(days=364) <= day <= today:
                errors["base"] = "calibration_exclusion_date"
            elif not same_day and len(items) >= 90:
                errors["base"] = "calibration_exclusion_limit"
            else:
                remaining = [item for item in items if item["date"] != day.isoformat()]
                remaining.append(
                    {"date": day.isoformat(), "reason": user_input["reason"]}
                )
                self._measurement_options()[CONF_CALIBRATION_EXCLUSIONS] = sorted(
                    remaining, key=lambda item: item["date"]
                )
                return await self.async_step_calibration()
        return self.async_show_form(
            step_id="calibration_exclusion",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "date",
                        default=(user_input or {}).get("date", today.isoformat()),
                    ): DateSelector(),
                    vol.Required(
                        "reason",
                        default=(user_input or {}).get("reason", "curtailment"),
                    ): _selector(_EXCLUSIONS),
                }
            ),
            description_placeholders={"timezone": self._measurement_timezone()},
        )

    async def async_step_calibration_remove_exclusion(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine Tagesmarkierung ohne neue Messwerte zurücknehmen."""

        items = self._calibration_exclusions()
        if user_input is not None:
            self._measurement_options()[CONF_CALIBRATION_EXCLUSIONS] = [
                item for item in items if item["date"] != user_input["date"]
            ]
            return await self.async_step_calibration()
        if not items:
            return await self.async_step_calibration()
        return self.async_show_form(
            step_id="calibration_remove_exclusion",
            data_schema=vol.Schema(
                {
                    vol.Required("date"): _selector(
                        {
                            item[
                                "date"
                            ]: f"{item['date']} · {_EXCLUSIONS[item['reason']]}"
                            for item in items
                        }
                    )
                }
            ),
            description_placeholders={"timezone": self._measurement_timezone()},
        )

    async def async_step_reset_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erst die bewusste Bestätigung setzt den Lernzustand sofort zurück."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm_reset") is not True:
                return await self.async_step_calibration()
            from .calibration_runtime import async_delete_calibration_data

            try:
                await async_delete_calibration_data(self.hass, self.config_entry)
            except HomeAssistantError:
                errors["base"] = "calibration_reset_failed"
            else:
                return await self.async_step_calibration()
        return self.async_show_form(
            step_id="reset_calibration",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("confirm_reset", default=False): BooleanSelector()}
            ),
        )

    async def async_step_calibration_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Kalibrierung und alle unabhängigen Anwenderwerte gemeinsam speichern."""

        return self.async_create_entry(title="", data=self._measurement_options())
