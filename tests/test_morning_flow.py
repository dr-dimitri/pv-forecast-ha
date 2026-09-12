"""Morgenvergleich bleibt freiwillig und braucht einen eigenen Nachweis."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous_serialize
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import CONF_MORNING_MODE, DOMAIN
from custom_components.pv_forecast.measurements import SourceConfig

from .helpers import persisted_roof

SOURCE = SourceConfig("solar", "sensor.pv_energy", "total", "Gesamte AC-PV-Anlage")


def _entry(hass, *, sources=None, history=True, **changes):
    """Unabhängige Modelle und Optionen dürfen sich nicht durch den Versuch ändern."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "Europe/Berlin"},
        options={
            "roofs": [persisted_roof("south")],
            "inverter_max_power_kw": 7.3,
            "measurement_sources": [SOURCE.to_dict()] if sources is None else sources,
            "history_enabled": history,
            "calibration_mode": "auto",
            **changes,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _choose(hass, result, step):
    return await _submit(hass, result, {"next_step_id": step})


async def _submit(hass, result, values):
    return await hass.config_entries.options.async_configure(result["flow_id"], values)


async def _menu(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "morning" not in result["menu_options"]
    result = await _choose(hass, result, "advanced_options")
    assert "morning" in result["menu_options"]
    return await _choose(hass, result, "morning")


@pytest.mark.parametrize("mode", ["observe", "auto", "off"])
@pytest.mark.parametrize("kind", ["total", "daily"])
async def test_modes_require_explicit_save_and_preserve_other_options(hass, mode, kind):
    """Weder Entwurf noch Speichern starten eine Wetterabfrage oder andere Modelle."""
    entry = _entry(hass, sources=[SOURCE.to_dict() | {"kind": kind}])
    original = deepcopy(dict(entry.options))
    result = await _menu(hass, entry)
    assert result["description_placeholders"]["mode"] == "Aus"
    assert result["description_placeholders"]["timezone"] == "Europe/Berlin"
    result = await _choose(hass, result, "morning_settings")
    assert result["data_schema"]({}) == {CONF_MORNING_MODE: "off"}
    serialized = voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert serialized[0]["selector"]["select"]["translation_key"] == "morning_mode"
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
    ) as fetch:
        result = await _submit(hass, result, {CONF_MORNING_MODE: mode})
        assert dict(entry.options) == original
        result = await _choose(hass, result, "morning_done")
        fetch.assert_not_called()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == original | {CONF_MORNING_MODE: mode}
    assert (entry.version, entry.minor_version) == (1, 1)


@pytest.mark.parametrize("mode", ["observe", "auto"])
@pytest.mark.parametrize(
    "history,sources,error",
    [
        (False, [SOURCE.to_dict()], "morning_history_required"),
        (True, [], "morning_energy_required"),
        (True, [SOURCE.to_dict() | {"kind": "power"}], "morning_energy_required"),
        (
            True,
            [SOURCE.to_dict() | {"confirmed_pv": False}],
            "morning_energy_required",
        ),
        (
            True,
            [SOURCE.to_dict() | {"confirmed_disjoint": False}],
            "morning_energy_required",
        ),
        (True, [{"entity_id": "sensor.unknown"}], "morning_energy_required"),
    ],
)
async def test_prerequisites_never_enable_archive_or_sources(
    hass, mode, history, sources, error
):
    """Globale Automatik ersetzt keine Voraussetzungen des Morgenvergleichs."""
    entry = _entry(hass, sources=sources, history=history, morning_mode="auto")
    original = deepcopy(dict(entry.options))
    result = await _choose(hass, await _menu(hass, entry), "morning_settings")
    result = await _submit(hass, result, {CONF_MORNING_MODE: mode})
    assert result["errors"] == {"base": error}
    assert dict(entry.options) == original
    result = await _submit(hass, result, {CONF_MORNING_MODE: "off"})
    result = await _choose(hass, result, "morning_done")
    assert result["data"] == original | {CONF_MORNING_MODE: "off"}


async def test_status_reads_existing_evidence_and_translated_reasons(hass, freezer):
    """Die UI stellt Zahlen dar und startet weder Lernen noch eine Beobachtung."""
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    freezer.move_to(now)
    entry = _entry(hass)
    manager = Mock()
    manager.morning_snapshot.return_value = {
        "mode": "observe",
        "status": "testing",
        "effective_factor": 1,
        "candidate_factor": 0.89,
        "training_days": 30,
        "validation_days": 7,
        "metrics": {
            "raw_morning_mae_kwh": 2.4,
            "baseline_morning_mae_kwh": 2,
            "candidate_morning_mae_kwh": 1.8,
            "baseline_rise_mae_minutes": None,
            "sample_days": 14,
            "paired_rise_days": 9,
            "raw_false_rises": 3,
            "baseline_false_rises": 2,
            "candidate_false_rises": 1,
        },
        "reasons": ["insufficient_validation_days"],
        "exclusion_reasons": {"measurement_resolution_too_coarse": 2},
        "retention_truncated": True,
    }
    entry.runtime_data = SimpleNamespace(history=manager)
    result = await _choose(hass, await _menu(hass, entry), "morning_status")
    assert result["errors"] == {}
    report = result["description_placeholders"]["report"]
    assert "30 / 30" in report and "7 / 14" in report
    assert "1.000" in report and "0.890" in report
    assert "2.400 / 2.000 / 1.800" in report
    assert "**Vergleichstage:** 14" in report
    assert "**Fälschlich vorhergesagte Anstiege:** 3 / 2 / 1" in report
    assert "Ältere Morgenbelege wurden gekürzt" in report
    assert "noch kein Nachweis" in report
    assert "Noch keine 14 späteren Morgen-Prüftage" in report
    assert "für den Anstiegsnachweis zu grob: 2" in report
    assert "insufficient_validation_days" not in report
    manager.morning_snapshot.assert_called_once_with(now)
    assert len(manager.mock_calls) == 1
    assert (await _submit(hass, result, {}))["step_id"] == "morning"


async def test_missing_runtime_status_is_explicit(hass):
    """Ein ungeladenes Archiv behauptet keine vorhandene Freigabe."""
    entry = _entry(hass)
    result = await _choose(hass, await _menu(hass, entry), "morning_status")
    assert result["errors"] == {"base": "morning_unavailable"}
    assert "kein Morgenstatus geladen" in result["description_placeholders"]["report"]


async def test_reset_requires_confirmation_and_preserves_global_calibration(hass):
    """Nur die bewusste Bestätigung ruft den eigenen Morgen-Reset auf."""
    entry = _entry(hass, morning_mode="auto")
    original = deepcopy(dict(entry.options))
    reset = AsyncMock()
    entry.runtime_data = SimpleNamespace(
        history=SimpleNamespace(async_reset_morning=reset)
    )
    result = await _choose(hass, await _menu(hass, entry), "reset_morning")
    assert result["data_schema"]({}) == {"confirm_reset": False}
    result = await _submit(hass, result, {"confirm_reset": False})
    reset.assert_not_called()
    result = await _choose(hass, result, "reset_morning")
    result = await _submit(hass, result, {"confirm_reset": True})
    reset.assert_awaited_once_with()
    result = await _choose(hass, result, "morning_done")
    assert result["data"] == original


@pytest.mark.parametrize("missing", [False, True])
async def test_unavailable_reset_reports_failure(hass, missing):
    """Ein fehlender Manager oder Speicherfehler bleibt als Fehlschlag sichtbar."""
    entry = _entry(hass, morning_mode="auto")
    if not missing:
        entry.runtime_data = SimpleNamespace(
            history=SimpleNamespace(
                async_reset_morning=AsyncMock(side_effect=HomeAssistantError)
            )
        )
    result = await _choose(hass, await _menu(hass, entry), "reset_morning")
    result = await _submit(hass, result, {"confirm_reset": True})
    assert result["errors"] == {
        "base": "morning_unavailable" if missing else "morning_reset_failed"
    }
    assert entry.options[CONF_MORNING_MODE] == "auto"


async def test_reset_rejects_a_concurrently_changed_configuration(hass):
    """Ein alter Bestätigungsdialog löscht keine Belege einer geänderten Anlage."""
    entry = _entry(hass)
    reset = AsyncMock()
    entry.runtime_data = SimpleNamespace(
        history=SimpleNamespace(async_reset_morning=reset)
    )
    result = await _choose(hass, await _menu(hass, entry), "reset_morning")
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"inverter_max_power_kw": 8.1}
    )
    result = await _submit(hass, result, {"confirm_reset": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_entry_changed"
    reset.assert_not_called()
