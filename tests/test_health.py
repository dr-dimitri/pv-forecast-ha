"""Lokaler Betriebscheck mit Zeitabdeckung und nebenwirkungsfreiem Optionspfad."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.pv_forecast.api import OpenMeteoRequestState
from custom_components.pv_forecast.health import (
    HealthState,
    check_health,
    forecast_coverage,
)
from custom_components.pv_forecast.health_runtime import capture_health

from .test_forecast_cache import basis, entry_for

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def state(**kwargs):
    return HealthState(
        **(
            dict(
                loaded=True,
                forecast=basis()[0],
                timezone="UTC",
                fetched_at=NOW,
                last_success=True,
            )
            | kwargs
        )
    )


def codes(snapshot):
    return {item.code: item for item in check_health(snapshot, NOW)}


@pytest.mark.parametrize(
    "day,zone",
    [
        (date(2026, 3, 29), "Europe/Berlin"),
        (date(2026, 10, 25), "Europe/Berlin"),
        (date(2026, 9, 10), "Asia/Kathmandu"),
    ],
)
def test_full_seven_day_horizon_and_current_energy_coverage(day, zone):
    forecast, _, args = basis(day, zone, 7)
    now = args["now"]
    result = forecast_coverage(forecast, zone, now)
    assert result["complete"] and result["energy_current_complete"]
    next_day = forecast_coverage(forecast, zone, now + timedelta(days=1))
    assert next_day["complete"] and next_day["energy_current_complete"]
    assert not next_day["current_local_days"]
    expired = forecast_coverage(forecast, zone, now + timedelta(days=6))
    assert expired["complete"] and not expired["energy_current_complete"]


def test_failures_pause_age_and_optional_features_remain_separate():
    result = codes(
        state(
            last_success=False,
            error="invalid_api_data",
            retry_after=5400,
            polling_disabled=True,
            fetched_at=NOW - timedelta(minutes=75),
        )
    )
    assert result["invalid_response"].severity == "error"
    assert result["provider_pause"].value == 90
    assert result["stale"].value == 75
    assert result["polling_disabled"].severity == "info"
    assert (
        result["no_sources"].severity
        == result["archive_off"].severity
        == result["learning_off"].severity
        == "info"
    )


@pytest.mark.parametrize(
    "stamp", [None, NOW + timedelta(seconds=1), NOW.replace(tzinfo=None)]
)
def test_unknown_or_future_age_is_not_healthy(stamp):
    assert "age_unknown" in codes(state(fetched_at=stamp))


def test_sources_archive_and_learning_states_are_actionable_without_evaluation():
    result = codes(
        state(
            source_count=2,
            measurements_available=True,
            sources=(
                "source_removed",
                "source_disabled",
                "source_derived",
                "source_daily",
            ),
            archive_enabled=True,
            archive_available=True,
            archive_storage_error=True,
            archive_truncated=True,
            calibration_mode="observe",
            calibration_status="prerequisites_missing",
            cache_status="unsupported_version",
        )
    )
    assert result["source_removed"].severity == "warning"
    assert result["source_daily"].severity == "info"
    assert result["archive_store"].severity == "error"
    assert result["learning_prerequisites"].severity == "info"
    assert result["cache_unsupported_version"].severity == "error"


async def test_options_without_runtime_and_recheck_never_touch_network_or_stores(
    hass, freezer, hass_storage
):
    freezer.move_to(NOW)
    entry = entry_for(hass)
    original = deepcopy(dict(entry.options))
    stores = deepcopy(hass_storage)
    request = OpenMeteoRequestState()
    request._record_temporary_failure("5400")
    hass.data["pv_forecast"] = request
    with (
        patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
        ) as fetch,
        patch("homeassistant.helpers.storage.Store.async_load") as load,
        patch("homeassistant.helpers.storage.Store.async_save") as save,
    ):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "health"}
        )
        assert flow["step_id"] == "health"
        report = flow["description_placeholders"]["report"]
        assert "Anlage nicht geladen" in report
        assert "90 Minuten" in report
        assert "kein geplanter Abruftermin" in report
        again = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "health"}
        )
        assert again["description_placeholders"]["report"] == report
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "init"}
        )
        fetch.assert_not_called()
        load.assert_not_called()
        save.assert_not_called()
    assert dict(entry.options) == original
    assert hass_storage == stores


async def test_missing_runtime_never_infers_removed_sources(hass):
    entry = entry_for(hass)
    hass.config_entries.async_update_entry(
        entry,
        options=dict(entry.options)
        | {
            "measurement_sources": [{"registry_id": "not-an-identity"}],
            "history_enabled": True,
            "calibration_mode": "observe",
        },
    )
    captured = capture_health(hass, entry, NOW)
    result = codes(captured)
    assert "measurements_unknown" in result
    assert "source_removed" not in result
    assert "archive_unknown" in result
    assert "learning_unknown" in result


def test_complete_forecast_with_gap_or_fallback_is_classified():
    original = basis()[0]
    gap = replace(original, total_intervals=original.total_intervals[1:])
    result = codes(state(forecast=gap))
    assert "horizon_incomplete" in result and "energy_incomplete" in result
    marked = replace(
        original,
        total_intervals=(
            replace(original.total_intervals[0], quality_flags=("gti_fallback",)),
            *original.total_intervals[1:],
        ),
    )
    assert codes(state(forecast=marked))["input_fallbacks"].value == 1


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("removed", "source_removed"),
        ("disabled", "source_disabled"),
        ("unavailable", "source_unavailable"),
        ("gap", "source_gap"),
        ("renamed", "source_gap"),
    ],
)
async def test_runtime_registry_states_are_read_without_changing_sources(
    hass, fault, expected
):
    from types import SimpleNamespace

    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import entity_registry as er

    from custom_components.pv_forecast.measurement_runtime import MeasurementManager

    from .test_measurement_runtime import _entry, _source

    registry = er.async_get(hass)
    entity = registry.async_get_or_create("sensor", "test", "energy")
    source = _source(registry_id=entity.id, entity_id=entity.entity_id)
    entry = _entry(hass, source)
    manager = MeasurementManager(hass, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(measurements=manager)
    hass.states.async_set(
        entity.entity_id, "unavailable" if fault == "unavailable" else "10"
    )
    if fault == "renamed":
        registry.async_update_entity(entity.entity_id, new_entity_id="sensor.renamed")
        hass.states.async_set("sensor.renamed", "10")
    if fault == "removed":
        registry.async_remove(entity.entity_id)
    if fault == "disabled":
        registry.async_update_entity(
            entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    before = deepcopy(dict(entry.options))
    result = codes(capture_health(hass, entry, NOW))
    assert expected in result
    assert dict(entry.options) == before


@pytest.mark.parametrize("derived", [False, True])
async def test_current_day_retention_loss_overrides_fresh_source_until_local_midnight(
    hass, derived
):
    from types import SimpleNamespace

    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import entity_registry as er

    from custom_components.pv_forecast.measurement_runtime import MeasurementManager
    from custom_components.pv_forecast.measurements import SourceConfig, SourceHistory

    from .test_measurement_runtime import _entry, _source

    registry = er.async_get(hass)
    entity = registry.async_get_or_create("sensor", "test", "energy")
    source = _source(registry_id=entity.id, entity_id=entity.entity_id)
    source["derived_energy"] = derived
    entry = _entry(hass, source)
    manager = MeasurementManager(hass, entry)
    captured = SourceHistory(SourceConfig.from_dict(source), "Europe/Berlin", 20)
    start = datetime(2026, 9, 10, 21, 30, tzinfo=UTC)
    for index in range(4):
        captured.add_reading(start + timedelta(minutes=5 * index), 100 + index, "kWh")
    now = start + timedelta(minutes=15)
    captured.prune(start - timedelta(days=7), max_readings=3)
    manager._histories[source["source_id"]] = captured
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(measurements=manager)
    hass.states.async_set(entity.entity_id, "103")

    with (
        patch("homeassistant.helpers.storage.Store.async_load") as load,
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch("custom_components.pv_forecast.history.HistoryArchive.assess") as assess,
    ):
        result = codes(capture_health(hass, entry, now))
        assert result["source_retention_gap"].severity == "warning"
        assert result["source_retention_gap"].value == 1
        assert "source_ok" not in result
        assert "source_gap" not in result
        assert ("source_derived" in result) is derived

        # In Berlin beginnt der nächste Tag bereits um 22 Uhr UTC.
        midnight = datetime(2026, 9, 10, 22, tzinfo=UTC)
        for later in (midnight, midnight + timedelta(minutes=1)):
            captured.add_reading(later, 103, "kWh")
            result = codes(capture_health(hass, entry, later))
            assert "source_retention_gap" not in result
            assert "source_ok" in result
        load.assert_not_called()
        save.assert_not_called()
        assess.assert_not_called()
