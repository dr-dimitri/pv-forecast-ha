"""Bewussten HA-Download, feste Datenstruktur und sensible Fehlerdaten prüfen."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.pv_forecast.api import (
    OpenMeteoConnectionError,
    OpenMeteoDataError,
    OpenMeteoRateLimitError,
    OpenMeteoRetryPendingError,
    OpenMeteoTemporaryError,
)
from custom_components.pv_forecast.calculations import InvalidConfigurationError
from custom_components.pv_forecast.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.diagnostics import async_get_config_entry_diagnostics

from .helpers import persisted_roof, weather

_SECRET = "Geheimer-Standort-und-Entity-Name"
_DAY = date(2026, 8, 23)
_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _weather(day: date = _DAY, timezone_name: str = "Europe/Berlin"):
    timezone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=2), time.min, timezone).astimezone(UTC)
    cursor = start.replace(minute=0) + timedelta(hours=1)
    intervals = []
    while cursor - timedelta(hours=1) < end:
        intervals.append(weather(end=cursor))
        cursor += timedelta(hours=1)
    return {f"{_SECRET}-a": tuple(intervals), f"{_SECRET}-b": tuple(intervals)}


def _entry(hass, timezone_name: str = "Europe/Berlin"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=_SECRET,
        unique_id=f"{_SECRET}-entry",
        version=1,
        minor_version=1,
        data={
            CONF_LATITUDE: 48.123456,
            CONF_LONGITUDE: 13.654321,
            CONF_TIME_ZONE: timezone_name,
            "location_name": _SECRET,
            "street": _SECRET,
            "postal_code": _SECRET,
            "country": _SECRET,
            "future_private_data": {"anything": _SECRET},
        },
        options={
            CONF_ROOFS: [
                persisted_roof(f"{_SECRET}-a", name=_SECRET),
                persisted_roof(f"{_SECRET}-b", name=_SECRET),
            ],
            "future_private_data": {"entity_id": _SECRET},
        },
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def fixed_clock(freezer):
    """Abrufalter und aktuelle lokale Tage offline reproduzierbar halten."""

    freezer.move_to(_NOW)


@pytest.fixture
def forecast_client():
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value=_weather(),
    ) as fetch:
        yield fetch


async def _load(hass, forecast_client):
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _assert_private(result):
    """Neue unbekannte Felder und Freitexte dürfen keinen Exportpfad öffnen."""

    serialized = json.dumps(result, allow_nan=False)
    for forbidden in (
        _SECRET,
        "48.123456",
        "13.654321",
        "Europe/Berlin",
        "https://",
        "energy_kwh",
        "entity_id",
        "candidate_id",
        "configuration_id",
        "raw_mae",
    ):
        assert forbidden not in serialized
    assert len(serialized) < 4000


async def test_download_has_fixed_metadata_snapshot(hass, hass_client, forecast_client):
    """Native Entry-Diagnose ist auffindbar und enthält nur die erlaubte Struktur."""

    entry = await _load(hass, forecast_client)
    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert result == {
        "schema_version": 1,
        "configuration_version": {"major": 1, "minor": 1},
        "model_version": "1",
        "supported_storage_versions": {
            "measurements": 2,
            "history": 6,
            "calibration": 1,
        },
        "forecast_cache": {"status": "disabled"},
        "calibration_rule_version": 1,
        "entry_state": "loaded",
        "runtime_available": True,
        "configuration": {
            "valid_roofs": True,
            "roof_count": 2,
            "geometry_count": 1,
            "inverter_limit_configured": False,
        },
        "forecast": {
            "last_update_success": True,
            "last_success_age_seconds": 0,
            "update_interval_seconds": 1800,
            "error_class": None,
            "retry_after_seconds": None,
            "coverage": {
                "available": True,
                "current_local_days": True,
                "interval_count": 48,
                "complete": True,
                "energy_current_complete": True,
                "incomplete_intervals": 0,
                "quality_marked_intervals": 0,
            },
        },
        "measurements": {
            "available": True,
            "running": False,
            "source_count": 0,
            "unresolved_identity_count": 0,
            "storage_error": None,
        },
        "history": {
            "available": True,
            "enabled": False,
            "loaded": False,
            "running": False,
            "storage_error": None,
        },
        "calibration": {
            "available": True,
            "mode": "off",
            "status": "off",
            "storage_error": None,
            "prerequisites_met": False,
        },
    }
    _assert_private(result)
    assert forecast_client.await_count == 1


async def test_diagnostics_reads_without_updates_storage_or_history(
    hass, freezer, forecast_client
):
    """Diagnostik löst weder HTTP, Lernen, Speichern noch Archivbewertung aus."""

    entry = await _load(hass, forecast_client)
    runtime = entry.runtime_data
    forecast = runtime.coordinator.data
    fetched_at = runtime.coordinator.last_update_success_time
    before = dict(entry.options), dict(entry.data)
    freezer.move_to(_NOW + timedelta(minutes=7))

    with (
        patch.object(Store, "async_load") as load,
        patch.object(Store, "async_save") as save,
        patch.object(Store, "async_delay_save") as delayed,
        patch.object(runtime.history, "snapshot") as history,
        patch.object(runtime.measurements, "snapshot") as measurements,
        patch.object(runtime.calibration, "async_reconcile") as learn,
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    for method in (load, save, delayed, history, measurements, learn):
        method.assert_not_called()
    assert result["forecast"]["last_success_age_seconds"] == 420
    assert runtime.coordinator.data is forecast
    assert runtime.coordinator.last_update_success_time == fetched_at
    assert (dict(entry.options), dict(entry.data)) == before
    assert forecast_client.await_count == 1


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (OpenMeteoRateLimitError, "rate_limit"),
        (OpenMeteoRetryPendingError, "retry_pending"),
        (OpenMeteoTemporaryError, "temporary_api_error"),
        (OpenMeteoDataError, "invalid_api_data"),
        (OpenMeteoConnectionError, "connection_error"),
        (InvalidConfigurationError, "invalid_configuration"),
        (TimeoutError, "timeout"),
        (ValueError, "update_failed"),
    ],
)
async def test_error_class_never_includes_messages_or_urls(
    hass, forecast_client, error_type, expected
):
    """Die Fehlerursache hilft bei der Diagnose, sensible Meldungen bleiben lokal."""

    entry = await _load(hass, forecast_client)
    error = UpdateFailed(_SECRET)
    error.__cause__ = error_type(
        f"https://example.invalid/?latitude=48.123456&{_SECRET}"
    )
    coordinator = entry.runtime_data.coordinator
    coordinator.last_update_success = False
    coordinator.last_exception = error
    hass.data[DOMAIN]._record_temporary_failure("3600")

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["forecast"]["error_class"] == expected
    assert result["forecast"]["last_update_success"] is False
    assert 3599 <= result["forecast"]["retry_after_seconds"] <= 3600
    assert result["forecast"]["coverage"]["available"] is True
    _assert_private(result)
    assert forecast_client.await_count == 1


async def test_status_allowlists_block_unknown_stored_texts(hass, forecast_client):
    """Auch optionale Manager liefern ausschließlich geprüfte Statuscodes."""

    entry = await _load(hass, forecast_client)
    runtime = entry.runtime_data
    runtime.measurements._storage_error = _SECRET
    runtime.history._storage_error = _SECRET
    with patch.object(
        runtime.calibration,
        "snapshot",
        return_value={
            "mode": _SECRET,
            "status": _SECRET,
            "storage_error": _SECRET,
            "candidate_id": _SECRET,
            "raw_mae_kwh": 12345.6789,
            "records": [{"entity_id": _SECRET}],
        },
    ):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["measurements"]["storage_error"] == "unknown"
    assert result["history"]["storage_error"] == "unknown"
    assert result["calibration"] == {
        "available": True,
        "mode": "unknown",
        "status": "unknown",
        "storage_error": "unknown",
        "prerequisites_met": False,
    }
    _assert_private(result)


async def test_active_measurement_and_learning_only_expose_status(
    hass, forecast_client
):
    """Aktive bestätigte Quellen liefern weder Zählerstände noch Quellidentitäten."""

    entry = _entry(hass)
    source_entity = "sensor.geheime_pv_erzeugung"
    hass.states.async_set(
        source_entity,
        "12345.6789",
        {"unit_of_measurement": "kWh", "friendly_name": _SECRET},
    )
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "measurement_sources": [
                {
                    "source_id": _SECRET,
                    "entity_id": source_entity,
                    "registry_id": None,
                    "kind": "total",
                    "scope": _SECRET,
                    "derived_energy": False,
                    "max_interval_minutes": 60,
                    "confirmed_pv": True,
                    "confirmed_disjoint": True,
                }
            ],
            "history_enabled": True,
            "calibration_mode": "observe",
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["measurements"] == {
        "available": True,
        "running": True,
        "source_count": 1,
        "unresolved_identity_count": 0,
        "storage_error": None,
    }
    assert result["history"] == {
        "available": True,
        "enabled": True,
        "loaded": True,
        "running": True,
        "storage_error": None,
    }
    assert result["calibration"] == {
        "available": True,
        "mode": "observe",
        "status": "learning",
        "storage_error": None,
        "prerequisites_met": True,
    }
    serialized = json.dumps(result)
    assert source_entity not in serialized
    assert "12345.6789" not in serialized
    _assert_private(result)
    assert forecast_client.await_count == 1


@pytest.mark.parametrize(
    ("timezone_name", "day", "interval_count"),
    [
        ("Europe/Berlin", date(2026, 3, 29), 47),
        ("Europe/Berlin", date(2026, 10, 25), 49),
        ("Asia/Kolkata", _DAY, 49),
    ],
)
async def test_coverage_respects_absolute_intervals(
    hass, freezer, forecast_client, timezone_name, day, interval_count
):
    """DST und Teilstunden gelten ohne Export der Standortzeitzone als vollständig."""

    freezer.move_to(datetime.combine(day, time(12), UTC))
    entry = _entry(hass, timezone_name)
    forecast_client.return_value = _weather(day, timezone_name)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["forecast"]["coverage"]["complete"] is True
    assert result["forecast"]["coverage"]["current_local_days"] is True
    assert result["forecast"]["coverage"]["interval_count"] == interval_count
    assert timezone_name not in json.dumps(result)


async def test_missing_coverage_and_old_local_days_remain_visible(
    hass, freezer, forecast_client
):
    """Ein erfolgreicher alter Stand ist kein lückenloser aktueller Prognosestand."""

    entry = await _load(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    forecast = coordinator.data
    intervals = forecast.total_intervals
    coordinator.data = replace(
        forecast,
        total_intervals=(
            replace(intervals[0], is_complete=False, quality_flags=(_SECRET,)),
            *intervals[2:],
        ),
    )
    freezer.move_to(_NOW + timedelta(days=1))
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["forecast"]["coverage"] == {
        "available": True,
        "current_local_days": False,
        "interval_count": 47,
        "complete": False,
        "energy_current_complete": False,
        "incomplete_intervals": 1,
        "quality_marked_intervals": 1,
    }
    _assert_private(result)
    coordinator.data = None
    coordinator.last_update_success_time = None
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["forecast"]["coverage"] == {"available": False}
    assert result["forecast"]["last_success_age_seconds"] is None


async def test_unloaded_or_broken_configuration_has_metadata_without_io(hass):
    """Auch vor einem erfolgreichen Setup sind Versionsdaten lesbar und redigiert."""

    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_ROOFS: [_SECRET]})
    with patch.object(Store, "async_load") as load:
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["configuration"] == {"valid_roofs": False}
    assert result["runtime_available"] is False
    assert result["entry_state"] == "not_loaded"
    assert "forecast" not in result
    _assert_private(result)
    load.assert_not_called()
