"""Opt-in, Vergleichsquellen und native Berichte für das lokale Prognosearchiv."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .measurements import normalize_reading_value

CONF_HISTORY_ENABLED = "history_enabled"
CONF_COMPARISON_FORECAST = "comparison_forecast"

_HORIZONS = {
    "daily_previous_18": "Tagesprognose vom Vortag, 18 Uhr",
    "daily_same_06": "Tagesprognose vom Zieltag, 06 Uhr",
    "hourly_1h": "Stundenprognose mit einer Stunde Vorlauf",
    "hourly_3h": "Stundenprognose mit drei Stunden Vorlauf",
}

_EXCLUSIONS = {
    "forecast_missing": "Rechtzeitige Prognose fehlt",
    "target_not_finished": "Zielintervall noch nicht abgeschlossen",
    "measurement_source_deleted": "Messkopie der Quelle gelöscht",
    "not_assessed": "Noch nicht bewertet",
    "configuration_changed": "Anlagenkonfiguration geändert",
    "no_measurement_sources": "Keine Messquelle zugeordnet",
    "measurements_missing": "Messwerte fehlen",
    "duplicate_measurement_sources": "Messquellen überschneiden sich",
    "measurement_source_missing": "Benötigte Messquelle fehlt",
    "measurement_identity_unresolved": "Identität der Messquelle ungeklärt",
    "measurement_incomplete": "Messzeitraum unvollständig",
    "measurement_boundary_changed": "Messgrenze geändert",
    "measurement_evidence_invalid": "Messnachweis ungültig",
    "arithmetic_overflow": "Zahlenbereich überschritten",
}


def _comparison_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Vorhandene Tagesprognosen anbieten, ohne Zählerklassen zu verlangen."""

    selector = EntitySelector(
        EntitySelectorConfig(filter={"domain": "sensor", "device_class": "energy"})
    )
    return vol.Schema(
        {
            vol.Required(
                "today_entity_id", default=defaults.get("today_entity_id", "")
            ): selector,
            vol.Required(
                "tomorrow_entity_id", default=defaults.get("tomorrow_entity_id", "")
            ): selector,
            vol.Required("scope", default=defaults.get("scope", "")): TextSelector(),
        }
    )


def _validated_comparison(
    hass: HomeAssistant, values: dict[str, Any]
) -> dict[str, Any]:
    """Nur unterschiedliche fremde Energie-Entities als Vergleich akzeptieren."""

    scope = str(values.get("scope", "")).strip()
    if not scope:
        raise ValueError("invalid_comparison_scope")
    result: dict[str, Any] = {"scope": scope, "confirmed_same_boundary": True}
    registry = er.async_get(hass)
    identities: set[str] = set()
    for day in ("today", "tomorrow"):
        entity_id = str(values.get(f"{day}_entity_id", ""))
        state = hass.states.get(entity_id)
        registered = registry.async_get(entity_id)
        if (
            state is None
            or not entity_id.startswith("sensor.")
            or state.attributes.get("device_class") != "energy"
            or state.attributes.get("unit_of_measurement") not in ("Wh", "kWh")
            or (registered is not None and registered.platform == DOMAIN)
        ):
            raise ValueError("invalid_comparison_source")
        identity = registered.id if registered is not None else entity_id
        if identity in identities:
            raise ValueError("duplicate_comparison_source")
        identities.add(identity)
        result[f"{day}_entity_id"] = entity_id
        result[f"{day}_registry_id"] = registered.id if registered is not None else None
    return result


class HistoryFlowMixin:
    """Gemeinsame Archivschritte im Setup und im administrativen Options Flow."""

    hass: HomeAssistant
    _pending_comparison: dict[str, Any] | None = None

    def _history_options(self) -> dict[str, Any]:
        """Den gemeinsamen Entwurf bewahren, einschließlich aller Messquellen."""

        return self._measurement_options()

    def _history_entry(self) -> ConfigEntry | None:
        """Im ersten Setup gibt es noch kein gespeichertes Archiv."""

        return self._measurement_entry()

    def _comparison(self) -> dict[str, Any]:
        """Umbenannte Vergleichssensoren über ihre Registry-IDs wiederfinden."""

        comparison = dict(self._history_options().get(CONF_COMPARISON_FORECAST, {}))
        registry = er.async_get(self.hass)
        for day in ("today", "tomorrow"):
            registry_id = comparison.get(f"{day}_registry_id")
            if registry_id and (registered := registry.async_get(registry_id)):
                comparison[f"{day}_entity_id"] = registered.entity_id
        return comparison

    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Opt-in und Archivverwaltung ohne zusätzliche Wetterabrufe anbieten."""

        comparison = self._comparison()
        menu = {
            "history_settings": "Archivierung aktivieren oder pausieren",
            "comparison_forecast": "Vorhandene Tagesprognose zum Vergleich zuordnen",
        }
        if comparison:
            menu["remove_comparison_forecast"] = "Vergleichszuordnung entfernen"
        if self._history_entry() is not None:
            menu["history_report"] = "Soll-Ist-Bericht ansehen (7/30/90 Tage)"
            menu["delete_history"] = "Lokales Prognosearchiv löschen"
        menu["history_done"] = "Fertig"
        return self.async_show_menu(
            step_id="history",
            menu_options=menu,
            description_placeholders={
                "enabled": (
                    "Aktiviert"
                    if self._history_options().get(CONF_HISTORY_ENABLED, False)
                    else "Pausiert / noch nicht aktiviert"
                ),
                "comparison": (
                    f"{comparison['scope']} · {comparison['today_entity_id']} / "
                    f"{comparison['tomorrow_entity_id']}"
                    if comparison
                    else "Kein fremder Forecast zugeordnet"
                ),
            },
        )

    async def async_step_history_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erfassung ausdrücklich einschalten oder bei erhaltenen Daten pausieren."""

        if user_input is not None:
            self._history_options()[CONF_HISTORY_ENABLED] = bool(
                user_input.get(CONF_HISTORY_ENABLED)
            )
            self._history_options()["short_term_enabled"] = bool(
                user_input.get("short_term_enabled")
            )
            return await self.async_step_history()
        return self.async_show_form(
            step_id="history_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HISTORY_ENABLED,
                        default=self._history_options().get(
                            CONF_HISTORY_ENABLED, False
                        ),
                    ): BooleanSelector(),
                    vol.Optional(
                        "short_term_enabled",
                        default=self._history_options().get(
                            "short_term_enabled", False
                        ),
                    ): BooleanSelector(),
                }
            ),
        )

    async def async_step_comparison_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Zwei vorhandene Prognosesensoren und ihre gemeinsame Messgrenze erfassen."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._pending_comparison = _validated_comparison(self.hass, user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return await self.async_step_confirm_comparison_forecast()
        return self.async_show_form(
            step_id="comparison_forecast",
            errors=errors,
            data_schema=_comparison_schema(user_input or self._comparison()),
        )

    def _comparison_preview(self, entity_id: str) -> str:
        """Einen vorhandenen Zustand mit Einheit und Zeitpunkt verständlich zeigen."""

        state = self.hass.states.get(entity_id)
        if state is None:
            return f"{entity_id}: derzeit nicht verfügbar"
        unit = state.attributes.get("unit_of_measurement")
        value, _ = normalize_reading_value(state.state, unit, "total")
        display = f"{value:g} kWh" if value is not None else "kein gültiger Wert"
        timezone = dt_util.get_time_zone(self._measurement_timezone())
        return (
            f"{state.name} (`{entity_id}`): {display}; Einheit {unit}; "
            f"letzte Meldung {state.last_reported.astimezone(timezone).isoformat()}"
        )

    async def async_step_confirm_comparison_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gleiche AC-Gesamtmessgrenze und lokale Zieltage ausdrücklich bestätigen."""

        comparison = self._pending_comparison
        assert comparison is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirmed_same_boundary") is True:
                self._history_options()[CONF_COMPARISON_FORECAST] = comparison
                self._pending_comparison = None
                return await self.async_step_history()
            errors["base"] = "comparison_confirmation_required"
        return self.async_show_form(
            step_id="confirm_comparison_forecast",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "confirmed_same_boundary", default=False
                    ): BooleanSelector()
                }
            ),
            description_placeholders={
                "scope": comparison["scope"],
                "timezone": self._measurement_timezone(),
                "today": self._comparison_preview(comparison["today_entity_id"]),
                "tomorrow": self._comparison_preview(comparison["tomorrow_entity_id"]),
            },
        )

    async def async_step_remove_comparison_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Künftigen Vergleich beenden; bereits archivierte Stände nicht umschreiben."""

        self._history_options().pop(CONF_COMPARISON_FORECAST, None)
        return await self.async_step_history()

    async def async_step_history_report(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ein festes Auswertungsfenster aus vorhandenen Archivdaten auswählen."""

        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._history_entry()
            manager = getattr(getattr(entry, "runtime_data", None), "history", None)
            if manager is None:
                errors["base"] = "history_unavailable"
            else:
                report = manager.snapshot(
                    days=int(user_input["days"]), now=dt_util.utcnow()
                )
                return self.async_show_form(
                    step_id="history_report_result",
                    data_schema=vol.Schema({}),
                    description_placeholders={"report": _format_report(report)},
                )
        return self.async_show_form(
            step_id="history_report",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("days", default="30"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=str(days), label=f"{days} abgeschlossene Tage"
                                )
                                for days in (7, 30, 90)
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_history_report_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Vom unveränderlichen Berichtsstand zurück zur Archivverwaltung gehen."""

        return await self.async_step_history()

    async def async_step_delete_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Archivdaten erst nach ausdrücklicher Bestätigung im Admin-Flow löschen."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm_delete") is True:
                if (entry := self._history_entry()) is not None:
                    from .history_runtime import async_delete_history_data

                    try:
                        await async_delete_history_data(self.hass, entry)
                    except HomeAssistantError:
                        errors["base"] = "history_delete_failed"
                    else:
                        return await self.async_step_history()
            else:
                return await self.async_step_history()
        return self.async_show_form(
            step_id="delete_history",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("confirm_delete", default=False): BooleanSelector()}
            ),
        )


def _format_report(report: dict[str, Any]) -> str:
    """Berechnete Kennzahlen darstellen; keine eigene Bewertung im UI durchführen."""

    lines = [
        f"**{report['window_days']} abgeschlossene Tage · {report['timezone']}**",
        f"Zeitraum: {report['window_start']} bis vor "
        f"{report['window_end_exclusive']}",
        "Erfassung: " + ("aktiviert" if report.get("enabled") else "pausiert"),
    ]
    if report.get("storage_error"):
        lines.append(
            "**Archivspeicher nicht lesbar.** Vorhandene Dateien bleiben erhalten; "
            "der Bericht enthält dadurch keinen verlässlichen vollständigen Bestand."
        )
    if report.get("retention_truncated"):
        lines.append(
            "**Speichergrenze erreicht:** Ältere Archivstände wurden gekürzt. "
            "Fehlende Zielintervalle bleiben in der Abdeckung sichtbar."
        )
    for horizon, label in _HORIZONS.items():
        metrics = report["horizons"][horizon]
        coverage = metrics.get("coverage")
        coverage_text = "—" if coverage is None else f"{coverage:.1%}"
        lines.extend(
            [
                f"**{label}**",
                f"Gültige Messpaare: {metrics['count_valid']} / "
                f"{metrics['count_expected']} · Rechtzeitige Prognosen: "
                f"{metrics['count_forecasts']} · Abdeckung: {coverage_text}",
                f"MAE: {_metric(metrics['mae_kwh'])} · "
                f"Bias: {_metric(metrics['bias_kwh'], signed=True)}",
            ]
        )
        reasons = metrics.get("exclusion_reasons", {})
        if reasons:
            lines.append(
                "Ausschlüsse: "
                + "; ".join(
                    f"{_EXCLUSIONS.get(reason, 'Andere dokumentierte Datenlücke')}: "
                    f"{count}"
                    for reason, count in reasons.items()
                )
            )
        comparison = metrics.get("existing_comparison", {})
        if comparison.get("count", 0):
            lines.extend(
                [
                    f"Fremdvergleich, dieselben {comparison['count']} Messpaare: "
                    f"eigene MAE {_metric(comparison['raw_mae_kwh'])}, "
                    f"fremde MAE {_metric(comparison['existing_mae_kwh'])}; "
                    f"eigener Bias {_metric(comparison['raw_bias_kwh'], signed=True)}, "
                    "fremder Bias "
                    f"{_metric(comparison['existing_bias_kwh'], signed=True)}.",
                    "Mittleres Datenalter am Stichtag: eigene Prognose "
                    f"{_age(comparison['mean_own_age_seconds'])}, "
                    f"fremde Prognose {_age(comparison['mean_existing_age_seconds'])}.",
                ]
            )
        calibrated = metrics.get("calibrated_comparison") or {}
        if calibrated.get("count", 0):
            lines.append(
                f"Angewendete Kalibrierung, dieselben {calibrated['count']} Messpaare: "
                f"Roh-MAE {_metric(calibrated['raw_mae_kwh'])}, "
                f"korrigierte MAE {_metric(calibrated['calibrated_mae_kwh'])}."
            )
    lines.append(
        "Vergleiche erscheinen nur bei gemeinsamen gültigen Messpaaren. "
        "Reine Testkandidaten zählen nicht als angewendete Kalibrierung."
    )
    return "\n\n".join(lines)


def _metric(value: float | None, *, signed: bool = False) -> str:
    """Fehlende Stichproben nicht als Nullfehler darstellen."""

    if value is None:
        return "noch keine gültige Stichprobe"
    return f"{value:+.3f} kWh" if signed else f"{value:.3f} kWh"


def _age(value: float | None) -> str:
    """Erfasstes Datenalter lesbar darstellen, ohne Modelllaufzeiten zu erfinden."""

    return "unbekannt" if value is None else f"{value / 60:.1f} Minuten"
