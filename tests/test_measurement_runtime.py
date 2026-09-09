"""Tests der passiven HA-Erfassung, Quellenidentität und lokalen Messhistorie."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from freezegun import freeze_time
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, EVENT_STATE_REPORTED
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pv_forecast.const import CONF_ROOFS, CONF_TIME_ZONE, DOMAIN
from custom_components.pv_forecast.measurement_runtime import (
    STORAGE_VERSION,
    MeasurementManager,
    async_delete_measurement_source_data,
    async_remove_measurement_store,
)

from .helpers import persisted_roof

START = datetime(2026, 9, 9, 10, tzinfo=UTC)


def _source(
    source_id: str = "source-1",
    entity_id: str = "sensor.pv_energy",
    registry_id: str | None = None,
    kind: str = "total",
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "entity_id": entity_id,
        "registry_id": registry_id,
        "kind": kind,
        "scope": "Gesamte AC-PV-Anlage",
        "derived_energy": False,
        "max_interval_minutes": 60,
        "confirmed_pv": True,
        "confirmed_disjoint": True,
    }


def _entry(hass, *sources: dict[str, object]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TIME_ZONE: "Europe/Berlin"},
        options={CONF_ROOFS: [persisted_roof()], "measurement_sources": list(sources)},
    )
    entry.add_to_hass(hass)
    return entry


async def _report(
    hass,
    clock,
    value,
    *,
    minutes: int,
    unit="kWh",
    entity_id="sensor.pv_energy",
    **attributes,
):
    clock.move_to(START + timedelta(minutes=minutes))
    hass.states.async_set(
        entity_id,
        value,
        {
            ATTR_UNIT_OF_MEASUREMENT: unit,
            "device_class": "power" if unit in ("W", "kW") else "energy",
            "state_class": "measurement" if unit in ("W", "kW") else "total_increasing",
            **attributes,
        },
    )
    await hass.async_block_till_done()


async def test_unchanged_zero_is_reported_without_polling(hass, aioclient_mock):
    """Auch unveränderte Nullstände belegen Zeitabdeckung ohne Geräteabrufe."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 0, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        await manager.async_start()
        await _report(hass, clock, 0, minutes=30)
        result = manager.snapshot(
            START, START + timedelta(minutes=30), START + timedelta(minutes=30)
        )
        assert result["sources"][0]["energy_kwh"] == 0
        assert result["sources"][0]["complete"] is True
        assert len(result["sources"][0]["deltas"]) == 1
        assert aioclient_mock.call_count == 0
        await manager.async_stop()


async def test_unit_change_keeps_one_counter_and_preview(hass):
    """Wh und kWh beschreiben denselben Zählerstand ohne neues Segment."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1000, minutes=0, unit="Wh")
        manager = MeasurementManager(hass, _entry(hass, _source()))
        await manager.async_start()
        await _report(hass, clock, 1.5, minutes=30)
        result = manager.snapshot(
            START, START + timedelta(minutes=30), START + timedelta(minutes=30)
        )
        assert result["sources"][0]["energy_kwh"] == 0.5
        assert manager.preview("source-1") == {
            "last_valid_value": 1.5,
            "unit": "kWh",
            "timestamp": (START + timedelta(minutes=30)).isoformat(),
        }
        await _report(hass, clock, "unavailable", minutes=40)
        assert manager.preview("source-1")["last_valid_value"] == 1.5
        await manager.async_stop()


async def test_registry_rename_preserves_history_and_resolves_after_restart(
    hass, entity_registry
):
    """Eine bestätigte Registry-ID überlebt Umbenennung und ein späteres Reload."""

    registered = entity_registry.async_get_or_create(
        "sensor", "test", "pv", suggested_object_id="pv_energy"
    )
    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        entry = _entry(hass, _source(registry_id=registered.id))
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        entity_registry.async_update_entity(
            registered.entity_id, new_entity_id="sensor.pv_new_name"
        )
        await hass.async_block_till_done()
        assert manager.entity_ids == ("sensor.pv_new_name",)
        await _report(hass, clock, 2, minutes=30, entity_id="sensor.pv_new_name")
        result = manager.snapshot(
            START, START + timedelta(minutes=30), START + timedelta(minutes=30)
        )
        assert result["sources"][0]["energy_kwh"] == 1
        assert result["sources"][0]["complete"] is True
        await manager.async_stop()
        restarted = MeasurementManager(hass, entry)
        await restarted.async_start()
        assert restarted.entity_ids == ("sensor.pv_new_name",)
        assert restarted.preview("source-1")["last_valid_value"] == 2
        await restarted.async_stop()


async def test_replacement_with_same_entity_id_is_not_accepted(hass, entity_registry):
    """Ein neuer Zähler unter altem Namen wird nicht zur bisherigen Quelle."""

    registered = entity_registry.async_get_or_create(
        "sensor", "test", "pv", suggested_object_id="pv_energy"
    )
    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        manager = MeasurementManager(
            hass, _entry(hass, _source(registry_id=registered.id))
        )
        await manager.async_start()
        entity_registry.async_remove(registered.entity_id)
        hass.states.async_remove(registered.entity_id)
        await hass.async_block_till_done()
        replacement = entity_registry.async_get_or_create(
            "sensor", "test", "pv-new", suggested_object_id="pv_energy"
        )
        assert replacement.entity_id == registered.entity_id
        await _report(hass, clock, 2, minutes=30)
        result = manager.snapshot(
            START, START + timedelta(minutes=30), START + timedelta(minutes=30)
        )
        assert manager.identity_unresolved == ("source-1",)
        assert result["sources"][0]["identity_unresolved"] is True
        assert result["sources"][0]["deltas"] == []
        assert manager.preview("source-1")["last_valid_value"] == 1
        await manager.async_stop()


async def test_restart_preserves_values_but_marks_unobserved_gap(hass):
    """Ein Neustart rekonstruiert keine währenddessen unbekannten Stunden."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 0, minutes=0)
        entry = _entry(hass, _source())
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        await _report(hass, clock, 1, minutes=30)
        await manager.async_stop()
        await _report(hass, clock, 4, minutes=120)
        restarted = MeasurementManager(hass, entry)
        await restarted.async_start()
        result = restarted.snapshot(
            START, START + timedelta(minutes=120), START + timedelta(minutes=120)
        )
        assert restarted.preview("source-1")["last_valid_value"] == 4
        assert result["sources"][0]["complete"] is False
        assert "restart" in result["sources"][0]["quality_flags"]
        hour = restarted.snapshot(
            START + timedelta(minutes=60),
            START + timedelta(minutes=120),
            START + timedelta(minutes=120),
        )
        assert hour["sources"][0]["energy_kwh"] is None
        await restarted.async_stop()


async def test_unload_removes_all_capture_and_flushes_store(hass):
    """Nach Entladen bleiben keine Messlistener oder Wartungstimer aktiv."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 0, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        await manager.async_start()
        await _report(hass, clock, 1, minutes=30)
        await manager.async_stop()
        assert not manager.running
        assert manager._listeners == []
        assert manager._cancel_cleanup is None
        stored = await manager._store.async_load()
        await _report(hass, clock, 2, minutes=60)
        assert manager.preview("source-1")["last_valid_value"] == 1
        assert await manager._store.async_load() == stored
        await manager.async_stop()


async def test_targeted_deletion_preserves_other_source_and_starts_new_segment(hass):
    """Gezieltes Löschen lässt andere Quellen und die eigene Zuordnung bestehen."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 0, minutes=0)
        await _report(hass, clock, 0, minutes=0, entity_id="sensor.pv_second")
        entry = _entry(hass, _source(), _source("source-2", "sensor.pv_second"))
        manager = MeasurementManager(hass, entry)
        entry.runtime_data = SimpleNamespace(measurements=manager)
        await manager.async_start()
        await _report(hass, clock, 1, minutes=30)
        await _report(hass, clock, 2, minutes=30, entity_id="sensor.pv_second")
        segment = manager._histories["source-1"].segment_id
        await async_delete_measurement_source_data(hass, entry, "source-1")
        assert manager.preview("source-1")["last_valid_value"] is None
        assert manager.preview("source-2")["last_valid_value"] == 2
        assert manager._histories["source-1"].segment_id != segment
        await _report(hass, clock, 2, minutes=60)
        assert manager._histories["source-1"].deltas == []
        await manager.async_stop()
        await async_delete_measurement_source_data(hass, entry, "source-1")
        stored = await manager._store.async_load()
        assert set(stored["sources"]) == {"source-2"}
        await async_remove_measurement_store(hass, entry.entry_id)
        assert await Store(hass, 1, manager._store.key).async_load() is None


async def test_late_report_uses_event_timestamp_and_does_not_change_baseline(hass):
    """Der Berichtzeitpunkt gilt auch wenn das State-Objekt schon neuer ist."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 0, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        await manager.async_start()
        await _report(hass, clock, 1, minutes=30)
        state = hass.states.get("sensor.pv_energy")
        hass.bus.async_fire(
            EVENT_STATE_REPORTED,
            {
                "entity_id": state.entity_id,
                "new_state": state,
                "last_reported": START + timedelta(minutes=10),
                "old_last_reported": START,
            },
        )
        await hass.async_block_till_done()
        assert (
            manager.preview("source-1")["timestamp"]
            == (START + timedelta(minutes=30)).isoformat()
        )
        await _report(hass, clock, 2, minutes=60)
        result = manager.snapshot(
            START, START + timedelta(minutes=60), START + timedelta(minutes=60)
        )
        assert result["sources"][0]["energy_kwh"] == 2
        await manager.async_stop()


async def test_empty_configuration_installs_no_store_or_listener(hass):
    """Die unveränderte reine Prognose benötigt keine Messdatenerfassung."""

    manager = MeasurementManager(hass, _entry(hass))
    with (
        patch.object(Store, "async_load") as load,
        patch.object(Store, "async_save") as save,
    ):
        await manager.async_start()
        await manager.async_stop()
        load.assert_not_called()
        save.assert_not_called()
    assert manager.entity_ids == ()
    assert manager._listeners == []


async def test_power_readings_are_never_integrated(hass):
    """Ein optionaler Leistungszähler liefert ausschließlich normierte Leistung."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1000, minutes=0, unit="W")
        manager = MeasurementManager(hass, _entry(hass, _source(kind="power")))
        await manager.async_start()
        await _report(hass, clock, 2000, minutes=30, unit="W")
        result = manager.snapshot(
            START, START + timedelta(minutes=30), START + timedelta(minutes=30)
        )
        assert result["sources"][0]["energy_kwh"] is None
        assert result["sources"][0]["deltas"] == []
        assert manager.preview("source-1")["last_valid_value"] == 2
        assert manager.preview("source-1")["unit"] == "kW"
        await manager.async_stop()


async def test_future_store_version_disables_capture_without_overwrite(hass):
    """Eine unbekannte Datensatzversion wird nicht durch leere Historie ersetzt."""

    with freeze_time(START, real_asyncio=True) as clock:
        entry = _entry(hass, _source())
        future_store = Store(
            hass, STORAGE_VERSION + 1, f"{DOMAIN}.measurements.{entry.entry_id}"
        )
        payload = {"future_data": {"must_survive": True}}
        await future_store.async_save(payload)
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        assert not manager.running
        assert manager._listeners == []
        await _report(hass, clock, 10, minutes=0)
        await manager.async_stop()
        assert await future_store.async_load() == payload
        result = manager.snapshot(START, START + timedelta(minutes=30), START)
        assert result["storage_error"] is not None
        assert result["sources"][0]["deltas"] == []


async def test_store_load_error_does_not_write_or_install_listeners(hass):
    """Ein lokaler Lesefehler lässt Prognose und vorhandenen Speicher unberührt."""

    manager = MeasurementManager(hass, _entry(hass, _source()))
    with (
        patch.object(manager._store, "async_load", side_effect=HomeAssistantError),
        patch.object(manager._store, "async_save") as save,
        patch.object(manager._store, "async_delay_save") as delayed_save,
    ):
        await manager.async_start()
        await manager.async_stop()
        save.assert_not_called()
        delayed_save.assert_not_called()
    assert manager._listeners == []
    assert (
        manager.snapshot(START, START + timedelta(hours=1), START)["storage_error"]
        == "storage_unavailable"
    )


@pytest.mark.parametrize(
    "attributes",
    [{"device_class": "power"}, {"state_class": "measurement"}],
)
async def test_changed_sensor_metadata_creates_gap(hass, attributes):
    """Nach Änderung der Sensorart werden kompatibel aussehende Zahlen verworfen."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        await manager.async_start()
        await _report(hass, clock, 2, minutes=30, **attributes)
        result = manager.snapshot(
            START, START + timedelta(minutes=30), START + timedelta(minutes=30)
        )
        assert result["sources"][0]["energy_kwh"] is None
        assert "invalid_metadata" in result["sources"][0]["quality_flags"]
        assert manager.preview("source-1")["last_valid_value"] == 1
        await manager.async_stop()


async def test_historical_source_identity_is_included_in_access_checks(hass):
    """Aufbewahrte Segmente eines alten Sensors benötigen weiterhin dessen Rechte."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 0, minutes=0)
        entry = _entry(hass, _source())
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        await _report(hass, clock, 1, minutes=30)
        await manager.async_stop()
        options = dict(entry.options)
        options["measurement_sources"] = [_source(entity_id="sensor.pv_replacement")]
        hass.config_entries.async_update_entry(entry, options=options)
        replacement = MeasurementManager(hass, entry)
        await replacement.async_start()
        assert set(replacement.entity_ids) == {
            "sensor.pv_energy",
            "sensor.pv_replacement",
        }
        assert replacement.preview("source-1")["last_valid_value"] is None
        await replacement.async_stop()


async def test_retention_bounds_saved_readings_and_deltas(hass):
    """Die Mengengrenze entfernt auch zugehörige Deltas und alte Abdeckung."""

    with (
        freeze_time(START, real_asyncio=True) as clock,
        patch("custom_components.pv_forecast.measurement_runtime.MAX_READINGS", 2),
    ):
        await _report(hass, clock, 0, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        await manager.async_start()
        await _report(hass, clock, 1, minutes=30)
        await _report(hass, clock, 2, minutes=60)
        await manager.async_stop()
        stored = await manager._store.async_load()
        assert len(stored["sources"]["source-1"]["readings"]) == 2
        assert len(stored["sources"]["source-1"]["deltas"]) == 1
        result = manager.snapshot(
            START, START + timedelta(minutes=60), START + timedelta(minutes=60)
        )
        assert result["sources"][0]["complete"] is False
        assert result["sources"][0]["energy_kwh"] == 1


async def test_removing_last_source_clears_reports_received_before_reload(hass):
    """Zwischen bestätigtem Löschen und Reload gemeldete Werte bleiben nicht zurück."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        entry = _entry(hass, _source())
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        await manager.async_delete_source_data("source-1")
        await _report(hass, clock, 2, minutes=30)
        await manager.async_stop()
        assert (await manager._store.async_load())["sources"]
        options = dict(entry.options)
        options["measurement_sources"] = []
        hass.config_entries.async_update_entry(entry, options=options)
        replacement = MeasurementManager(hass, entry)
        await replacement.async_start()
        assert await Store(hass, 1, manager._store.key).async_load() is None
        assert not replacement.running
        assert replacement._listeners == []


async def test_frequent_reports_do_not_postpone_pending_save(hass):
    """Häufige lokale Meldungen verschieben die bereits geplante Speicherung nicht."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        try:
            with patch.object(manager._store, "async_delay_save") as delayed_save:
                await manager.async_start()
                await _report(hass, clock, 1.1, minutes=1)
                await _report(hass, clock, 1.2, minutes=2)
                delayed_save.assert_called_once()
                serializer = delayed_save.call_args.args[0]
                data = serializer()
                assert data["sources"]["source-1"]["readings"][-1]["value"] == 1.2
                await _report(hass, clock, 1.3, minutes=3)
                assert delayed_save.call_count == 2
        finally:
            await manager.async_stop()


async def test_continuous_reports_reach_store_within_sixty_seconds(hass, hass_storage):
    """Laufende Berichte werden auch ohne Ruhephase tatsächlich gespeichert."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        manager = MeasurementManager(hass, _entry(hass, _source()))
        try:
            await manager.async_start()
            for second in (20, 40):
                clock.move_to(START + timedelta(seconds=second))
                hass.states.async_set(
                    "sensor.pv_energy",
                    1,
                    {
                        ATTR_UNIT_OF_MEASUREMENT: "kWh",
                        "device_class": "energy",
                        "state_class": "total_increasing",
                    },
                )
                await hass.async_block_till_done()
            assert manager._store.key not in hass_storage
            clock.move_to(START + timedelta(seconds=60))
            async_fire_time_changed(hass, START + timedelta(seconds=60))
            await hass.async_block_till_done()
            saved = hass_storage[manager._store.key]["data"]
            assert len(saved["sources"]["source-1"]["readings"]) == 3
        finally:
            await manager.async_stop()


async def test_missing_unregistered_historical_source_is_unresolved(hass):
    """Eine entfernte unregistrierte Quelle hat keine weiter prüfbare Identität."""

    with freeze_time(START, real_asyncio=True) as clock:
        await _report(hass, clock, 1, minutes=0)
        entry = _entry(hass, _source())
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        await _report(hass, clock, 1.5, minutes=30)
        await manager.async_stop()
        hass.states.async_remove("sensor.pv_energy")
        await _report(hass, clock, 2, minutes=60, entity_id="sensor.pv_replacement")
        options = dict(entry.options)
        options["measurement_sources"] = [_source(entity_id="sensor.pv_replacement")]
        hass.config_entries.async_update_entry(entry, options=options)
        replacement = MeasurementManager(hass, entry)
        try:
            await replacement.async_start()
            assert set(replacement.entity_ids) == {
                "sensor.pv_energy",
                "sensor.pv_replacement",
            }
            assert replacement.identity_unresolved == ("source-1",)
            result = replacement.snapshot(
                START, START + timedelta(minutes=30), START + timedelta(minutes=60)
            )
            assert result["sources"][0]["identity_unresolved"] is True
        finally:
            await replacement.async_stop()


async def test_registered_source_without_state_keeps_confirmed_identity(
    hass, entity_registry
):
    """Die Registry-Identität bleibt bei vorübergehend fehlendem Zustand prüfbar."""

    registered = entity_registry.async_get_or_create(
        "sensor", "test", "pv", suggested_object_id="pv_energy"
    )
    manager = MeasurementManager(hass, _entry(hass, _source(registry_id=registered.id)))
    try:
        await manager.async_start()
        assert hass.states.get(registered.entity_id) is None
        assert manager.identity_unresolved == ()
    finally:
        await manager.async_stop()
