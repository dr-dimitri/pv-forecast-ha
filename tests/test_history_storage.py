"""Inkrementelle Archivgrößen, entkoppelte Generationen und native Dateigrenzen."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers.json import prepare_save_json

from custom_components.pv_forecast.history import ArchiveRecord, HistoryArchive
from custom_components.pv_forecast.history_corrections import (
    correct_day,
    correction_group,
    correction_revision,
)
from custom_components.pv_forecast.history_storage import ArchiveStorage

from .test_history import DAY, SOURCE, capture_day, evidence, report_after
from .test_storage_writes import NATIVE_WRITE


def _encode(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False).encode()


def _native(data, storage):
    return prepare_save_json({**storage._envelope, "data": data})[1]


def test_unchanged_records_are_prepared_once_and_never_fully_serialized():
    """Prune und anschließender Snapshot teilen alle unveränderten Recordbausteine."""
    archive = HistoryArchive("UTC")
    for offset in range(20):
        capture_day(archive, DAY + timedelta(days=offset), dc_power=1)
    now = report_after(DAY + timedelta(days=21))
    original = archive.to_dict()
    converted = []
    serialize = ArchiveRecord.to_dict

    def counted(record):
        converted.append(record.record_id)
        return serialize(record)

    with (
        patch.object(ArchiveRecord, "to_dict", counted),
        patch.object(archive, "to_dict", side_effect=AssertionError("Vollkopie")),
    ):
        assert not archive.prune(now)
        snapshot = archive.storage_snapshot()
        assert len(converted) == len(archive.records)
        assert not archive.prune(now)
        assert archive.storage_snapshot() == snapshot
        assert len(converted) == len(archive.records)
    assert _encode(snapshot) == _encode(original)
    assert archive._storage_size() == len(_encode(original))
    assert not archive.retention_truncated


def test_assessment_correction_and_source_deletion_replace_only_changed_records():
    """Korrekturen samt Originalbelegen und Revisionen entsprechen der Vollausgabe."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    now = record.end + timedelta(hours=1)
    before = archive.storage_snapshot()
    frozen = _encode(before)
    assert archive.assess(record.record_id, evidence(record, 12.5), now)
    assert _encode(archive.storage_snapshot()) == _encode(archive.to_dict())
    group = correction_group(archive, record.record_id, now)
    assert correct_day(
        archive,
        record.record_id,
        14.0,
        now,
        expected_revision=correction_revision(group),
    )
    assert _encode(archive.storage_snapshot()) == _encode(archive.to_dict())
    assert archive._storage_size() == len(_encode(archive.to_dict()))
    assert archive.delete_measurement_source(SOURCE.source_id)
    assert _encode(archive.storage_snapshot()) == _encode(archive.to_dict())
    assert _encode(before) == frozen
    assert archive._storage_size() == len(_encode(archive.to_dict()))


def test_nested_trial_and_observation_changes_do_not_mutate_old_snapshot():
    """Optionale Dicts bleiben trotz frozen Record von Schreibgenerationen getrennt."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    record = replace(
        record,
        short_term={"references": [{"id": "a"}], "energy_kwh": 1.0},
        temperature_comparison={"mountings": {"roof": "a"}},
    )
    archive.records[record.record_id] = record
    original = archive.storage_snapshot()
    frozen = _encode(original)
    record.short_term["references"][0]["id"] = "b"
    record.temperature_comparison["mountings"]["roof"] = "b"
    archive.underperformance["event"] = {"acknowledged": True}
    archive.note_configuration("new", record.end)
    current = archive.storage_snapshot()
    assert _encode(original) == frozen
    assert _encode(current) == _encode(archive.to_dict())
    assert archive._storage_size() == len(_encode(current))
    archive.records.clear()
    archive.prune(record.end)
    assert archive._storage_records == {}
    assert _encode(original) == frozen


@pytest.mark.parametrize("margin", [0, 1, 2000, 10000])
def test_exact_payload_limit_with_unicode_and_revisions(margin):
    """UTF-8, Listenkommas und geänderte Metadaten zählen auch am exakten Rand."""
    archive = HistoryArchive("UTC")
    for offset in range(12):
        capture_day(archive, DAY + timedelta(days=offset))
    record = next(iter(archive.records.values()))
    archive.records[record.record_id] = replace(
        record, quality_flags=('Überschuss ☀\n"',), raw_energy_kwh=0.00001
    )
    now = report_after(DAY + timedelta(days=13))
    size = len(_encode(archive.to_dict()))
    assert archive.prune(now, max_bytes=size - margin) is bool(margin)
    assert archive._storage_size() == len(_encode(archive.to_dict())) <= size - margin
    assert archive.retention_truncated is bool(margin)


def test_too_small_metadata_limit_is_rejected_without_claiming_record_loss():
    archive = HistoryArchive("UTC")
    limit = len(_encode(archive.to_dict()))
    assert not archive.prune(datetime(2026, 9, 9, tzinfo=UTC), max_bytes=limit)
    with pytest.raises(ValueError, match="Metadaten"):
        archive.prune(datetime(2026, 9, 9, tzinfo=UTC), max_bytes=limit - 1)
    assert not archive.retention_truncated


@pytest.mark.parametrize("max_records", [0, 1, 5, 6000])
def test_native_size_matches_exact_file_and_enforces_both_limits(max_records):
    archive = HistoryArchive("UTC")
    for offset in range(8):
        capture_day(archive, DAY + timedelta(days=offset), dc_power=1)
    now = report_after(DAY + timedelta(days=9))
    storage = ArchiveStorage("pv_forecast.history.Ünicode", 7, 1)
    snapshot = storage.snapshot(
        archive, now, now, max_records=6000, max_bytes=32 * 1024 * 1024
    )
    assert storage.file_size(snapshot) == len(_native(snapshot, storage))
    assert snapshot["archive"] == archive.to_dict()
    # Native Einrückung wird tatsächlich mitgezählt; bloße Nutzlast passt hier.
    limit = len(_encode(snapshot["archive"])) + 1024
    assert len(_native(snapshot, storage)) > limit
    result = storage.snapshot(
        archive, now, now, max_records=max_records, max_bytes=limit
    )
    assert storage.file_size(result) == len(_native(result, storage)) <= limit
    assert len(archive.records) <= max_records
    assert archive.retention_truncated
    assert result["archive"] == archive.to_dict()
    assert set(storage._record_sizes) <= archive.records.keys()


def test_native_limit_rechecks_metadata_and_does_not_change_unmodified_snapshot():
    archive = HistoryArchive("UTC")
    capture_day(archive)
    now = report_after(DAY)
    storage = ArchiveStorage("pv_forecast.history.test", 7, 1)
    original = deepcopy(archive.to_dict())
    data = storage.snapshot(
        archive, now, now, max_records=6000, max_bytes=32 * 1024 * 1024
    )
    exact = storage.file_size(data)
    repeated = storage.snapshot(archive, now, now, max_records=6000, max_bytes=exact)
    assert repeated["archive"] == original
    assert len(_native(repeated, storage)) == exact
    assert not archive.retention_truncated


async def test_archive_worker_keeps_old_snapshot_and_new_generation_pending(
    hass, freezer, tmp_path
):
    """Echte native JSON-Erzeugung liest ausschließlich den entkoppelten Stand."""
    import asyncio
    import threading

    from homeassistant.helpers.storage import Store

    from custom_components.pv_forecast.history_runtime import ArchiveManager

    from .test_history_runtime import NOW, _Coordinator, _entry

    freezer.move_to(NOW)
    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    store = manager._store
    store.path = str(tmp_path / "history")
    await manager.async_start()
    await hass.async_block_till_done()
    record = next(iter(manager._archive.records.values()))
    record = replace(record, short_term={"references": [{"value": "alt"}]})
    manager._archive.records[record.record_id] = record
    entered = asyncio.Event()
    release = threading.Event()
    native_worker = store._write_data
    main_thread = threading.get_ident()
    worker_threads = []

    def delayed_worker(data):
        worker_threads.append(threading.get_ident())
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        native_worker(data)

    try:
        with (
            patch.object(Store, "_async_write_data", NATIVE_WRITE),
            patch.object(store, "_write_data", delayed_worker),
        ):
            write = asyncio.create_task(store._async_handle_write_data())
            await asyncio.wait_for(entered.wait(), timeout=2)
            record.short_term["references"][0]["value"] = "neu"
            manager._archive.underperformance["segment_started_at"] = NOW.isoformat()
            manager._dirty = True
            manager._schedule_save()
            release.set()
            await write
            saved = json.loads((tmp_path / "history").read_text())["data"]
            selected = next(
                row
                for row in saved["archive"]["records"]
                if row["record_id"] == record.record_id
            )
            assert selected["short_term"]["references"][0]["value"] == "alt"
            assert store.write_pending
            await store._async_handle_write_data()
            assert not store.write_pending
        assert worker_threads and main_thread not in worker_threads
        saved = json.loads((tmp_path / "history").read_text())["data"]
        assert saved == manager._serialize()
        assert all(
            row["short_term"]["references"][0]["value"] == "neu"
            for row in saved["archive"]["records"]
            if row["short_term"] is not None
        )
    finally:
        release.set()
        await manager.async_stop()


async def test_native_archive_guard_keeps_previous_file_on_oversized_write(
    hass, tmp_path
):
    """Auch ein direkter nativer Schreibpfad darf keine übergroße Datei ersetzen."""
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store

    from custom_components.pv_forecast.history_runtime import _history_store

    store = _history_store(hass, "test")
    store.path = str(tmp_path / "history")
    with patch.object(Store, "_async_write_data", NATIVE_WRITE):
        await store.async_save_checked({"retained": True})
        previous = (tmp_path / "history").read_bytes()
        with (
            patch(
                "custom_components.pv_forecast.history_runtime.MAX_STORAGE_BYTES", 200
            ),
            pytest.raises(HomeAssistantError, match="nicht gespeichert"),
        ):
            await store.async_save_checked({"oversized": "x" * 1000})
        assert (tmp_path / "history").read_bytes() == previous
        assert store.write_pending
        await store.async_save_checked({"retained": True})
        assert not store.write_pending


@pytest.mark.parametrize("action", ["stop", "delete"])
@pytest.mark.parametrize("restore_error", [False, True])
async def test_late_archive_restore_cannot_undo_unload_or_deletion(
    hass, freezer, action, restore_error
):
    """Ein abgelöster Lade-Worker darf weder Daten noch Listener zurückbringen."""
    import asyncio
    import threading
    from unittest.mock import AsyncMock

    from custom_components.pv_forecast import history_runtime
    from custom_components.pv_forecast.history_runtime import ArchiveManager

    from .test_history_runtime import NOW, _Coordinator, _entry

    freezer.move_to(NOW)
    archive = HistoryArchive("UTC")
    capture_day(archive)
    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    stored = {"archive": archive.to_dict(), "last_fetched_at": None}
    entered, release = asyncio.Event(), threading.Event()
    original = history_runtime._restore_archive

    def delayed_restore(*args):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        if restore_error:
            raise ValueError("Alter Ladevorgang")
        return original(*args)

    try:
        with (
            patch.object(manager._store, "async_load", AsyncMock(return_value=stored)),
            patch.object(history_runtime, "_restore_archive", delayed_restore),
        ):
            task = asyncio.create_task(manager.async_start())
            await asyncio.wait_for(entered.wait(), timeout=2)
            if action == "stop":
                await manager.async_stop()
            else:
                await manager.async_delete_data()
            release.set()
            await task
        assert manager._archive.records == {}
        assert manager.storage_error is None
        assert not manager._store.write_pending
        if action == "stop":
            assert not manager.running
            assert manager.coordinator.listeners == []
        else:
            assert manager._storage._record_sizes == {}
            assert len(manager.coordinator.listeners) == 1
    finally:
        release.set()
        await manager.async_stop()


async def test_overlapping_restore_workers_do_not_share_active_cache(hass, freezer):
    """Nur der neuere Ladevorgang darf seine zuvor ungeteilten Objekte übernehmen."""
    import asyncio
    import threading
    from unittest.mock import AsyncMock

    from custom_components.pv_forecast import history_runtime
    from custom_components.pv_forecast.history_runtime import ArchiveManager

    from .test_history_runtime import NOW, _Coordinator, _entry

    freezer.move_to(NOW)
    archive = HistoryArchive("UTC")
    capture_day(archive)
    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    stored = {"archive": archive.to_dict(), "last_fetched_at": None}
    entered, release = asyncio.Event(), threading.Event()
    original = history_runtime._restore_archive
    caches = []

    def delayed_first_restore(*args):
        caches.append(args[-1])
        if len(caches) == 1:
            hass.loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=5)
        return original(*args)

    try:
        with (
            patch.object(manager._store, "async_load", AsyncMock(return_value=stored)),
            patch.object(history_runtime, "_restore_archive", delayed_first_restore),
        ):
            first = asyncio.create_task(manager.async_start())
            await asyncio.wait_for(entered.wait(), timeout=2)
            await manager.async_start()
            current = manager._archive
            assert len(caches) == 2
            assert caches[0] is not caches[1]
            assert manager._storage is caches[1]
            release.set()
            await first
        assert manager._archive is current
        assert manager._storage is caches[1]
        assert len(manager.coordinator.listeners) == 1
    finally:
        release.set()
        await manager.async_stop()
