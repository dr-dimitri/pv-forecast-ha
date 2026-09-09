"""Standortsegmente bewahren Messwerte und deren ursprüngliche Tageszeitzone."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.pv_forecast.calibration_runtime import _calibration_store
from custom_components.pv_forecast.configuration import location_fingerprint
from custom_components.pv_forecast.const import (
    CONF_LOCATION_SOURCE,
    LOCATION_SOURCE_HOME_ASSISTANT,
)
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.history_runtime import (
    _history_store,
    async_delete_history_data,
)
from custom_components.pv_forecast.measurement_runtime import (
    STORAGE_VERSION,
    MeasurementManager,
    _measurement_store,
    _MeasurementStore,
)
from custom_components.pv_forecast.measurements import SourceConfig, SourceHistory
from custom_components.pv_forecast.reconfiguration import (
    ReconfigurationChangedError,
    _async_validate_stores,
    async_prepare_location_change,
)

from .test_history import capture_day
from .test_reconfiguration import FETCH, NOW, _begin, _entry


def _source(kind="total"):
    return SourceConfig("source", "sensor.pv", kind, "AC-PV ohne Speicher")


def test_location_transition_keeps_old_data_without_counter_bridge():
    history = SourceHistory(_source(), "UTC", 20)
    history.bind_location("old-location", "UTC", NOW)
    history.add_reading(NOW, 0, "kWh")
    history.add_reading(NOW + timedelta(hours=1), 1, "kWh")
    old = history.to_dict()
    transition = NOW + timedelta(hours=2)
    history.bind_location("new-location", "Asia/Kolkata", transition)
    new_segment = history.segment_id
    history.add_reading(NOW + timedelta(hours=1), 1, "kWh")
    assert history.latest_reading is None
    history.add_reading(transition, 100, "kWh")
    history.add_reading(transition + timedelta(hours=1), 101, "kWh")
    assert len(history.deltas) == 2
    assert history.deltas[-1].energy_kwh == 1
    assert history.deltas[-1].start == transition
    assert history.segment_id == new_segment
    assert history.to_dict()["readings"][:2] == old["readings"]
    historical = history.snapshot(NOW, NOW + timedelta(hours=1), transition)
    assert historical["energy_kwh"] == 1
    assert historical["segment_contexts"][old["segment_id"]]["timezone"] == "UTC"
    current = history.current_location_view()
    assert (
        current.snapshot(NOW, NOW + timedelta(hours=1), transition)["energy_kwh"]
        is None
    )
    stored = history.to_dict()
    restored = SourceHistory.from_dict(_source(), stored, "Asia/Kolkata", 20)
    assert restored.to_dict() == stored


def test_old_daily_correction_uses_its_original_timezone():
    start = datetime(2026, 9, 9, 0, tzinfo=UTC)
    history = SourceHistory(_source("daily"), "Pacific/Honolulu", 20)
    history.bind_location("hawaii", "Pacific/Honolulu", start)
    for minute, value in ((0, 1), (30, 2), (60, 1)):
        history.add_reading(start + timedelta(minutes=minute), value, "kWh")
    original = history.snapshot(
        start, start + timedelta(hours=1), start + timedelta(hours=1)
    )
    assert "daily_correction" in original["quality_flags"]
    history.bind_location("india", "Asia/Kolkata", start + timedelta(hours=2))
    restored = SourceHistory.from_dict(
        _source("daily"), history.to_dict(), "Asia/Kolkata", 20
    )
    actual = restored.snapshot(
        start, start + timedelta(hours=1), start + timedelta(hours=2)
    )
    assert actual["energy_kwh"] == original["energy_kwh"]
    assert "daily_correction" in actual["quality_flags"]
    assert not actual["energy_complete"]


async def test_legacy_measurement_migration_preserves_every_existing_value(
    hass, hass_storage
):
    entry = _entry(hass)
    history = SourceHistory(_source(), "UTC", 20)
    history.add_reading(NOW, 0, "kWh")
    history.add_reading(NOW + timedelta(minutes=30), 1, "kWh")
    legacy_source = history.to_dict()
    legacy_source.pop("segment_contexts")
    payload = {"sources": {"source": deepcopy(legacy_source)}}
    await Store(hass, 1, f"pv_forecast.measurements.{entry.entry_id}").async_save(
        payload
    )
    store = _measurement_store(hass, entry.entry_id)
    migrated = await store.async_load()
    assert hass_storage[store.key]["version"] == STORAGE_VERSION
    assert hass_storage[store.key]["data"] == migrated
    restored = dict(migrated["sources"]["source"])
    contexts = restored.pop("segment_contexts")
    assert restored == legacy_source
    assert contexts[history.segment_id] == {
        "location_id": location_fingerprint(entry.data),
        "timezone": "UTC",
        "started_at": None,
    }


async def test_runtime_move_keeps_history_but_waits_for_new_report(hass, freezer):
    freezer.move_to(NOW)
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "measurement_sources": [_source().to_dict()],
        },
    )
    attributes = {
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
    }
    hass.states.async_set("sensor.pv", "0", attributes)
    old = MeasurementManager(hass, entry)
    await old.async_start()
    freezer.move_to(NOW + timedelta(minutes=30))
    hass.states.async_set("sensor.pv", "1", attributes)
    await hass.async_block_till_done()
    await old.async_stop()
    previous_segment = old._histories["source"].segment_id
    transition = NOW + timedelta(hours=1)
    freezer.move_to(transition)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "latitude": 35.6,
            "longitude": 139.7,
            "time_zone": "Asia/Tokyo",
        },
    )
    new = MeasurementManager(hass, entry)
    await new.async_start()
    try:
        assert new._histories["source"].segment_id != previous_segment
        assert new.preview("source")["last_valid_value"] is None
        old_reading = new.snapshot(NOW, NOW + timedelta(minutes=30), transition)
        assert old_reading["total_energy"]["energy_kwh"] == 1
        assert old_reading["current_location_total_energy"]["energy_kwh"] is None
        windows = await new.async_interval_windows(
            [(NOW, NOW + timedelta(minutes=30))], transition
        )
        assert windows[0]["energy_kwh"] is None
        hass.states.async_set("sensor.pv", "100", attributes)
        await hass.async_block_till_done()
        assert new.preview("source")["last_valid_value"] == 100
        freezer.move_to(transition + timedelta(minutes=30))
        hass.states.async_set("sensor.pv", "102", attributes)
        await hass.async_block_till_done()
        assert len(new._histories["source"].deltas) == 2
        assert new._histories["source"].deltas[-1].energy_kwh == 2
        end = transition + timedelta(minutes=30)
        current = new.snapshot(NOW, end, end)
        assert current["total_energy"]["energy_kwh"] == 3
        assert current["current_location_total_energy"]["energy_kwh"] == 2
        assert not current["current_location_total_energy"]["energy_complete"]
        historical = await new.async_snapshot(NOW, end, end)
        assert historical == {
            key: value
            for key, value in current.items()
            if key != "current_location_total_energy"
        }
    finally:
        await new.async_stop()


async def test_store_read_detects_changed_entry_context(hass):
    entry = _entry(hass)
    store = _measurement_store(hass, entry.entry_id)

    async def change_while_reading():
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "time_zone": "Asia/Tokyo"}
        )
        return None

    with (
        patch(
            "custom_components.pv_forecast.reconfiguration._measurement_store",
            return_value=store,
        ),
        patch.object(store, "async_load", side_effect=change_while_reading),
        patch.object(store, "async_save") as save,
        pytest.raises(ReconfigurationChangedError),
    ):
        await _async_validate_stores(hass, entry)
    save.assert_not_called()
    assert store.location_data["time_zone"] == "UTC"


async def test_confirmed_archive_deletion_during_prepare_is_not_restored(hass):
    """Eine Löschung während der Prüfung hat Vorrang vor zuvor gelesenen Daten."""

    entry = _entry(hass)
    measurement = SourceHistory(_source(), "UTC", 20)
    measurement.bind_location(location_fingerprint(entry.data), "UTC", NOW)
    await _measurement_store(hass, entry.entry_id).async_save(
        {"sources": {"source": measurement.to_dict()}}
    )
    archive = HistoryArchive("UTC")
    capture_day(archive)
    await _history_store(hass, entry.entry_id).async_save(
        {"archive": archive.to_dict(), "last_fetched_at": None}
    )
    calibration_store = _calibration_store(hass, entry.entry_id)
    original_load = calibration_store.async_load
    loads = 0

    async def delete_while_reading():
        nonlocal loads
        loads += 1
        if loads == 2:
            # Ein anderer Options-Flow bestätigt nach dem Archivlesen die Löschung.
            await async_delete_history_data(hass, entry)
            assert await _history_store(hass, entry.entry_id).async_load() is None
        return await original_load()

    with (
        patch(
            "custom_components.pv_forecast.reconfiguration._calibration_store",
            return_value=calibration_store,
        ),
        patch.object(calibration_store, "async_load", side_effect=delete_while_reading),
    ):
        await async_prepare_location_change(hass, entry)
    assert loads == 2
    assert await _history_store(hass, entry.entry_id).async_load() is None


async def test_failed_migration_write_keeps_old_location_context(hass, freezer):
    """Ein von HA protokollierter Schreibfehler erlaubt keine neue Standortbasis."""

    freezer.move_to(NOW + timedelta(hours=2))
    entry = _entry(hass)
    original = dict(entry.data)
    old = SourceHistory(_source(), "UTC", 20)
    old.add_reading(NOW, 0, "kWh")
    old.add_reading(NOW + timedelta(minutes=30), 1, "kWh")
    legacy = old.to_dict()
    legacy.pop("segment_contexts")
    await Store(hass, 1, f"pv_forecast.measurements.{entry.entry_id}").async_save(
        {"sources": {"source": legacy}}
    )
    await hass.config.async_set_time_zone("Asia/Tokyo")
    with (
        patch(FETCH, return_value={}),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
        patch.object(
            _MeasurementStore,
            "_async_write_data",
            side_effect=WriteError("Datenträger voll"),
        ),
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "reconfigure_storage_unavailable"}
    assert dict(entry.data) == original
    reload_entry.assert_not_called()
    restored = await _measurement_store(hass, entry.entry_id).async_load()
    contexts = restored["sources"]["source"]["segment_contexts"]
    assert contexts[old.segment_id]["location_id"] == location_fingerprint(original)
    assert contexts[old.segment_id]["timezone"] == "UTC"


async def test_corrupt_json_blocks_reconfigure_and_repeated_confirmation(
    hass, tmp_path
):
    """HA-Umbenennung einer defekten Datei erlaubt keinen späteren Standortwechsel."""

    entry = _entry(hass)
    original = dict(entry.data)
    hass.config.config_dir = str(tmp_path)
    path = Path(_measurement_store(hass, entry.entry_id).path)
    path.parent.mkdir(parents=True, exist_ok=True)
    await hass.async_add_executor_job(path.write_text, '{"version": 1, nicht gueltig')

    async def read_real_file(store):
        # Nur den HA-Testfixture-Speichermock umgehen; originale JSON-Behandlung.
        return await store._async_load_data()

    with (
        patch(FETCH, return_value={}),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
        patch.object(Store, "_async_load", read_real_file),
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        for _ in range(2):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            assert result["errors"] == {"base": "reconfigure_storage_unavailable"}
            assert dict(entry.data) == original
        assert not await hass.async_add_executor_job(path.exists)
        assert list(path.parent.glob(path.name + ".corrupt.*"))
        reload_entry.assert_not_called()

        restored = SourceHistory(_source(), "UTC", 20)
        restored.bind_location(location_fingerprint(original), "UTC", NOW)
        payload = {
            "version": STORAGE_VERSION,
            "minor_version": 1,
            "key": _measurement_store(hass, entry.entry_id).key,
            "data": {"sources": {"source": restored.to_dict()}},
        }
        await hass.async_add_executor_job(path.write_text, json.dumps(payload))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["reason"] == "reconfigure_successful"
        assert dict(entry.data) != original
        reload_entry.assert_called_once_with(entry.entry_id)
