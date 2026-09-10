"""Echte HA-Serialisierung, fehlgeschlagene Dateischreibung und Wiederaufnahme."""

import asyncio
import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pv_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.pv_forecast.health import check_health
from custom_components.pv_forecast.health_runtime import capture_health
from custom_components.pv_forecast.history_runtime import ArchiveManager
from custom_components.pv_forecast.measurement_runtime import MeasurementManager
from custom_components.pv_forecast.storage import ConfirmedStore

from .test_calibration_runtime import _managers
from .test_history_runtime import NOW, _Coordinator, _entry, _source
from .test_measurement_runtime import START, _report

# Die HA-Fixture ersetzt diese Methode durch einen reinen Speicher-Mock.
NATIVE_WRITE = Store._async_write_data
NATIVE_LOAD = Store._async_load
NATIVE_REMOVE = Store.async_remove


@pytest.fixture
def native_writes():
    """Nur lokale Dateien; Netz und reale HA-Anwenderdaten bleiben unbeteiligt."""
    with (
        patch.object(Store, "_async_write_data", NATIVE_WRITE),
        patch.object(Store, "async_remove", NATIVE_REMOVE),
    ):
        yield


async def _start(hass, kind, tmp_path):
    history = None
    if kind == "archive":
        manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    elif kind == "learning":
        _, _, history, manager = _managers(hass)
        history._store.path = str(tmp_path / "archive")
        await history.async_start()
    else:
        manager = MeasurementManager(hass, _entry(hass, source=_source()))
    manager._store.path = str(tmp_path / kind)
    await manager.async_start()
    await hass.async_block_till_done()
    return manager, history


@pytest.mark.parametrize("kind", ["archive", "learning", "measurements"])
async def test_failed_write_survives_until_unload(
    hass, freezer, tmp_path, native_writes, kind
):
    """Ohne neue Meldung wird der fehlgeschlagene Stand beim Entladen gesichert."""
    freezer.move_to(NOW)
    manager, history = await _start(hass, kind, tmp_path)
    store = manager._store
    with patch.object(store, "_write_prepared_data", side_effect=WriteError("voll")):
        await store._async_handle_write_data()
    assert manager.storage_error == "storage_unavailable"
    assert manager._storage_error is None  # Kein dauerhafter Ladesperrgrund.
    assert store.write_pending
    if kind != "measurements":
        assert manager._dirty
    if kind == "archive":
        assert manager.snapshot(7, NOW)["storage_error"] == "storage_unavailable"
    if kind == "learning":
        assert manager.snapshot()["status"] == "storage_unavailable"
    await manager.async_stop()
    assert manager.storage_error is None
    assert not store.write_pending
    assert store._delay_handle is None
    if kind != "measurements":
        assert not manager._dirty
    saved = json.loads((tmp_path / kind).read_text())
    assert saved["version"] == store.version
    assert saved["data"]
    # Eine neue Storeinstanz liest ausschließlich die wirklich geschriebene Datei.
    fresh = Store(hass, store.version, store.key)
    fresh.path = store.path
    with patch.object(Store, "_async_load", NATIVE_LOAD):
        assert await fresh.async_load() == saved["data"]
    if history:
        await history.async_stop()


@pytest.mark.parametrize(
    "kind,delay", [("archive", 300), ("learning", 300), ("measurements", 300)]
)
async def test_failed_write_retries_at_existing_cadence(
    hass, freezer, tmp_path, native_writes, kind, delay
):
    """Ein transienter Fehler benötigt keine neue fachliche Änderung."""
    freezer.move_to(NOW)
    manager, history = await _start(hass, kind, tmp_path)
    store = manager._store
    original_write = store._write_prepared_data
    failures = 2

    def write(*args):
        nonlocal failures
        if failures:
            failures -= 1
            raise WriteError("voll")
        original_write(*args)

    with patch.object(store, "_write_prepared_data", side_effect=write) as writer:
        await store._async_handle_write_data()
        for attempt in (1, 2):
            freezer.move_to(NOW + timedelta(seconds=delay * attempt - 1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert writer.call_count == attempt
            freezer.move_to(NOW + timedelta(seconds=delay * attempt))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert writer.call_count == attempt + 1
        assert manager.storage_error is None
        assert not store.write_pending
        assert store._delay_handle is None
    await manager.async_stop()
    if history:
        await history.async_stop()


async def test_new_generation_stays_pending_during_old_write(hass, tmp_path):
    """Eine alte erfolgreiche Schreibung darf neuere Änderungen nicht quittieren."""
    store = ConfirmedStore(hass, 1, "pv_forecast.test")
    store.path = str(tmp_path / "store")
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed_write(data):
        started.set()
        await finish.wait()

    with patch.object(Store, "_async_write_data", side_effect=delayed_write):
        task = asyncio.create_task(store.async_save({"value": 1}))
        await started.wait()
        store.async_delay_save(lambda: {"value": 2}, 300)
        finish.set()
        await task
    assert store.write_pending
    await store._async_handle_write_data()
    assert not store.write_pending
    assert (await store.async_load())["value"] == 2


async def test_checked_mutation_reports_write_failure(hass, tmp_path, native_writes):
    store = ConfirmedStore(hass, 1, "pv_forecast.test")
    store.path = str(tmp_path / "store")
    await store.async_save_checked({"keep": True})
    with patch.object(store, "_write_prepared_data", side_effect=WriteError("voll")):
        with pytest.raises(HomeAssistantError, match="nicht gespeichert"):
            await store.async_save_checked({"keep": False})
    assert json.loads((tmp_path / "store").read_text())["data"]["keep"] is True
    assert store.write_error == "storage_unavailable"
    await store.async_save_checked({"keep": False})
    assert json.loads((tmp_path / "store").read_text())["data"]["keep"] is False
    assert store.write_error is None


async def test_remove_waits_for_old_write_and_cancels_retries(
    hass, tmp_path, native_writes
):
    store = ConfirmedStore(hass, 1, "pv_forecast.test")
    store.path = str(tmp_path / "store")
    store.async_track_writes(None, 300)
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed_write(data):
        started.set()
        await finish.wait()
        await NATIVE_WRITE(store, data)

    with patch.object(Store, "_async_write_data", side_effect=delayed_write):
        task = asyncio.create_task(store.async_save({"old": True}))
        await started.wait()
        remove = asyncio.create_task(store.async_remove())
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(task, remove)
    assert not (tmp_path / "store").exists()
    assert not store.write_pending
    assert store._data is None
    assert store._delay_handle is None


@pytest.mark.parametrize("kind", ["archive", "measurements"])
async def test_unload_with_persistent_error_leaves_no_retry(
    hass, freezer, tmp_path, native_writes, kind
):
    freezer.move_to(NOW)
    manager, _ = await _start(hass, kind, tmp_path)
    with patch.object(
        manager._store, "_write_prepared_data", side_effect=WriteError("voll")
    ) as writer:
        await manager._store._async_handle_write_data()
        await manager.async_stop()
        assert writer.call_count == 2
    assert manager.storage_error == "storage_unavailable"
    if kind == "archive":
        assert manager._dirty
    assert manager._store.write_pending
    assert manager._store._delay_handle is None


async def test_new_data_does_not_postpone_retry(hass, freezer, tmp_path, native_writes):
    freezer.move_to(NOW)
    manager, _ = await _start(hass, "archive", tmp_path)
    store = manager._store
    with patch.object(store, "_write_prepared_data", side_effect=WriteError("voll")):
        await store._async_handle_write_data()
    deadline = store._delay_handle.when()
    freezer.move_to(NOW + timedelta(seconds=290))
    manager._dirty = True
    manager._schedule_save()
    assert store._delay_handle.when() == deadline
    freezer.move_to(NOW + timedelta(seconds=300))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert manager.storage_error is None
    assert not store.write_pending
    await manager.async_stop()


async def test_write_failure_is_visible_in_health_and_private_diagnostics(
    hass, freezer, tmp_path, native_writes
):
    freezer.move_to(NOW)
    manager, _ = await _start(hass, "archive", tmp_path)
    entry = manager.entry
    entry.mock_state(hass, ConfigEntryState.LOADED)
    manager.coordinator.last_exception = None
    entry.runtime_data = SimpleNamespace(
        coordinator=manager.coordinator,
        history=manager,
        measurements=None,
        calibration=None,
    )
    with patch.object(
        manager._store,
        "_write_prepared_data",
        side_effect=WriteError("PRIVATER DATEIPFAD"),
    ):
        await manager._store._async_handle_write_data()
    findings = {f.code for f in check_health(capture_health(hass, entry, NOW), NOW)}
    assert "archive_store" in findings
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["history"]["storage_error"] == "storage_unavailable"
    assert "PRIVATER DATEIPFAD" not in json.dumps(diagnostics)
    await manager.async_stop()
    findings = {f.code for f in check_health(capture_health(hass, entry, NOW), NOW)}
    assert "archive_store" not in findings
    assert (await async_get_config_entry_diagnostics(hass, entry))["history"][
        "storage_error"
    ] is None


@pytest.mark.parametrize("kind", ["archive", "measurements", "acknowledge"])
async def test_explicit_manager_mutation_is_only_confirmed_after_write(
    hass, freezer, tmp_path, native_writes, kind
):
    freezer.move_to(NOW)
    entry = _entry(hass, source=_source())
    manager = (
        MeasurementManager(hass, entry)
        if kind == "measurements"
        else ArchiveManager(hass, entry, _Coordinator(), None)
    )
    manager._store.path = str(tmp_path / "store")
    await manager.async_start()
    await hass.async_block_till_done()
    await manager._store._async_handle_write_data()

    async def mutate():
        if kind == "measurements":
            await manager.async_delete_source_data("source-1")
        elif kind == "archive":
            await manager.async_delete_measurement_source("source-1")
        else:
            await manager.async_observation_control("acknowledge")

    with patch.object(
        manager._store, "_write_prepared_data", side_effect=WriteError("voll")
    ):
        with pytest.raises(HomeAssistantError):
            await mutate()
        # Auch eine wiederholte Löschung der bereits im RAM entfernten Daten
        # darf den weiterhin nicht gespeicherten Stand nicht bestätigen.
        with pytest.raises(HomeAssistantError):
            await mutate()
    assert manager._store.write_pending
    await mutate()
    assert not manager._store.write_pending
    assert manager.storage_error is None
    await manager.async_stop()


async def test_native_serialization_failure_has_same_status(
    hass, tmp_path, native_writes
):
    store = ConfirmedStore(hass, 1, "pv_forecast.test")
    store.path = str(tmp_path / "store")
    with pytest.raises(HomeAssistantError, match="nicht gespeichert"):
        await store.async_save_checked({"invalid": object()})
    assert store.write_error == "storage_unavailable"
    assert store.write_pending


async def test_measurement_writes_run_every_five_minutes(
    hass, freezer, tmp_path, native_writes
):
    """30-Sekunden-Erfassung erzeugt zwölf reguläre Vollschreibungen pro Stunde."""
    freezer.move_to(START)
    await _report(hass, freezer, 1, minutes=0)
    manager, _ = await _start(hass, "measurements", tmp_path)
    with patch.object(
        manager._store,
        "_write_prepared_data",
        wraps=manager._store._write_prepared_data,
    ) as writer:
        for step in range(1, 121):
            await _report(hass, freezer, 1 + step / 100, minutes=step / 2)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
        assert writer.call_count == 12
        await manager.async_stop()
        assert writer.call_count == 13
    assert not manager._store.write_pending
    assert manager._store._delay_handle is None


async def test_measurement_snapshot_and_json_use_correct_threads(
    hass, freezer, tmp_path, native_writes
):
    """Während JSON entsteht, verändern neue Meldungen den alten Snapshot nicht."""
    freezer.move_to(START)
    await _report(hass, freezer, 1, minutes=0)
    manager, _ = await _start(hass, "measurements", tmp_path)
    store = manager._store
    main_thread = threading.get_ident()
    started = asyncio.Event()
    finish = threading.Event()
    snapshot_threads = []
    json_threads = []
    original_snapshot = manager._serialize
    from homeassistant.helpers import json as json_helper

    original_prepare = json_helper.prepare_save_json

    def snapshot():
        snapshot_threads.append(threading.get_ident())
        return original_snapshot()

    def prepare(*args, **kwargs):
        json_threads.append(threading.get_ident())
        hass.loop.call_soon_threadsafe(started.set)
        assert finish.wait(timeout=10)
        return original_prepare(*args, **kwargs)

    with (
        patch.object(manager, "_serialize", side_effect=snapshot),
        patch.object(json_helper, "prepare_save_json", side_effect=prepare),
    ):
        task = asyncio.create_task(store._async_handle_write_data())
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            history = next(iter(manager._histories.values()))
            history.mark_gap("test_gap")
            await _report(hass, freezer, 1.1, minutes=1)
            expected_new = original_snapshot()
        finally:
            finish.set()
        await task
    assert snapshot_threads == [main_thread]
    assert json_threads and all(thread != main_thread for thread in json_threads)
    saved = json.loads((tmp_path / "measurements").read_text())["data"]
    assert saved != expected_new
    assert store.write_pending
    await store._async_handle_write_data()
    assert json.loads((tmp_path / "measurements").read_text())["data"] == expected_new
    assert not store.write_pending
    await manager.async_stop()


async def test_measurement_final_write_flushes_pending_snapshot(
    hass, freezer, tmp_path, native_writes
):
    """HA-Final-Write bestätigt den letzten Stand vor dem Fünfminutentermin."""
    freezer.move_to(START)
    await _report(hass, freezer, 1, minutes=0)
    manager, _ = await _start(hass, "measurements", tmp_path)
    await _report(hass, freezer, 1.1, minutes=1)
    expected = manager._serialize()
    assert manager._store.write_pending
    assert not (tmp_path / "measurements").exists()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert json.loads((tmp_path / "measurements").read_text())["data"] == expected
    assert not manager._store.write_pending
    assert manager._store._delay_handle is None
    await manager.async_stop()
