"""Standortwechsel bewahren alte Archivtage und trennen deren Vergleichsgrundlage."""

from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.storage import Store

from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.history_runtime import (
    STORAGE_VERSION,
    ArchiveManager,
    _history_store,
)

from .test_history import SOURCE, capture_day, evidence, forecast
from .test_history_runtime import _Coordinator, _entry


def changed_archive():
    """Einen alten DST-Tag und einen späteren Tag am neuen Standort archivieren."""

    old = HistoryArchive("Europe/Berlin")
    old_record = capture_day(old, target=date(2026, 3, 29))
    old.assess(old_record.record_id, evidence(old_record, 10), old_record.end)
    archive = HistoryArchive.from_dict(old.to_dict(), "America/New_York")
    target_day = date(2026, 4, 2)
    observed = datetime.combine(
        target_day - timedelta(days=1), time(18), archive.timezone
    ).astimezone(UTC)
    archive.capture(
        forecast(target_day - timedelta(days=1), timezone=archive.timezone.key),
        observed,
        observed,
        "configuration-b",
        [SOURCE],
    )
    new_record = next(
        record
        for record in archive.records.values()
        if record.target_date == target_day and record.horizon == "daily_previous_18"
    )
    archive.assess(new_record.record_id, evidence(new_record, 8), new_record.end)
    return old, archive, old_record, new_record


def test_original_record_times_revisions_and_zones_survive_an_active_zone_change():
    old, changed, old_record, _ = changed_archive()
    assert changed.timezone.key == "America/New_York"
    for record_id, original in old.records.items():
        assert changed.records[record_id].to_dict() == original.to_dict()
    assert changed.records[old_record.record_id].timezone == "Europe/Berlin"
    assert old_record.end - old_record.start == timedelta(hours=23)
    stored = changed.to_dict()
    assert HistoryArchive.from_dict(stored, "America/New_York").to_dict() == stored


def test_current_metrics_are_separate_from_old_configuration_and_zone_groups():
    _, archive, old_record, new_record = changed_archive()
    now = datetime(2026, 4, 3, 12, tzinfo=UTC)
    result = archive.snapshot(now, 7, include_records=True)
    current = result["horizons"]["daily_previous_18"]
    assert result["configuration_id"] == "configuration-b"
    assert current["count_valid"] == 1
    assert current["mae_kwh"] == 16
    assert current["bias_kwh"] == 16
    groups = result["configuration_groups"]
    old_group = next(group for group in groups if group["timezone"] == "Europe/Berlin")
    assert old_group["active"] is False
    assert old_group["configuration_id"] == "configuration-a"
    assert old_group["horizons"]["daily_previous_18"]["count_valid"] == 1
    assert old_group["horizons"]["daily_previous_18"]["mae_kwh"] == 13
    by_id = {record["record_id"]: record for record in result["records"]}
    assert by_id[old_record.record_id]["timezone"] == "Europe/Berlin"
    assert by_id[new_record.record_id]["timezone"] == "America/New_York"


def test_changed_configuration_without_new_data_has_empty_current_metrics():
    _, archive, _, _ = changed_archive()
    result = archive.snapshot(
        datetime(2026, 4, 3, 12, tzinfo=UTC),
        7,
        configuration_id="new-without-data",
    )
    assert result["horizons"]["daily_previous_18"]["count_valid"] == 0
    assert result["horizons"]["daily_previous_18"]["mae_kwh"] is None
    assert (
        sum(
            group["horizons"]["daily_previous_18"]["count_valid"]
            for group in result["configuration_groups"]
            if not group["active"]
        )
        == 2
    )


def test_different_configurations_in_the_same_zone_do_not_pool_their_metrics():
    old = HistoryArchive("UTC")
    record = capture_day(old, target=date(2026, 3, 29))
    old.assess(record.record_id, evidence(record, 5), record.end)
    old.note_configuration("new-location", record.end + timedelta(hours=1))
    result = old.snapshot(record.end + timedelta(days=1), 7)
    assert result["horizons"]["daily_previous_18"]["count_valid"] == 0
    previous = next(
        group for group in result["configuration_groups"] if not group["active"]
    )
    assert previous["horizons"]["daily_previous_18"]["count_valid"] == 1


@pytest.mark.parametrize("zone", ["Europe/Berlin", "Unbekannt/Defekt"])
def test_each_stored_record_still_requires_its_actual_original_day_bounds(zone):
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    stored = archive.to_dict()
    next(item for item in stored["records"] if item["record_id"] == record.record_id)[
        "timezone"
    ] = zone
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(stored, "America/New_York")


def test_unknown_model_contract_remains_rejected_after_zone_support():
    archive = HistoryArchive("UTC")
    capture_day(archive)
    stored = archive.to_dict()
    stored["records"][0]["model_version"] = "unknown"
    with pytest.raises(ValueError, match="Modellvertrag"):
        HistoryArchive.from_dict(stored, "UTC")


def test_current_hourly_targets_never_reuse_a_former_location():
    archive = HistoryArchive("UTC")
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    archive.capture(forecast(), now, now, "old-place", [SOURCE])
    archive.note_configuration("new-place", now + timedelta(minutes=1))
    assert archive.current_targets(now + timedelta(hours=2))["intervals"] == []
    assert archive.current_targets(now + timedelta(hours=2), "old-place")["intervals"]


def test_retention_counts_days_in_each_records_original_timezone():
    archive = HistoryArchive("Pacific/Honolulu")
    target = date(2026, 1, 1)
    observed = datetime.combine(target, time(8), archive.timezone).astimezone(UTC)
    archive.capture(
        forecast(target, timezone=archive.timezone.key),
        observed,
        observed,
        "configuration-a",
        [SOURCE],
    )
    original = next(
        record
        for record in archive.records.values()
        if record.horizon == "hourly_1h" and record.target_date == target
    )
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    now = datetime.combine(target + timedelta(days=91), time.min, UTC)
    assert now.astimezone(ZoneInfo(original.timezone)).date() == target + timedelta(
        days=90
    )
    restored.prune(now)
    assert original.record_id in restored.records


@pytest.mark.parametrize("old_version", [1, 2, 3, 4, 5, 6, 7])
async def test_current_store_migrates_previous_versions_without_changing_old_records(
    hass, old_version
):
    archive = HistoryArchive("Europe/Berlin")
    capture_day(archive)
    payload = {"archive": archive.to_dict(), "last_fetched_at": None}
    if old_version == 1:
        for record in payload["archive"]["records"]:
            for key in (
                "basis",
                "calibrated_energy_kwh",
                "applied_factor",
                "applied_candidate_id",
                "candidate_factor",
                "candidate_id",
                "candidate_energy_kwh",
            ):
                record.pop(key)
    for record in payload["archive"]["records"]:
        record.pop("short_term", None)
        record.pop("temperature_comparison", None)
    payload["archive"].pop("underperformance", None)
    before = deepcopy(payload)
    store = _history_store(hass, f"migration-{old_version}")
    assert await store._async_migrate_func(old_version, 1, payload) == before
    assert payload == before
    assert STORAGE_VERSION == 8


async def test_runtime_loads_paused_archive_under_new_zone_without_rewriting_old_data(
    hass,
    freezer,
):
    freezer.move_to("2026-09-10T12:00:00Z")
    entry = _entry(hass, enabled=False)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "time_zone": "America/New_York"}
    )
    old = HistoryArchive("Europe/Berlin")
    capture_day(old)
    key = f"pv_forecast.history.{entry.entry_id}"
    await Store(hass, 2, key).async_save(
        {"archive": old.to_dict(), "last_fetched_at": None}
    )
    manager = ArchiveManager(hass, entry, _Coordinator(), None)
    try:
        await manager.async_start()
        assert manager.loaded
        assert manager._storage_error is None
        assert manager._archive.timezone.key == "America/New_York"
        assert manager._dirty is True
        assert manager._archive._active_configuration_id() != "configuration-a"
        for record_id, record in old.records.items():
            assert manager._archive.records[record_id].to_dict() == record.to_dict()
        assert (
            manager.snapshot()["horizons"]["daily_previous_18"]["count_forecasts"] == 0
        )
    finally:
        await manager.async_stop()
