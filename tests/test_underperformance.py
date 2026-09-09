"""Feste Vergleichsbelege, Fehlalarmgrenzen und Bedienung ohne Defektdiagnosen."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from custom_components.pv_forecast.history_runtime import _configuration_id
from custom_components.pv_forecast.underperformance import (
    empty_state,
    notification_due,
    observe,
    validate_state,
)

from .test_calibration_runtime import _managers
from .test_uncertainty import day_record, records


def scenario(actual=12):
    values = records()
    values.extend(day_record(i, actual=actual) for i in range(92, 99))
    now = values[-1].end + timedelta(hours=12)
    return values, now


def check(values, now, state=None, excluded=None):
    return observe(
        values,
        state or empty_state(),
        now,
        "configuration-1",
        "UTC",
        excluded or set(),
        accepted_factor=0.9,
    )


def test_persistent_drop_has_seven_days_and_fixed_validated_reference():
    values, now = scenario()
    state, report = check(values, now)
    assert report["status"] == "active"
    assert report["days"] == report["below_days"] == 7
    assert report["raw_kwh"] == 140
    assert report["actual_kwh"] == 84
    assert report["coverage_fraction"] == 1
    assert report["comparison"]["training_count"] == 60
    assert report["comparison"]["validation_count"] == 30
    assert report["comparison"]["variant"] == "raw_model"
    assert report["learning_paused"] is True
    assert len(state["event"]["evidence"]) == 97
    assert validate_state(state) == state


@pytest.mark.parametrize(
    "mode",
    [
        "one_cloudy_day",
        "four_bad_days",
        "night",
        "gap",
        "missing_forecast",
        "quality",
        "source_changed",
        "maintenance",
        "small_drop",
        "seasonal",
    ],
)
def test_insufficient_or_explained_data_does_not_trigger(mode):
    values, now = scenario()
    excluded = set()
    if mode == "one_cloudy_day":
        values[-7:-1] = [day_record(i, actual=20) for i in range(92, 98)]
    elif mode == "four_bad_days":
        values[-3:] = [day_record(i, actual=20) for i in range(96, 99)]
    elif mode == "night":
        values[-1] = day_record(98, prediction=0, actual=0)
    elif mode == "gap":
        values[-1] = day_record(98, actual=None)
    elif mode == "missing_forecast":
        values.pop()
    elif mode == "quality":
        values[-1] = replace(values[-1], quality_flags=("missing_gti",))
    elif mode == "source_changed":
        values[-1] = replace(
            values[-1],
            measurement_sources=(
                replace(values[-1].measurement_sources[0], registry_id="different"),
            ),
        )
    elif mode == "maintenance":
        excluded.add(values[-1].target_date)
    elif mode == "small_drop":
        values[-7:] = [day_record(i, actual=17) for i in range(92, 99)]
    elif mode == "seasonal":
        # Das aktuelle Rohmodell enthält die saisonal geringere Einstrahlung.
        values[-7:] = [day_record(i, prediction=12, actual=12) for i in range(92, 99)]
    state, report = check(values, now, excluded=excluded)
    assert state["event"] is None
    assert report["status"] in ("clear", "insufficient_days", "basis_unavailable")


def test_uncertainty_gate_cannot_be_replaced_by_a_large_forecast_difference():
    values, now = scenario()
    state, report = check(values[10:], now)
    assert state["event"] is None
    assert report["status"] == "basis_unavailable"


def test_accepted_calibration_cannot_hide_the_same_raw_drop():
    values, now = scenario()
    values = [
        replace(v, calibrated_energy_kwh=v.raw_energy_kwh * 0.6, applied_factor=0.6)
        for v in values
    ]
    state, report = check(values, now)
    assert report["status"] == "active"
    assert report["comparison"]["variant"] == "raw_model"
    original = deepcopy(state)
    values.extend(day_record(i, actual=12, factor=0.6) for i in range(99, 115))
    after, later = check(values, now + timedelta(days=16), state)
    assert after == original
    assert later["first_day"] == report["first_day"]
    assert later["status"] == "active"


@pytest.mark.parametrize("change", ["measurement", "deleted_reference", "excluded_day"])
def test_reference_change_is_not_an_automatic_recovery(change):
    values, now = scenario()
    state, _ = check(values, now)
    excluded = set()
    key = next(iter(state["event"]["evidence"]))
    index = next(i for i, v in enumerate(values) if v.record_id == key)
    if change == "measurement":
        values[index] = replace(
            values[index],
            assessment=replace(values[index].assessment, actual_energy_kwh=1),
        )
    elif change == "deleted_reference":
        values.pop(index)
    else:
        excluded.add(values[index].target_date)
    after, report = check(values, now, state, excluded)
    assert after == state
    assert report["status"] == "reference_changed"
    assert report["learning_paused"] is True
    assert "actual_kwh" not in report


def test_physical_change_clears_old_event_and_clear_requires_new_full_days():
    values, now = scenario()
    state, _ = check(values, now)
    changed, report = observe(values, state, now, "new-configuration", "UTC", set())
    assert changed["event"] is None
    assert report["status"] == "insufficient_days"
    assert check(values, now, empty_state(now))[1]["status"] == "insufficient_days"


@pytest.mark.parametrize(
    "hour,expected", [(7, False), (8, True), (21, True), (22, False)]
)
def test_opt_in_quiet_hours_acknowledgement_and_restart(hour, expected):
    values, now = scenario()
    state, _ = check(values, now)
    now = now.replace(hour=hour)
    assert notification_due(state, now, "UTC", enabled=True) is expected
    assert not notification_due(state, now, "UTC", enabled=False)
    state["event"]["notified"] = True
    assert not notification_due(validate_state(state), now, "UTC", enabled=True)
    state["event"].update(notified=False, acknowledged=True)
    assert not notification_due(state, now, "UTC", enabled=True)


@pytest.mark.parametrize(
    "change", ["unknown_version", "oversized", "invalid_references", "naive_time"]
)
def test_corrupt_state_is_never_silently_normalized(change):
    values, now = scenario()
    state, _ = check(values, now)
    if change == "unknown_version":
        state["schema_version"] = 2
    elif change == "oversized":
        state["extra"] = "x" * 65536
    elif change == "invalid_references":
        state["event"]["evidence"] = {}
    else:
        state["event"]["created_at"] = "2026-01-01T00:00:00"
    with pytest.raises(ValueError):
        validate_state(state)


async def test_runtime_pauses_learning_and_bundles_notifications_and_controls(
    hass, freezer
):
    entry, coordinator, history, calibration = _managers(hass, "auto")
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "underperformance_enabled": True,
            "underperformance_notifications": True,
        },
    )
    values, now = scenario()
    freezer.move_to(now)
    values = [replace(v, configuration_id=_configuration_id(entry)) for v in values]
    history._archive.records = {v.record_id: v for v in values}
    history._loaded = history._running = True
    calibration._running = True
    coordinator.calibration_factor = 0.9
    with (
        patch.object(type(history), "identity_unresolved", ()),
        patch(
            "custom_components.pv_forecast.history_runtime.persistent_notification.async_create"
        ) as create,
        patch.object(calibration._state.__class__, "update") as update,
    ):
        history._observe(now)
        assert create.call_count == 1
        assert "84" not in create.call_args.args[1]
        history._observe(now)
        assert create.call_count == 1
        calibration.async_reconcile()
        update.assert_not_called()
        assert coordinator.calibration_factor == 1
        assert calibration.snapshot()["status"] == "underperformance_paused"
        assert calibration.capture_parameters() == {}
        history._store.async_save = Mock(side_effect=lambda value: _async_none())
        await history.async_observation_control("acknowledge")
        assert history.learning_paused
        assert history.snapshot(now=now)["underperformance"]["acknowledged"] is True
        await history.async_observation_control("clear")
        assert not history.learning_paused
        assert history._archive.underperformance["segment_start"] == now.isoformat()
        update.assert_called_once()
    history._running = calibration._running = False


async def _async_none():
    return None


def test_dst_days_remain_seven_local_days_with_complete_evidence():
    from datetime import date

    values = records(
        timezone="Europe/Berlin", base_date=date(2026, 3, 29) - timedelta(days=94)
    )
    base = values[0].target_date
    values.extend(
        day_record(i, actual=12, timezone="Europe/Berlin", base_date=base)
        for i in range(92, 99)
    )
    now = values[-1].end + timedelta(hours=10)
    state, report = observe(
        values, empty_state(), now, "configuration-1", "Europe/Berlin", set()
    )
    assert report["status"] == "active"
    assert len(state["event"]["case_ids"]) == 7


async def test_deleting_archive_clears_cached_measurements_and_hint(hass, freezer):
    entry, _coordinator, history, _calibration = _managers(hass)
    values, now = scenario()
    freezer.move_to(now)
    values = [replace(v, configuration_id=_configuration_id(entry)) for v in values]
    history._loaded = True
    history._archive.records = {v.record_id: v for v in values}
    history._archive.underperformance, history._observation_report = observe(
        values, empty_state(), now, _configuration_id(entry), "UTC", set()
    )
    assert "actual_kwh" in history.snapshot(now=now)["underperformance"]
    await history.async_delete_data()
    assert history._archive.underperformance["event"] is None
    assert "actual_kwh" not in history.snapshot(now=now)["underperformance"]
    await history.async_stop()


def test_source_deletion_removes_dependent_event_references():
    from custom_components.pv_forecast.history import HistoryArchive

    values, now = scenario()
    archive = HistoryArchive("UTC")
    archive.records = {v.record_id: v for v in values}
    archive.underperformance, _ = check(values, now)
    assert archive.delete_measurement_source("pv")
    assert archive.underperformance["event"] is None


async def test_physical_change_also_discards_an_inactive_observation(hass):
    _entry, _coordinator, history, _calibration = _managers(hass)
    values, now = scenario()
    history._archive.underperformance, history._observation_report = check(values, now)
    history._observe(now)
    assert history._archive.underperformance["event"] is None
    assert not history.learning_paused


async def test_read_after_retention_never_exposes_a_cached_measurement(hass):
    entry, _coordinator, history, _calibration = _managers(hass)
    values, now = scenario()
    values = [replace(v, configuration_id=_configuration_id(entry)) for v in values]
    history._archive.records = {v.record_id: v for v in values}
    history._archive.underperformance, history._observation_report = observe(
        values, empty_state(), now, _configuration_id(entry), "UTC", set()
    )
    assert history.snapshot(now=now)["underperformance"]["actual_kwh"] == 84
    history._archive.prune(now, max_records=10)
    report = history.snapshot(now=now)["underperformance"]
    assert report["status"] == "reference_changed"
    assert "actual_kwh" not in report
    assert history.learning_paused


def test_archive_roundtrip_preserves_reference_and_notification_marker():
    from custom_components.pv_forecast.history import HistoryArchive, _record_id

    values, now = scenario()
    values = [
        replace(
            v,
            record_id=_record_id(v.horizon, v.start, v.end),
            raw_energy_kwh=float(v.raw_energy_kwh),
            assessment=replace(
                v.assessment,
                actual_energy_kwh=float(v.assessment.actual_energy_kwh),
                sources=tuple(
                    replace(source, energy_kwh=float(source.energy_kwh))
                    for source in v.assessment.sources
                ),
            ),
        )
        for v in values
    ]
    archive = HistoryArchive("UTC")
    archive.records = {v.record_id: v for v in values}
    archive._latest_observed_at = max(v.observed_at for v in values)
    archive.underperformance, _ = check(values, now)
    archive.underperformance["event"]["notified"] = True
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    state, report = check(
        list(restored.records.values()), now, restored.underperformance
    )
    assert state == archive.underperformance
    assert report["status"] == "active"
    assert not notification_due(state, now, "UTC", enabled=True)
