"""Optionale Kalibrierung ohne stille Aktivierung oder Verlust unabhängiger Optionen."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous_serialize
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.calibration_configuration import (
    CONF_CALIBRATION_EXCLUSIONS,
    CONF_CALIBRATION_MODE,
)
from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.measurements import SourceConfig

from .helpers import persisted_roof
from .test_config_flow import ROOF_FORM

SOURCE = SourceConfig("solar", "sensor.pv_energy", "total", "Gesamte AC-PV-Anlage")


def _entry(hass, *, sources=None, history=True, **changes):
    """Eine Anlage mit unabhängigen Anwenderwerten bereitstellen."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Meine PV-Anlage",
        version=1,
        minor_version=1,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "Europe/Berlin"},
        options={
            "roofs": [persisted_roof("south")],
            "inverter_max_power_kw": 7.3,
            "measurement_sources": [SOURCE.to_dict()] if sources is None else sources,
            "history_enabled": history,
            "comparison_forecast": {"scope": "Vorhandene unabhängige Zuordnung"},
            **changes,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _choose(hass, result, step):
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def _menu(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "calibration" in result["menu_options"]
    return await _choose(hass, result, "calibration")


async def _submit(hass, result, values):
    return await hass.config_entries.options.async_configure(result["flow_id"], values)


async def test_setup_does_not_offer_or_enable_calibration(hass):
    """Der neue Optionsschritt erweitert die Ersteinrichtung nicht."""

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
    assert "calibration" not in result["menu_options"]
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert CONF_CALIBRATION_MODE not in flow._options


@pytest.mark.parametrize("mode", ["observe", "auto"])
@pytest.mark.parametrize("kind", ["total", "daily"])
async def test_mode_preserves_existing_options_and_requires_explicit_save(
    hass, mode, kind
):
    """Ein gewählter Modus speichert weder vorher noch über unabhängige Werte hinweg."""

    entry = _entry(hass, sources=[SOURCE.to_dict() | {"kind": kind}])
    original = deepcopy(dict(entry.options))
    result = await _menu(hass, entry)
    assert result["description_placeholders"]["timezone"] == "Europe/Berlin"
    assert result["description_placeholders"]["mode"] == "Aus"
    result = await _choose(hass, result, "calibration_settings")
    assert result["data_schema"]({}) == {CONF_CALIBRATION_MODE: "off"}
    assert voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
    ) as fetch:
        result = await _submit(hass, result, {CONF_CALIBRATION_MODE: mode})
        assert dict(entry.options) == original
        result = await _choose(hass, result, "calibration_done")
        fetch.assert_not_called()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == original | {CONF_CALIBRATION_MODE: mode}
    assert entry.version == 1 and entry.minor_version == 1


@pytest.mark.parametrize(
    "history,sources,error",
    [
        (False, [SOURCE.to_dict()], "calibration_history_required"),
        (False, [], "calibration_history_required"),
        (True, [], "calibration_energy_required"),
        (True, [SOURCE.to_dict() | {"kind": "power"}], "calibration_energy_required"),
        (
            True,
            [SOURCE.to_dict() | {"confirmed_pv": False}],
            "calibration_energy_required",
        ),
        (
            True,
            [SOURCE.to_dict() | {"confirmed_disjoint": False}],
            "calibration_energy_required",
        ),
        (True, [{"entity_id": "sensor.unconfirmed"}], "calibration_energy_required"),
    ],
)
async def test_missing_prerequisites_never_activate_archive_or_sources(
    hass, history, sources, error
):
    """Fehlende Grundlagen bleiben sichtbar und unverändert."""

    entry = _entry(hass, sources=sources, history=history)
    original = deepcopy(dict(entry.options))
    result = await _choose(hass, await _menu(hass, entry), "calibration_settings")
    result = await _submit(hass, result, {CONF_CALIBRATION_MODE: "auto"})
    assert result["errors"] == {"base": error}
    assert dict(entry.options) == original
    # Abschalten muss auch mit unbrauchbaren oder inzwischen entfernten Quellen gehen.
    result = await _submit(hass, result, {CONF_CALIBRATION_MODE: "off"})
    result = await _choose(hass, result, "calibration_done")
    assert result["data"] == original | {CONF_CALIBRATION_MODE: "off"}


async def test_existing_automatic_mode_can_be_disabled_without_prerequisites(hass):
    """Eine früher aktive Kalibrierung darf das Abschalten nicht blockieren."""

    entry = _entry(hass, sources=[], history=False, calibration_mode="auto")
    result = await _choose(hass, await _menu(hass, entry), "calibration_settings")
    result = await _submit(hass, result, {CONF_CALIBRATION_MODE: "off"})
    result = await _choose(hass, result, "calibration_done")
    assert result["data"][CONF_CALIBRATION_MODE] == "off"
    assert result["data"]["history_enabled"] is False


async def test_status_reads_manager_values_and_returns_to_menu(hass):
    """Die UI formatiert die gemeinsame Prüfstichprobe, ohne selbst zu lernen."""

    entry = _entry(hass)
    manager = Mock()
    manager.snapshot.return_value = {
        "mode": "observe",
        "status": "testing",
        "effective_factor": 1,
        "candidate_factor": 0.89,
        "training_days": 30,
        "validation_days": 7,
        "last_learned_at": "2026-09-08T18:00:00+00:00",
        "raw_mae_kwh": 2,
        "calibrated_mae_kwh": 1.8,
        "improvement_percent": 10,
        "reasons": ["insufficient_validation_days", "extreme_residual"],
        "exclusion_reasons": {"missing_basis": 12, "strong_clipping": 3},
        "storage_error": None,
    }
    entry.runtime_data = SimpleNamespace(calibration=manager)
    result = await _choose(hass, await _menu(hass, entry), "calibration_status")
    assert result["errors"] == {}
    text = result["description_placeholders"]["report"]
    assert "30 / 30" in text and "7 / 14" in text
    assert "0.890" in text and "1.000" in text
    assert "2.000 kWh" in text and "1.800 kWh" in text and "10.000 %" in text
    assert "2026-09-08T20:00:00+02:00" in text
    assert "Noch keine 14" in text and "dadurch allein kein Ausschluss" in text
    assert "insufficient_validation_days" not in text
    assert "ungekürzte Lernbasis: 12" in text
    assert "AC-Begrenzung kürzt den Lerntag zu stark: 3" in text
    manager.snapshot.assert_called_once_with()
    manager.async_update.assert_not_called()
    assert (await _submit(hass, result, {}))["step_id"] == "calibration"


async def test_missing_status_and_storage_error_remain_distinct(hass):
    """Nicht geladener Status und nicht lesbarer Speicher behaupten keine Freigabe."""

    entry = _entry(hass)
    result = await _choose(hass, await _menu(hass, entry), "calibration_status")
    assert result["errors"] == {"base": "calibration_unavailable"}
    result = await _submit(hass, result, {})
    entry.runtime_data = SimpleNamespace(calibration=Mock())
    entry.runtime_data.calibration.snapshot.return_value = {
        "mode": "auto",
        "status": "storage_unavailable",
        "effective_factor": 1,
        "candidate_factor": None,
        "raw_mae_kwh": None,
        "storage_error": "unreadable",
    }
    result = await _choose(hass, result, "calibration_status")
    assert result["errors"] == {}
    text = result["description_placeholders"]["report"]
    assert "Lernspeicher nicht lesbar" in text
    assert "noch kein Nachweis" in text


async def test_reset_requires_confirmation_and_preserves_options(hass):
    """Nur ausdrückliche Bestätigung löst das sofortige Rücksetzen aus."""

    entry = _entry(hass, calibration_mode="auto")
    original = deepcopy(dict(entry.options))
    result = await _choose(hass, await _menu(hass, entry), "reset_calibration")
    with patch(
        "custom_components.pv_forecast.calibration_runtime.async_delete_calibration_data",
        new_callable=AsyncMock,
    ) as reset:
        result = await _submit(hass, result, {"confirm_reset": False})
        reset.assert_not_called()
        result = await _choose(hass, result, "reset_calibration")
        result = await _submit(hass, result, {"confirm_reset": True})
        reset.assert_awaited_once_with(hass, entry)
    assert result["step_id"] == "calibration"
    result = await _choose(hass, result, "calibration_done")
    assert result["data"] == original


async def test_failed_reset_keeps_confirmation_and_options(hass):
    """Ein Speicherfehler erscheint als Fehler und nicht als erfolgreicher Neustart."""

    entry = _entry(hass, calibration_mode="auto")
    result = await _choose(hass, await _menu(hass, entry), "reset_calibration")
    with patch(
        "custom_components.pv_forecast.calibration_runtime.async_delete_calibration_data",
        side_effect=HomeAssistantError("Speicher nicht lesbar"),
    ):
        result = await _submit(hass, result, {"confirm_reset": True})
    assert result["errors"] == {"base": "calibration_reset_failed"}
    assert entry.options[CONF_CALIBRATION_MODE] == "auto"


async def test_local_day_marking_replacement_and_removal_preserve_other_options(
    hass, freezer
):
    """Nach lokalem Mitternachtswechsel ist bereits der neue Anlagentag markierbar."""

    freezer.move_to(datetime(2026, 9, 9, 22, 30, tzinfo=UTC))
    entry = _entry(hass, history=False, sources=[])
    original = deepcopy(dict(entry.options))
    result = await _choose(hass, await _menu(hass, entry), "calibration_exclusion")
    assert result["data_schema"]({}) == {"date": "2026-09-10", "reason": "curtailment"}
    result = await _submit(
        hass, result, {"date": "2026-09-10", "reason": "curtailment"}
    )
    assert dict(entry.options) == original
    result = await _choose(hass, result, "calibration_exclusion")
    result = await _submit(
        hass, result, {"date": "2026-09-10", "reason": "maintenance"}
    )
    result = await _choose(hass, result, "calibration_done")
    assert result["data"] == original | {
        CONF_CALIBRATION_EXCLUSIONS: [{"date": "2026-09-10", "reason": "maintenance"}]
    }
    result = await _choose(
        hass, await _menu(hass, entry), "calibration_remove_exclusion"
    )
    result = await _submit(hass, result, {"date": "2026-09-10"})
    result = await _choose(hass, result, "calibration_done")
    assert result["data"] == original | {CONF_CALIBRATION_EXCLUSIONS: []}


@pytest.mark.parametrize("day", ["2026-09-11", "2025-09-10"])
async def test_day_marking_rejects_future_and_outside_local_year(hass, freezer, day):
    """Die 365-Tage-Grenze enthält heute und höchstens 364 frühere lokale Tage."""

    freezer.move_to("2026-09-10T12:00:00Z")
    entry = _entry(hass)
    result = await _choose(hass, await _menu(hass, entry), "calibration_exclusion")
    result = await _submit(hass, result, {"date": day, "reason": "curtailment"})
    assert result["errors"] == {"base": "calibration_exclusion_date"}
    assert CONF_CALIBRATION_EXCLUSIONS not in entry.options


async def test_ninety_markings_limit_allows_replacing_existing_day(hass, freezer):
    """Die Obergrenze erlaubt weiterhin Korrekturen vorhandener Tage."""

    today = date(2026, 9, 10)
    freezer.move_to("2026-09-10T12:00:00Z")
    exclusions = [
        {"date": (today - timedelta(days=index)).isoformat(), "reason": "curtailment"}
        for index in range(90)
    ]
    entry = _entry(hass, calibration_exclusions=exclusions)
    result = await _choose(hass, await _menu(hass, entry), "calibration_exclusion")
    result = await _submit(
        hass,
        result,
        {"date": (today - timedelta(days=90)).isoformat(), "reason": "maintenance"},
    )
    assert result["errors"] == {"base": "calibration_exclusion_limit"}
    result = await _submit(
        hass, result, {"date": today.isoformat(), "reason": "maintenance"}
    )
    result = await _choose(hass, result, "calibration_done")
    saved = result["data"][CONF_CALIBRATION_EXCLUSIONS]
    assert len(saved) == 90
    assert saved[-1] == {"date": today.isoformat(), "reason": "maintenance"}


async def test_first_permitted_day_is_included_and_expired_marking_frees_capacity(
    hass, freezer
):
    """Veraltete Markierungen belegen beim bewussten Bearbeiten keinen neuen Platz."""

    freezer.move_to("2026-09-10T12:00:00Z")
    entry = _entry(
        hass, calibration_exclusions=[{"date": "2025-09-10", "reason": "maintenance"}]
    )
    result = await _choose(hass, await _menu(hass, entry), "calibration_exclusion")
    result = await _submit(
        hass, result, {"date": "2025-09-11", "reason": "curtailment"}
    )
    result = await _choose(hass, result, "calibration_done")
    assert result["data"][CONF_CALIBRATION_EXCLUSIONS] == [
        {"date": "2025-09-11", "reason": "curtailment"}
    ]
