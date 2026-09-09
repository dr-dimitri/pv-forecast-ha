"""Native Einrichtung und Verwaltung des optionalen Prognosearchivs."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous_serialize
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.history_configuration import (
    CONF_COMPARISON_FORECAST,
    CONF_HISTORY_ENABLED,
    _format_report,
    _validated_comparison,
)

from .helpers import persisted_roof
from .test_config_flow import ROOF_FORM

COMPARISON = {
    "today_entity_id": "sensor.other_today",
    "tomorrow_entity_id": "sensor.other_tomorrow",
    "scope": "Gesamte PV-Anlage am AC-Ausgang, ohne Batterie",
}
ATTRS = {"device_class": "energy", "unit_of_measurement": "Wh"}


def _entry(hass, *, enabled=False, comparison=None):
    """Eintrag mit unveränderten Dach- und Messquellenoptionen bereitstellen."""

    options = {
        "roofs": [persisted_roof()],
        "inverter_max_power_kw": 8,
        "measurement_sources": [],
        CONF_HISTORY_ENABLED: enabled,
    }
    if comparison is not None:
        options[CONF_COMPARISON_FORECAST] = comparison
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Prognose",
        unique_id=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "Europe/Berlin"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def _states(hass):
    """Bestehende Vorhersagesensoren brauchen ausdrücklich keine state_class."""

    hass.states.async_set(COMPARISON["today_entity_id"], "12500", ATTRS)
    hass.states.async_set(COMPARISON["tomorrow_entity_id"], "unknown", ATTRS)


async def _menu(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await _choose(hass, result, "history")


async def _choose(hass, result, action):
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": action}
    )


@pytest.mark.asyncio
async def test_setup_archive_is_optional_and_persists_independent_changes(hass):
    """Das Setup fragt kein Archiv ab, solange der Nutzer es nicht auswählt."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location_source": "home_assistant"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "summary"
        assert "history" in result["menu_options"]
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        assert CONF_HISTORY_ENABLED not in flow._options
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "history"}
        )
        assert "history_report" not in result["menu_options"]
        assert "delete_history" not in result["menu_options"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "history_settings"}
        )
        assert result["data_schema"]({}) == {
            CONF_HISTORY_ENABLED: False,
            "short_term_enabled": False,
        }
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HISTORY_ENABLED: True}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "history_done"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "edit_system"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"inverter_max_power_kw": 5}
        )
    with patch("custom_components.pv_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    assert result["options"][CONF_HISTORY_ENABLED] is True
    assert result["options"]["inverter_max_power_kw"] == 5
    assert result["version"] == 1 and result["minor_version"] == 1


@pytest.mark.asyncio
async def test_options_comparison_needs_confirmation_without_state_class(hass):
    """Vorhandene Forecasts erst nach expliziter Prüfung speichern."""

    _states(hass)
    entry = _entry(hass)
    original = dict(entry.options)
    result = await _choose(hass, await _menu(hass, entry), "comparison_forecast")
    assert voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], COMPARISON
    )
    assert result["step_id"] == "confirm_comparison_forecast"
    assert "12.5 kWh" in result["description_placeholders"]["today"]
    assert "kein gültiger Wert" in result["description_placeholders"]["tomorrow"]
    assert result["description_placeholders"]["timezone"] == "Europe/Berlin"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirmed_same_boundary": False}
    )
    assert result["errors"] == {"base": "comparison_confirmation_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirmed_same_boundary": True}
    )
    result = await _choose(hass, result, "history_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY_ENABLED: True}
    )
    result = await _choose(hass, result, "history_done")
    saved = result["data"][CONF_COMPARISON_FORECAST]
    assert saved == COMPARISON | {
        "today_registry_id": None,
        "tomorrow_registry_id": None,
        "confirmed_same_boundary": True,
    }
    assert result["data"]["roofs"] == original["roofs"]
    assert result["data"]["measurement_sources"] == original["measurement_sources"]
    assert result["data"]["inverter_max_power_kw"] == 8
    assert result["data"][CONF_HISTORY_ENABLED] is True


@pytest.mark.asyncio
async def test_registry_rename_preserves_comparison_identity(hass):
    """Entity-Rename ändert weder Messgrenze noch bestätigten Registry-Bezug."""

    registry = er.async_get(hass)
    today = registry.async_get_or_create("sensor", "other_forecast", "today")
    tomorrow = registry.async_get_or_create("sensor", "other_forecast", "tomorrow")
    for entity in (today, tomorrow):
        hass.states.async_set(entity.entity_id, "1", ATTRS)
    comparison = _validated_comparison(
        hass,
        COMPARISON
        | {
            "today_entity_id": today.entity_id,
            "tomorrow_entity_id": tomorrow.entity_id,
        },
    )
    entry = _entry(hass, comparison=comparison)
    renamed = registry.async_update_entity(
        today.entity_id, new_entity_id="sensor.renamed_today"
    )
    hass.states.async_set(renamed.entity_id, "2000", ATTRS)
    result = await _choose(hass, await _menu(hass, entry), "comparison_forecast")
    defaults = result["data_schema"]({})
    assert defaults["today_entity_id"] == renamed.entity_id
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], defaults
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirmed_same_boundary": True}
    )
    result = await _choose(hass, result, "history_done")
    assert result["data"][CONF_COMPARISON_FORECAST]["today_registry_id"] == today.id


@pytest.mark.parametrize(
    "override,error",
    [
        ({"scope": "   "}, "invalid_comparison_scope"),
        ({"today_entity_id": "sensor.missing"}, "invalid_comparison_source"),
        ({"tomorrow_entity_id": "sensor.other_today"}, "duplicate_comparison_source"),
    ],
)
@pytest.mark.asyncio
async def test_comparison_rejects_missing_duplicate_and_unclear_sources(
    hass, override, error
):
    """Fehlende Quellen und unklare Zuordnungen gelangen nicht in den Vergleich."""

    _states(hass)
    with pytest.raises(ValueError, match=error):
        _validated_comparison(hass, COMPARISON | override)


@pytest.mark.parametrize(
    "attrs",
    [
        ATTRS | {"unit_of_measurement": "kW"},
        ATTRS | {"device_class": "power"},
    ],
)
@pytest.mark.asyncio
async def test_comparison_rejects_wrong_metadata(hass, attrs):
    """Der Vergleich akzeptiert ausschließlich Energie in Wh oder kWh."""

    _states(hass)
    hass.states.async_set(COMPARISON["today_entity_id"], "1", attrs)
    with pytest.raises(ValueError, match="invalid_comparison_source"):
        _validated_comparison(hass, COMPARISON)


@pytest.mark.asyncio
async def test_comparison_rejects_own_forecast_entities(hass):
    """Die eigene Vorhersage wird nicht als unabhängiger Vergleich ausgegeben."""

    _states(hass)
    own = er.async_get(hass).async_get_or_create("sensor", DOMAIN, "own_forecast")
    hass.states.async_set(own.entity_id, "1234", ATTRS)
    with pytest.raises(ValueError, match="invalid_comparison_source"):
        _validated_comparison(hass, COMPARISON | {"today_entity_id": own.entity_id})


@pytest.mark.asyncio
async def test_pause_keeps_comparison_and_local_reports(hass):
    """Pausieren bewahrt vorhandene Vergleichszuordnungen."""

    _states(hass)
    comparison = _validated_comparison(hass, COMPARISON)
    entry = _entry(hass, enabled=True, comparison=comparison)
    result = await _choose(hass, await _menu(hass, entry), "history_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY_ENABLED: False}
    )
    result = await _choose(hass, result, "history_done")
    assert result["data"][CONF_HISTORY_ENABLED] is False
    assert result["data"][CONF_COMPARISON_FORECAST] == comparison


@pytest.mark.asyncio
async def test_remove_comparison_preserves_history_and_other_options(hass):
    """Eine Zuordnungsänderung löscht keine eingefrorenen Prognosestände."""

    _states(hass)
    entry = _entry(
        hass, enabled=True, comparison=_validated_comparison(hass, COMPARISON)
    )
    manager = Mock()
    entry.runtime_data = SimpleNamespace(history=manager)
    result = await _choose(hass, await _menu(hass, entry), "remove_comparison_forecast")
    result = await _choose(hass, result, "history_done")
    assert CONF_COMPARISON_FORECAST not in result["data"]
    assert result["data"][CONF_HISTORY_ENABLED] is True
    assert result["data"]["roofs"] == entry.options["roofs"]
    manager.async_delete_data.assert_not_called()


@pytest.mark.asyncio
async def test_history_report_unloaded_is_controlled(hass):
    """Ein Bericht ohne geladenen Manager erzeugt eine verständliche Formmeldung."""

    entry = _entry(hass)
    result = await _choose(hass, await _menu(hass, entry), "history_report")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"days": "7"}
    )
    assert result["errors"] == {"base": "history_unavailable"}


@pytest.mark.asyncio
async def test_delete_history_requires_confirmation_and_targets_only_entry(hass):
    """Eine bewusste Löschung betrifft nur das Archiv der gewählten Anlage."""

    entry = _entry(hass, enabled=True)
    result = await _choose(hass, await _menu(hass, entry), "delete_history")
    with patch(
        "custom_components.pv_forecast.history_runtime.async_delete_history_data",
        new_callable=AsyncMock,
    ) as delete:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm_delete": False}
        )
        delete.assert_not_called()
        result = await _choose(hass, result, "delete_history")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm_delete": True}
        )
        delete.assert_awaited_once_with(hass, entry)
    assert result["type"] is FlowResultType.MENU
    result = await _choose(hass, result, "history_done")
    assert result["data"] == dict(entry.options)


@pytest.mark.parametrize("days", [7, 30, 90])
@pytest.mark.asyncio
async def test_native_report_reads_selected_window_without_capture(hass, days):
    """Die native Ansicht liest vorhandene Daten und zeigt fehlende Paare ehrlich."""

    entry = _entry(hass, enabled=False)
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    report = HistoryArchive("Europe/Berlin").snapshot(now, days)
    report.update(enabled=False, storage_error=None)
    manager = Mock()
    manager.snapshot.return_value = report
    entry.runtime_data = SimpleNamespace(history=manager)
    result = await _choose(hass, await _menu(hass, entry), "history_report")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"days": str(days)}
    )
    assert result["step_id"] == "history_report_result"
    text = result["description_placeholders"]["report"]
    assert f"{days} abgeschlossene Tage" in text
    assert "Erfassung: pausiert" in text
    assert f"Gültige Messpaare: 0 / {days}" in text
    assert "noch keine gültige Stichprobe" in text
    assert "Rechtzeitige Prognose fehlt" in text
    assert "forecast_missing" not in text
    assert manager.snapshot.call_args.kwargs["days"] == days
    manager.async_start.assert_not_called()
    manager.async_delete_data.assert_not_called()
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "history"


def test_report_shows_paired_comparison_bias_age_and_storage_limit():
    """Die UI zeigt Vergleichsstichprobe und Datenalter ohne Prozentgenauigkeit."""

    report = HistoryArchive("Europe/Berlin").snapshot(
        datetime(2026, 9, 9, 12, tzinfo=UTC), 7
    )
    report.update(enabled=True, storage_error=None, retention_truncated=True)
    metrics = report["horizons"]["daily_previous_18"]
    metrics.update(
        count_valid=2, count_forecasts=3, mae_kwh=1.5, bias_kwh=-0.25, coverage=2 / 7
    )
    metrics["existing_comparison"] = {
        "count": 2,
        "raw_mae_kwh": 1.5,
        "existing_mae_kwh": 1.75,
        "raw_bias_kwh": -0.25,
        "existing_bias_kwh": 0.5,
        "mean_own_age_seconds": 1800,
        "mean_existing_age_seconds": None,
    }
    metrics["calibrated_comparison"] = {
        "count": 2,
        "raw_mae_kwh": 1.5,
        "calibrated_mae_kwh": 0.75,
    }
    text = _format_report(report)
    assert "Speichergrenze erreicht" in text
    assert "MAE: 1.500 kWh" in text
    assert "Bias: -0.250 kWh" in text
    assert "Abdeckung: 28.6%" in text
    assert "dieselben 2 Messpaare" in text
    assert "fremde MAE 1.750 kWh" in text
    assert "eigene Prognose 30.0 Minuten" in text
    assert "Mittleres Datenalter am Stichtag" in text
    assert "fremde Prognose unbekannt" in text
    assert "Angewendete Kalibrierung, dieselben 2 Messpaare" in text
    assert "korrigierte MAE 0.750 kWh" in text


@pytest.mark.asyncio
async def test_delete_failure_keeps_confirmation_form_and_options(hass):
    """Eine fehlgeschlagene Löschung wird nicht als Erfolg dargestellt."""

    entry = _entry(hass, enabled=True)
    result = await _choose(hass, await _menu(hass, entry), "delete_history")
    with patch(
        "custom_components.pv_forecast.history_runtime.async_delete_history_data",
        side_effect=HomeAssistantError("Speicher nicht erreichbar"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm_delete": True}
        )
    assert result["errors"] == {"base": "history_delete_failed"}
    assert entry.options[CONF_HISTORY_ENABLED] is True


async def test_short_term_observation_persists_and_can_be_disabled(hass):
    entry = _entry(hass, enabled=True)
    result = await _choose(hass, await _menu(hass, entry), "history_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY_ENABLED: True, "short_term_enabled": True}
    )
    result = await _choose(hass, result, "history_done")
    assert result["data"]["short_term_enabled"] is True
    hass.config_entries.async_update_entry(entry, options=result["data"])
    result = await _choose(hass, await _menu(hass, entry), "history_settings")
    assert result["data_schema"]({})["short_term_enabled"] is True
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY_ENABLED: True, "short_term_enabled": False}
    )
    result = await _choose(hass, result, "history_done")
    assert result["data"]["short_term_enabled"] is False
    assert result["data"]["roofs"] == entry.options["roofs"]
