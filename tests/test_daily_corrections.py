"""Manuelle Tageswahrheit: Revisionen, ursprüngliche Messungen und native Eingabe."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.pv_forecast.calibration_runtime import CalibrationManager
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.history_corrections import (
    correct_day,
    correction_group,
    correction_groups,
    correction_revision,
)
from custom_components.pv_forecast.history_runtime import ArchiveManager, _history_store

from .test_history import DAY, SOURCE, capture_day, evidence, forecast, report_after
from .test_history_runtime import _Coordinator, _entry


def _archive(*, timezone="UTC", day=DAY):
    archive = HistoryArchive(timezone)
    first = capture_day(archive, day, dc_power=1)
    at = first.start + timedelta(hours=6)
    archive.capture(
        forecast(day, timezone=timezone), at, at, "configuration-a", [SOURCE]
    )
    now = first.end + timedelta(hours=12)
    for record in tuple(archive.records.values()):
        if record.end <= now:
            archive.assess(record.record_id, evidence(record, 12), now)
    return archive, first.record_id, now


def _correct(archive, record_id, energy, now):
    group = correction_group(archive, record_id, now)
    return correct_day(
        archive, record_id, energy, now, expected_revision=correction_revision(group)
    )


def test_correction_is_daily_truth_and_preserves_every_forecast_and_hour():
    archive, record_id, now = _archive()
    before = deepcopy(archive.to_dict())
    assert _correct(archive, record_id, 17.125, now)
    group = correction_group(archive, record_id, now)
    assert len(group) == 2
    for record in group:
        assert record.assessment.actual_energy_kwh == 17.125
        assert record.assessment.valid and record.assessment.manual
        assert record.assessment.sources == ()
        assert record.measured_assessment.actual_energy_kwh == 12
        assert record.assessment_revisions[-1] == record.measured_assessment
    assert not archive.assess(
        record_id, evidence(group[0], 25), now + timedelta(minutes=1)
    )
    for previous in before["records"]:
        current = archive.records[previous["record_id"]].to_dict()
        if previous["record_id"] not in {r.record_id for r in group}:
            assert current == previous
        else:
            for key in previous.keys() - {"assessment", "assessment_revisions"}:
                assert current[key] == previous[key]
    metrics = archive.snapshot(now, 7)["horizons"]["daily_previous_18"]
    assert metrics["mae_kwh"] == pytest.approx(24 - 17.125)
    assert metrics["bias_kwh"] == pytest.approx(24 - 17.125)
    day = archive.day_view(now, "configuration-a", date=DAY.isoformat())
    assert day["daily_measurement"]["energy_kwh"] == 17.125
    assert day["daily_measurement"]["measured_energy_kwh"] == 12
    assert day["daily_measurement"]["manual_correction"]


def test_multiple_edits_restart_and_removal_preserve_original_beyond_revision_limit():
    archive, record_id, now = _archive()
    original = archive.records[record_id].assessment
    for i, energy in enumerate([14, 15, 16, 17, 18, 0]):
        instant = now + timedelta(minutes=i)
        assert _correct(archive, record_id, energy, instant)
        assert not _correct(archive, record_id, energy, instant)
        archive = HistoryArchive.from_dict(archive.to_dict(), "UTC")
        assert archive.records[record_id].measured_assessment == original
        assert len(archive.records[record_id].assessment_revisions) <= 3
    assert archive.records[record_id].assessment.actual_energy_kwh == 0
    assert _correct(archive, record_id, None, now + timedelta(minutes=10))
    current = archive.records[record_id]
    assert current.assessment == replace(
        original, assessed_at=now + timedelta(minutes=10)
    )
    assert not current.assessment.manual
    assert current.measured_assessment is None
    assert (
        HistoryArchive.from_dict(archive.to_dict(), "UTC").to_dict()
        == archive.to_dict()
    )


def test_manual_truth_can_complete_a_missing_day_without_fabricating_hourly_evidence():
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    now = report_after()
    archive.assess(record.record_id, None, now)
    assert _correct(archive, record.record_id, 12, now)
    assert archive.records[record.record_id].assessment.valid
    assert archive.records[record.record_id].measured_assessment.valid is False
    assert _correct(archive, record.record_id, None, now + timedelta(minutes=1))
    assert not archive.records[record.record_id].assessment.valid


@pytest.mark.parametrize("energy", [-1, float("nan"), float("inf"), True, "12"])
def test_invalid_correction_never_changes_archive(energy):
    archive, record_id, now = _archive()
    before = archive.to_dict()
    with pytest.raises(ValueError, match="daily_correction_invalid"):
        _correct(archive, record_id, energy, now)
    assert archive.to_dict() == before


@pytest.mark.parametrize(
    "zone,day,hours",
    [
        ("Europe/Berlin", date(2026, 3, 29), 23),
        ("Europe/Berlin", date(2026, 10, 25), 25),
        ("Asia/Kolkata", DAY, 24),
    ],
)
def test_local_completed_days_keep_absolute_bounds(zone, day, hours):
    archive, record_id, now = _archive(timezone=zone, day=day)
    record = archive.records[record_id]
    assert (record.end - record.start).total_seconds() == hours * 3600
    with pytest.raises(ValueError, match="daily_correction_unavailable"):
        _correct(archive, record_id, 5, record.end - timedelta(microseconds=1))
    assert _correct(archive, record_id, 5, now)
    assert (
        HistoryArchive.from_dict(archive.to_dict(), zone).records[record_id].start
        == record.start
    )


def test_stale_dialog_source_deletion_and_expired_days_cannot_restore_values():
    archive, record_id, now = _archive()
    revision = correction_revision(correction_group(archive, record_id, now))
    _correct(archive, record_id, 17, now)
    with pytest.raises(ValueError, match="daily_correction_changed"):
        correct_day(archive, record_id, 19, now, expected_revision=revision)
    with pytest.raises(ValueError, match="daily_correction_unavailable"):
        _correct(archive, record_id, 19, now + timedelta(days=366))
    assert archive.delete_measurement_source(SOURCE.source_id)
    assert archive.records[record_id].assessment is None
    assert archive.records[record_id].measured_assessment is None
    assert archive.records[record_id].assessment_revisions == ()
    assert not correction_groups(archive, now)
    HistoryArchive.from_dict(archive.to_dict(), "UTC")


def test_source_change_does_not_share_correction_between_daily_cutoffs():
    archive, record_id, now = _archive()
    second = next(
        r for r in correction_group(archive, record_id, now) if r.record_id != record_id
    )
    archive.records[second.record_id] = replace(
        second, measurement_sources=(replace(SOURCE, registry_id="new-meter"),)
    )
    _correct(archive, record_id, 19, now)
    assert archive.records[second.record_id].assessment.actual_energy_kwh == 12
    assert not archive.records[second.record_id].assessment.manual


@pytest.mark.parametrize("version", range(1, 7))
async def test_lossless_store_migration_does_not_invent_manual_truth(hass, version):
    archive, _, _ = _archive()
    payload = {"archive": archive.to_dict(), "last_fetched_at": None}
    assert (
        await _history_store(hass, f"old-{version}")._async_migrate_func(
            version, 1, deepcopy(payload)
        )
        == payload
    )
    assert all(
        "manual" not in (r["assessment"] or {}) for r in payload["archive"]["records"]
    )


def test_persistence_rejects_forged_hour_correction_and_damaged_original():
    archive, record_id, now = _archive()
    _correct(archive, record_id, 13, now)
    data = archive.to_dict()
    daily = next(r for r in data["records"] if r["record_id"] == record_id)
    daily["measured_assessment"]["actual_energy_kwh"] = 1
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(data, "UTC")
    data = archive.to_dict()
    hour = next(r for r in data["records"] if r["horizon"] == "hourly_1h")
    hour["assessment"] = archive.records[record_id].assessment.to_dict()
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(data, "UTC")


def test_manual_confirmation_survives_derived_gap_revalidation():
    archive, record_id, now = _archive()
    for record in correction_group(archive, record_id, now):
        source = replace(
            record.assessment.sources[0], quality_flags=("derived_energy", "gap")
        )
        archive.records[record.record_id] = replace(
            record, assessment=replace(record.assessment, sources=(source,))
        )
    assert _correct(archive, record_id, 13, now)
    assert not archive.invalidate_derived_gaps(now)
    assert archive.records[record_id].assessment.actual_energy_kwh == 13
    _correct(archive, record_id, None, now + timedelta(minutes=1))
    assert archive.records[record_id].assessment.valid is False
    assert archive.records[record_id].assessment.reasons == ("derived_measurement_gap",)


def test_late_manual_correction_keeps_the_band_training_revision_known_at_the_time():
    from .test_uncertainty import evaluate, records

    values = records()
    original = evaluate(values)
    archive = HistoryArchive("UTC")
    archive.records = {r.record_id: r for r in values}
    _correct(archive, values[0].record_id, 900, values[80].end)
    result = evaluate(list(archive.records.values()))
    assert result["evaluation"] == original["evaluation"]
    assert result["upper_kwh"] == original["upper_kwh"]


def _runtime(hass):
    entry = _entry(hass, enabled=False, source=SOURCE)
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, entry, coordinator, None)
    manager._archive, record_id, now = _archive()
    manager._loaded = True
    entry.runtime_data = SimpleNamespace(history=manager)
    return entry, coordinator, manager, record_id, now


async def test_runtime_persists_and_reconciles_without_reload_or_http(
    hass, freezer, aioclient_mock
):
    entry, coordinator, manager, record_id, now = _runtime(hass)
    freezer.move_to(now)
    original_options = dict(entry.options)
    learning = CalibrationManager(hass, entry, coordinator, manager)
    before = next(d for d in learning._days() if d.record_id == record_id)
    manager.calibration = Mock()
    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as reload:
        await manager.async_correct_day(
            record_id,
            17,
            expected_revision=correction_revision(
                correction_group(manager._archive, record_id, now)
            ),
        )
        reload.assert_not_called()
    after = next(d for d in learning._days() if d.record_id == record_id)
    assert after.actual_energy_kwh == 17 and after.valid
    assert after.evidence_fingerprint != before.evidence_fingerprint
    manager.calibration.async_reconcile.assert_called_once()
    restored = HistoryArchive.from_dict(
        (await _history_store(hass, entry.entry_id).async_load())["archive"], "UTC"
    )
    assert restored.records[record_id].assessment.actual_energy_kwh == 17
    assert restored.records[record_id].measured_assessment.actual_energy_kwh == 12
    assert dict(entry.options) == original_options
    assert aioclient_mock.mock_calls == []
    await manager.async_stop()


async def _editor(hass, entry):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "daily_correction"}
    )
    return await manager.async_configure(result["flow_id"], {"date": DAY.isoformat()})


async def test_native_editor_confirm_edit_remove_and_cancel(hass, freezer):
    entry, _, manager, record_id, now = _runtime(hass)
    freezer.move_to(now)
    options = dict(entry.options)
    flow = hass.config_entries.options
    result = await _editor(hass, entry)
    assert result["step_id"] == "daily_correction_edit"
    assert result["description_placeholders"]["measured"] == "12.0 kWh"
    result = await flow.async_configure(
        result["flow_id"], {"energy_kwh": 17, "confirm": False}
    )
    assert result["errors"]["base"] == "daily_correction_confirmation_required"
    assert not manager._archive.records[record_id].assessment.manual
    result = await flow.async_configure(
        result["flow_id"], {"energy_kwh": 17, "confirm": True}
    )
    assert result["step_id"] == "daily_correction_saved"
    flow.async_abort(result["flow_id"])
    for value in (18.135, 0):
        result = await _editor(hass, entry)
        result = await flow.async_configure(
            result["flow_id"], {"energy_kwh": value, "confirm": True}
        )
        assert result["step_id"] == "daily_correction_saved"
        assert manager._archive.records[record_id].assessment.actual_energy_kwh == value
        flow.async_abort(result["flow_id"])
    result = await _editor(hass, entry)
    flow.async_abort(result["flow_id"])
    assert manager._archive.records[record_id].assessment.actual_energy_kwh == 0
    result = await _editor(hass, entry)
    result = await flow.async_configure(
        result["flow_id"], {"remove": True, "confirm": True}
    )
    assert result["step_id"] == "daily_correction_saved"
    assert manager._archive.records[record_id].assessment.actual_energy_kwh == 12
    flow.async_abort(result["flow_id"])
    assert dict(entry.options) == options
    await manager.async_stop()


async def test_two_editors_cannot_overwrite_more_recent_confirmation(hass, freezer):
    entry, _, manager, record_id, now = _runtime(hass)
    freezer.move_to(now)
    first = await _editor(hass, entry)
    second = await _editor(hass, entry)
    await hass.config_entries.options.async_configure(
        second["flow_id"], {"energy_kwh": 17, "confirm": True}
    )
    result = await hass.config_entries.options.async_configure(
        first["flow_id"], {"energy_kwh": 15, "confirm": True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "daily_correction_changed"
    assert manager._archive.records[record_id].assessment.actual_energy_kwh == 17
    await manager.async_stop()


@pytest.mark.parametrize("change", ["options", "reload", "delete"])
async def test_open_editor_cannot_write_after_its_context_changes(
    hass, freezer, change
):
    entry, _, manager, record_id, now = _runtime(hass)
    freezer.move_to(now)
    result = await _editor(hass, entry)
    if change == "options":
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"inverter_max_power_kw": 5}
        )
    elif change == "reload":
        entry.runtime_data = SimpleNamespace(
            history=ArchiveManager(hass, entry, _Coordinator(), None)
        )
    else:
        await manager.async_delete_measurement_source(SOURCE.source_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"energy_kwh": 18, "confirm": True}
    )
    assert result["type"] is FlowResultType.ABORT
    current = manager._archive.records[record_id].assessment
    assert current is None or not current.manual
    await manager.async_stop()


@pytest.mark.parametrize("condition", ["missing", "unloaded", "storage"])
async def test_missing_or_unreadable_archive_cannot_be_corrected(
    hass, freezer, condition
):
    entry, _, manager, _record_id, now = _runtime(hass)
    freezer.move_to(now)
    if condition == "missing":
        entry.runtime_data = None
    elif condition == "unloaded":
        manager._loaded = False
    else:
        manager._storage_error = "storage_unavailable"
    with patch.object(manager._store, "async_save_checked") as save:
        result = await _editor(hass, entry)
        assert result["step_id"] == "daily_correction"
        assert result["errors"]["base"] == "history_unavailable"
        save.assert_not_called()
    await manager.async_stop()


async def test_failed_write_is_not_confirmed_and_same_value_can_be_retried(
    hass, freezer
):
    entry, _, manager, _record_id, now = _runtime(hass)
    freezer.move_to(now)
    result = await _editor(hass, entry)
    with patch.object(manager._store, "_async_write_data", side_effect=OSError("disk")):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"energy_kwh": 17, "confirm": True}
        )
    assert result["errors"]["base"] == "daily_correction_save_failed"
    assert manager._dirty
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"energy_kwh": 17, "confirm": True}
    )
    assert result["step_id"] == "daily_correction_saved"
    assert (await manager._store.async_load())["archive"]["records"]
    await manager.async_stop()


async def test_mutation_in_progress_blocks_second_correction(hass, freezer):
    _, _, manager, record_id, now = _runtime(hass)
    freezer.move_to(now)
    revision = correction_revision(correction_group(manager._archive, record_id, now))
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pending(_):
        entered.set()
        await resume.wait()

    with patch.object(manager._store, "async_save_checked", side_effect=pending):
        task = asyncio.create_task(
            manager.async_correct_day(record_id, 17, expected_revision=revision)
        )
        await entered.wait()
        with pytest.raises(HomeAssistantError):
            await manager.async_correct_day(record_id, 18, expected_revision=revision)
        resume.set()
        await task
    assert manager._archive.records[record_id].assessment.actual_energy_kwh == 17
    await manager.async_stop()


async def test_unload_while_waiting_for_assessment_cancels_the_correction(
    hass, freezer
):
    _, _, manager, record_id, now = _runtime(hass)
    freezer.move_to(now)
    revision = correction_revision(correction_group(manager._archive, record_id, now))
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pending():
        entered.set()
        await resume.wait()

    with patch.object(manager, "_async_cancel_assessment", side_effect=pending):
        task = asyncio.create_task(
            manager.async_correct_day(record_id, 17, expected_revision=revision)
        )
        await entered.wait()
        manager._stopped = True
        resume.set()
        with pytest.raises(HomeAssistantError, match="entladen"):
            await task
    assert not manager._archive.records[record_id].assessment.manual
    await manager.async_stop()
