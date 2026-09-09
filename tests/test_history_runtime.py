"""Offline-Tests der passiven Archiv-Erfassung, Speicherung und Quellenrechte."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import callback
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pv_forecast.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.history_runtime import (
    ArchiveManager,
    _configuration_id,
    async_delete_history_data,
    async_delete_history_source_data,
)
from custom_components.pv_forecast.measurement_runtime import (
    MeasurementManager,
    async_delete_measurement_source_data,
)
from custom_components.pv_forecast.measurements import (
    SourceConfig,
    SourceHistory,
    aggregate_energy,
)
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    TotalForecastInterval,
)

from .helpers import persisted_roof

NOW = datetime(2026, 9, 8, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_time(freezer):
    freezer.move_to(NOW)


def _forecast(energy=1.0):
    start = NOW.replace(hour=0)
    return ForecastResult(
        start.date(),
        {},
        DailyYield(24 * energy, 24 * energy),
        tuple(
            TotalForecastInterval(
                start + timedelta(hours=hour),
                start + timedelta(hours=hour + 1),
                energy,
                energy,
            )
            for hour in range(48)
        ),
    )


class _Coordinator:
    """Lokale Listener wie beim gemeinsamen Coordinator ohne Abruf oder Timer."""

    def __init__(self):
        self.data = _forecast()
        self.last_update_success = True
        self.last_update_success_time = NOW
        self.listeners = []

    @callback
    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    @callback
    def update(self):
        for listener in tuple(self.listeners):
            listener()


def _entry(hass, *, enabled=True, source=None, comparison=None):
    options = {CONF_ROOFS: [persisted_roof()]}
    if enabled is not None:
        options["history_enabled"] = enabled
    if source is not None:
        options["measurement_sources"] = [source.to_dict()]
    if comparison is not None:
        options["comparison_forecast"] = comparison
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LATITUDE: 52, CONF_LONGITUDE: 13, CONF_TIME_ZONE: "UTC"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def _source(entity_id="sensor.pv_energy", registry_id=None):
    return SourceConfig(
        source_id="source-1",
        entity_id=entity_id,
        registry_id=registry_id,
        kind="total",
        scope="Gesamte AC-PV-Anlage",
        confirmed_pv=True,
        confirmed_disjoint=True,
    )


async def test_legacy_installs_no_listener_and_does_not_access_store(hass):
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, _entry(hass, enabled=None), coordinator, None)
    with (
        patch.object(manager._store, "async_load") as load,
        patch.object(manager._store, "async_save") as save,
    ):
        await manager.async_start()
        await manager.async_stop()
        load.assert_not_called()
        save.assert_not_called()
    assert coordinator.listeners == []


async def test_minute_listener_deduplicates_forecast_without_network(
    hass, freezer, aioclient_mock
):
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, _entry(hass), coordinator, None)
    try:
        with patch.object(
            manager._archive, "capture", wraps=manager._archive.capture
        ) as capture:
            await manager.async_start()
            for minute in (1, 2, 3):
                freezer.move_to(NOW + timedelta(minutes=minute))
                coordinator.update()
            capture.assert_called_once()
            assert manager._archive.records
            assert aioclient_mock.call_count == 0
            assert len(coordinator.listeners) == 1
    finally:
        await manager.async_stop()
    assert coordinator.listeners == []


async def test_restart_keeps_frozen_candidate_and_deduplication(hass, freezer):
    entry = _entry(hass)
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, entry, coordinator, None)
    await manager.async_start()
    frozen = manager._archive.to_dict()
    await manager.async_stop()
    freezer.move_to(NOW + timedelta(minutes=30))
    restarted = ArchiveManager(hass, entry, coordinator, None)
    try:
        await restarted.async_start()
        restored = restarted._archive.to_dict()
        assert restored["records"] == frozen["records"]
        assert restored["configuration_changes"] == frozen["configuration_changes"]
        assert restarted._last_fetched_at == NOW
    finally:
        await restarted.async_stop()


async def test_paused_archive_remains_readable_without_writes(hass):
    entry = _entry(hass)
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, entry, coordinator, None)
    await manager.async_start()
    expected = manager._archive.to_dict()
    await manager.async_stop()
    options = dict(entry.options)
    options["history_enabled"] = False
    hass.config_entries.async_update_entry(entry, options=options)
    paused = ArchiveManager(hass, entry, coordinator, None)
    with (
        patch.object(paused._store, "async_save") as save,
        patch.object(paused._store, "async_delay_save") as delay,
    ):
        await paused.async_start()
        assert paused._archive.to_dict() == expected
        assert paused.snapshot()["enabled"] is False
        assert paused.running is False
        await paused.async_stop()
        save.assert_not_called()
        delay.assert_not_called()
    assert coordinator.listeners == []


async def test_future_version_is_preserved_until_explicit_full_deletion(hass):
    entry = _entry(hass)
    coordinator = _Coordinator()
    future = Store(hass, 2, f"{DOMAIN}.history.{entry.entry_id}")
    await future.async_save({"future": "erhalten"})
    manager = ArchiveManager(hass, entry, coordinator, None)
    entry.runtime_data = SimpleNamespace(history=manager)
    try:
        await manager.async_start()
        assert not manager.running
        assert manager.snapshot()["storage_error"] is not None
        assert await future.async_load() == {"future": "erhalten"}
        await async_delete_history_data(hass, entry)
        assert manager.snapshot()["storage_error"] is None
        assert not manager._archive.records
        assert manager.running
    finally:
        await manager.async_stop()


async def test_continuous_changes_use_one_fixed_five_minute_write(
    hass, freezer, hass_storage
):
    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    try:
        await manager.async_start()
        for minute in (1, 2, 3, 4):
            freezer.move_to(NOW + timedelta(minutes=minute))
            manager.coordinator.last_update_success_time = NOW + timedelta(
                minutes=minute
            )
            manager.coordinator.update()
        assert manager._store.key not in hass_storage
        freezer.move_to(NOW + timedelta(minutes=5))
        async_fire_time_changed(hass, NOW + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert (
            hass_storage[manager._store.key]["data"]["last_fetched_at"]
            == (NOW + timedelta(minutes=4)).isoformat()
        )
    finally:
        await manager.async_stop()


async def test_foreign_forecast_uses_confirmed_local_states_and_freezes_value(
    hass, freezer
):
    config = {
        "today_entity_id": "sensor.foreign_today",
        "tomorrow_entity_id": "sensor.foreign_tomorrow",
        "today_registry_id": None,
        "tomorrow_registry_id": None,
        "scope": "Gesamte AC-PV-Anlage",
        "confirmed_same_boundary": True,
    }
    hass.states.async_set(
        "sensor.foreign_today",
        "12000",
        {"device_class": "energy", "unit_of_measurement": "Wh"},
    )
    hass.states.async_set(
        "sensor.foreign_tomorrow",
        "15000",
        {"device_class": "energy", "unit_of_measurement": "Wh"},
    )
    manager = ArchiveManager(
        hass, _entry(hass, comparison=config), _Coordinator(), None
    )
    try:
        await manager.async_start()
        record = next(
            record
            for record in manager._archive.records.values()
            if record.horizon == "daily_previous_18"
        )
        assert record.comparison.energy_kwh == 15
        freezer.move_to(NOW + timedelta(hours=2))
        hass.states.async_set(
            "sensor.foreign_tomorrow",
            "50000",
            {"device_class": "energy", "unit_of_measurement": "Wh"},
        )
        manager.coordinator.data = _forecast(2)
        manager.coordinator.last_update_success_time = NOW + timedelta(hours=2)
        manager.coordinator.update()
        assert manager._archive.records[record.record_id].comparison.energy_kwh == 15
        assert "sensor.foreign_tomorrow" in manager.entity_ids
    finally:
        await manager.async_stop()


async def test_invalid_or_stale_foreign_forecast_is_missing(hass, freezer):
    config = {
        "today_entity_id": "sensor.foreign_today",
        "tomorrow_entity_id": "sensor.foreign_tomorrow",
        "today_registry_id": None,
        "tomorrow_registry_id": None,
        "scope": "AC-Anlage",
        "confirmed_same_boundary": True,
    }
    hass.states.async_set(
        "sensor.foreign_today",
        "1",
        {"device_class": "energy", "unit_of_measurement": "kWh"},
    )
    freezer.move_to(NOW + timedelta(hours=3))
    hass.states.async_set(
        "sensor.foreign_tomorrow",
        "nan",
        {"device_class": "energy", "unit_of_measurement": "kWh"},
    )
    manager = ArchiveManager(
        hass, _entry(hass, comparison=config), _Coordinator(), None
    )
    assert manager._comparison(NOW + timedelta(hours=3)) == {}


async def test_historical_registry_rename_and_missing_unregistered_acl(
    hass, entity_registry
):
    registered = entity_registry.async_get_or_create(
        "sensor", "test", "pv", suggested_object_id="pv_energy"
    )
    source = _source(registry_id=registered.id)
    manager = ArchiveManager(hass, _entry(hass, source=source), _Coordinator(), None)
    try:
        await manager.async_start()
        entity_registry.async_update_entity(
            registered.entity_id, new_entity_id="sensor.pv_renamed"
        )
        assert manager.entity_ids == ("sensor.pv_renamed",)
        assert manager.identity_unresolved == ()
        entity_registry.async_remove("sensor.pv_renamed")
        assert manager.identity_unresolved == ("sensor.pv_energy",)
    finally:
        await manager.async_stop()


async def test_local_assessment_and_targeted_source_deletion(hass, freezer):
    source = replace(_source(), kind="daily")
    history = SourceHistory(source, "UTC", 20)
    measurements = Mock()
    measurements.async_snapshot = AsyncMock(
        side_effect=lambda start, end, now: {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sources": [history.snapshot(start, end, now)],
        }
    )
    entry = _entry(hass, source=source)
    manager = ArchiveManager(hass, entry, _Coordinator(), measurements)
    entry.runtime_data = SimpleNamespace(history=manager)
    try:
        await manager.async_start()
        record = next(
            record
            for record in manager._archive.records.values()
            if record.horizon == "hourly_1h"
            and record.start == NOW + timedelta(hours=1)
        )
        history.add_reading(record.start, 0, "kWh")
        history.add_reading(record.end, 0.5, "kWh")
        freezer.move_to(record.end)
        manager.coordinator.update()
        await hass.async_block_till_done()
        assert manager._archive.records[record.record_id].assessment.valid is True
        assert (
            manager._archive.records[record.record_id].assessment.actual_energy_kwh
            == 0.5
        )
        history.add_reading(record.end + timedelta(minutes=5), 0.4, "kWh")
        freezer.move_to(record.end + timedelta(minutes=5))
        manager.coordinator.update()
        await hass.async_block_till_done()
        corrected = manager._archive.records[record.record_id]
        assert corrected.assessment.valid is False
        assert corrected.assessment_revisions[-1].actual_energy_kwh == 0.5
        await async_delete_history_source_data(hass, entry, source.source_id)
        assert manager._archive.records[record.record_id].assessment is None
        manager.coordinator.update()
        await hass.async_block_till_done()
        assert manager._archive.records[record.record_id].assessment is None
    finally:
        await manager.async_stop()


def test_configuration_fingerprint_ignores_names_but_preserves_physical_changes(hass):
    entry = _entry(hass, source=_source(registry_id="stable-registry"))
    before = _configuration_id(entry)
    roof = dict(entry.options[CONF_ROOFS][0])
    roof["name"] = "Neuer Anzeigename"
    options = dict(entry.options)
    options[CONF_ROOFS] = [roof]
    options["measurement_sources"] = [
        _source(entity_id="sensor.renamed", registry_id="stable-registry").to_dict()
    ]
    hass.config_entries.async_update_entry(entry, options=options)
    assert _configuration_id(entry) == before
    roof["installed_power_kwp"] = 20
    options[CONF_ROOFS] = [roof]
    hass.config_entries.async_update_entry(entry, options=options)
    assert _configuration_id(entry) != before


async def test_export_is_read_only_and_includes_utc_and_nested_metadata(hass, freezer):
    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    try:
        await manager.async_start()
        before = manager._archive.to_dict()
        freezer.move_to(NOW + timedelta(days=3))
        exported = manager.export(7, "json")
        csv = manager.export(7, "csv")
        assert exported["records"]
        assert "raw_energy_kwh" in csv
        assert "+00:00" in csv
        assert "measurement_sources" in csv
        assert manager._archive.to_dict() == before
    finally:
        await manager.async_stop()


async def test_deletion_after_unload_does_not_reactivate_capture(hass):
    """Archivlöschung an einer entladenen Anlage darf keinen Listener neu starten."""

    entry = _entry(hass)
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, entry, coordinator, None)
    entry.runtime_data = SimpleNamespace(history=manager)
    await manager.async_start()
    await manager.async_stop()
    try:
        await async_delete_history_data(hass, entry)
        assert coordinator.listeners == []
        assert manager.running is False
    finally:
        await manager.async_stop()


async def test_restored_foreign_forecast_is_not_a_fresh_observation(hass):
    """HA-Wiederherstellung datiert einen unbekannt alten Forecast nicht neu."""

    config = {
        "today_entity_id": "sensor.foreign_today",
        "tomorrow_entity_id": "sensor.foreign_tomorrow",
        "scope": "AC-Anlage",
        "confirmed_same_boundary": True,
    }
    hass.states.async_set(
        "sensor.foreign_today",
        "12",
        {"device_class": "energy", "unit_of_measurement": "kWh", "restored": True},
    )
    manager = ArchiveManager(
        hass, _entry(hass, comparison=config), _Coordinator(), None
    )
    assert manager._comparison(NOW) == {}


def test_power_source_does_not_change_archival_configuration(hass):
    entry = _entry(hass, source=_source())
    before = _configuration_id(entry)
    options = dict(entry.options)
    options["measurement_sources"] = [
        _source().to_dict(),
        replace(_source("sensor.pv_power"), source_id="power", kind="power").to_dict(),
    ]
    hass.config_entries.async_update_entry(entry, options=options)
    assert _configuration_id(entry) == before
    options["measurement_sources"][-1] = replace(
        _source("sensor.pv_other_energy"), source_id="energy-2"
    ).to_dict()
    hass.config_entries.async_update_entry(entry, options=options)
    assert _configuration_id(entry) != before


async def test_large_assessment_yields_and_coalesces_followup_updates(hass):
    """HA-Callbacks laufen zwischen Fenstern ohne parallele Bewertungstasks."""

    measurements = Mock(async_snapshot=AsyncMock(return_value=None))
    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), measurements)
    await manager.async_start()
    await hass.async_block_till_done()
    template = next(iter(manager._archive.records.values()))
    manager._archive.records = {
        str(index): replace(
            template,
            record_id=str(index),
            start=NOW - timedelta(hours=index + 1),
            end=NOW - timedelta(hours=index),
        )
        for index in range(168)
    }
    observed = []
    callbacks_run = 0

    @callback
    def other_ha_callback():
        nonlocal callbacks_run
        callbacks_run += 1

    def assess(*_args):
        observed.append(callbacks_run)
        hass.loop.call_soon(other_ha_callback)
        if len(observed) == 1:
            active = manager._assessment_task
            for _ in range(30):
                manager.coordinator.update()
                assert manager._assessment_task is active
        return False

    try:
        with (
            patch.object(manager._archive, "assess", side_effect=assess),
            patch.object(manager._archive, "prune", return_value=False),
        ):
            manager.coordinator.update()
            await hass.async_block_till_done()
        assert len(observed) == 2 * 168
        assert observed[1] > observed[0]
        assert manager._assessment_task is None
    finally:
        await manager.async_stop()


@pytest.mark.parametrize("action", ["stop", "delete", "source_delete"])
async def test_pending_assessment_is_cancelled_before_lifecycle_changes(
    hass, freezer, action
):
    """Entladen und Löschen warten nicht auf eine hängende Messbewertung."""

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_snapshot(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    manager = ArchiveManager(
        hass,
        _entry(hass, source=_source()),
        _Coordinator(),
        Mock(async_snapshot=AsyncMock(side_effect=pending_snapshot)),
    )
    try:
        await manager.async_start()
        freezer.move_to(NOW + timedelta(hours=6))
        manager.coordinator.update()
        await entered.wait()
        if action == "stop":
            await manager.async_stop()
        elif action == "delete":
            await manager.async_delete_data()
        else:
            await manager.async_delete_measurement_source("source-1")
        assert cancelled.is_set()
        assert manager._assessment_task is None
    finally:
        await manager.async_stop()


async def test_async_measurement_snapshot_reuses_sources_and_yields_between_them(hass):
    """Historische Energie vor einem Wechsel zu Leistung nur einmal lesen."""

    first, second = _source(), replace(_source("sensor.second"), source_id="second")
    entry = _entry(hass, source=first)
    options = dict(entry.options)
    options["measurement_sources"] = [first.to_dict(), second.to_dict()]
    hass.config_entries.async_update_entry(entry, options=options)
    manager = MeasurementManager(hass, entry)
    for history in manager._histories.values():
        history.add_reading(NOW - timedelta(hours=1), 0, "kWh")
        history.add_reading(NOW, 1, "kWh")
    manager._histories[first.source_id].replace_source(replace(first, kind="power"))
    expected = aggregate_energy(
        manager._histories.values(), NOW - timedelta(hours=1), NOW, NOW
    )
    original = SourceHistory.snapshot
    calls = []
    progress = []

    def read(history, *args):
        calls.append((history.source.source_id, len(progress)))
        hass.loop.call_soon(progress.append, history.source.source_id)
        return original(history, *args)

    with patch.object(SourceHistory, "snapshot", autospec=True, side_effect=read):
        result = await manager.async_snapshot(NOW - timedelta(hours=1), NOW, NOW)
    assert len(calls) == 2
    assert calls[1][1] > calls[0][1]
    assert result["total_energy"] == expected
    assert result == manager.snapshot(NOW - timedelta(hours=1), NOW, NOW)


async def test_immediate_stop_applies_storage_limits_before_pending_assessment(hass):
    """Auch eine vor der Bewertung angeforderte Speicherung wahrt die harte Grenze."""

    manager = ArchiveManager(hass, _entry(hass), _Coordinator(), None)
    with patch("custom_components.pv_forecast.history_runtime.MAX_RECORDS", 1):
        await manager.async_start()
        assert len(manager._archive.records) > 1
        await manager.async_stop()
    stored = await manager._store.async_load()
    assert len(stored["archive"]["records"]) == 1


async def test_measurement_deletion_after_unload_removes_both_local_stores(hass):
    """Der öffentliche Messlöschpfad entfernt bei Unload auch die Archivkopien."""

    entry = _entry(hass, source=_source())
    coordinator = _Coordinator()
    measurements = MeasurementManager(hass, entry)
    archive = ArchiveManager(hass, entry, coordinator, measurements)
    entry.runtime_data = SimpleNamespace(history=archive, measurements=measurements)
    hass.states.async_set(
        "sensor.pv_energy",
        1,
        {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
        },
    )
    await measurements.async_start()
    await archive.async_start()
    await archive.async_stop()
    await measurements.async_stop()
    await async_delete_measurement_source_data(hass, entry, "source-1")
    assert coordinator.listeners == []
    assert archive._assessment_task is None
    assert not (await measurements._store.async_load())["sources"]
    stored = await archive._store.async_load()
    assert stored["archive"]["records"]
    assert all(
        not record["measurement_sources"] for record in stored["archive"]["records"]
    )
