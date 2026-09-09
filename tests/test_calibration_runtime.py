"""Praktische Lifecycle-Prüfungen des lokalen Anlagenlernens ohne Wetterabruf."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.calibration import CalibrationState
from custom_components.pv_forecast.calibration_runtime import (
    CalibrationManager,
    async_delete_calibration_data,
)
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.history_runtime import (
    ArchiveManager,
    _configuration_id,
    async_delete_history_data,
    async_delete_history_source_data,
)

from .helpers import roof, weather
from .test_history_runtime import NOW, _Coordinator, _entry, _source


class _LocalCoordinator(_Coordinator):
    def __init__(self):
        super().__init__()
        self.raw_data = self.data
        self.calibration_factor = 1.0
        self.calibration_candidate_id = None

    @callback
    def async_set_calibration(self, factor, candidate_id):
        if (factor, candidate_id) == (
            self.calibration_factor,
            self.calibration_candidate_id,
        ):
            return
        self.calibration_factor = factor
        self.calibration_candidate_id = candidate_id
        self.update()


def _managers(hass, mode="observe"):
    entry = _entry(hass, source=_source())
    if mode is not None:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "calibration_mode": mode}
        )
    coordinator = _LocalCoordinator()
    history = ArchiveManager(hass, entry, coordinator, None)
    manager = CalibrationManager(hass, entry, coordinator, history)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, history=history, calibration=manager
    )
    return entry, coordinator, history, manager


@pytest.fixture(autouse=True)
def fixed_clock(freezer):
    freezer.move_to(NOW)


async def test_restart_preserves_learning_segment(hass, freezer):
    """Ein HA-Neustart darf die bereits gesammelten Lerntage nicht zurücksetzen."""
    entry, coordinator, history, manager = _managers(hass)
    began = NOW - timedelta(days=12)
    state = CalibrationState(_configuration_id(entry), began, "UTC")
    await manager._store.async_save({"state": state.to_dict()})
    await history.async_start()
    try:
        await manager.async_start()
        assert manager._state.segment_start == began
        assert manager.snapshot()["effective_factor"] == 1.0
    finally:
        await manager.async_stop()
        await history.async_stop()
    stored = await manager._store.async_load()
    assert stored["state"]["segment_start"] == began.isoformat()
    assert coordinator.listeners == []


async def test_location_change_revokes_old_learning_segment_with_paused_archive(hass):
    """Eine neue Lage übernimmt auch ohne aktive Lernvoraussetzungen keinen Altstand."""

    entry, coordinator, history, manager = _managers(hass, mode="off")
    state = CalibrationState(_configuration_id(entry), NOW - timedelta(days=12), "UTC")
    await manager._store.async_save({"state": state.to_dict()})
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "latitude": 35.6},
        options={**entry.options, "history_enabled": False},
    )
    await history.async_start()
    try:
        await manager.async_start()
        assert manager._state.configuration_id == _configuration_id(entry)
        assert manager._state.segment_start == NOW
        assert manager._state.approved_factor == 1
        assert coordinator.calibration_factor == 1
    finally:
        await manager.async_stop()
        await history.async_stop()


async def test_default_off_does_not_load_or_write_learning_store(hass):
    """Bestandsanlagen erhalten ohne Opt-in keinen neuen Lernlistener oder Store."""
    _, coordinator, _history, manager = _managers(hass, mode=None)
    with patch.object(manager._store, "async_load") as load:
        await manager.async_start()
        await manager.async_stop()
    load.assert_not_called()
    assert coordinator.listeners == []
    assert manager.snapshot()["status"] == "off"


async def test_unknown_learning_store_keeps_raw_and_is_not_overwritten(hass):
    """Unbekannte Daten bleiben erhalten, während die Prognose unverändert läuft."""
    _, coordinator, history, manager = _managers(hass, mode="auto")
    await history.async_start()
    try:
        with (
            patch.object(manager._store, "async_load", side_effect=NotImplementedError),
            patch.object(manager._store, "async_save") as save,
            patch.object(manager._store, "async_delay_save") as delayed,
        ):
            await manager.async_start()
            await manager.async_stop()
        save.assert_not_called()
        delayed.assert_not_called()
        assert manager.snapshot()["status"] == "storage_unavailable"
        assert coordinator.calibration_factor == 1.0
    finally:
        await history.async_stop()


async def test_reset_starts_now_and_unload_does_not_restart(hass, freezer):
    """Reset und Entladen dürfen keinen alten Faktor oder neue Listener zurücklassen."""
    entry, coordinator, history, manager = _managers(hass)
    await history.async_start()
    await manager.async_start()
    freezer.move_to(NOW + timedelta(days=1))
    await async_delete_calibration_data(hass, entry)
    assert manager._state.segment_start == NOW + timedelta(days=1)
    assert manager.capture_parameters()["trial_candidate_id"] is None
    await manager.async_stop()
    await history.async_stop()
    await async_delete_calibration_data(hass, entry)
    assert coordinator.listeners == []
    assert manager._running is False
    assert await manager._store.async_load() is None


@pytest.mark.parametrize("delete_source", [False, True])
async def test_deleting_archive_evidence_discards_learning_references(
    hass, freezer, delete_source
):
    """Auch gezielte Quellenlöschung verwirft die davon abhängige Freigabe."""
    entry, coordinator, history, manager = _managers(hass)
    await history.async_start()
    await manager.async_start()
    try:
        freezer.move_to(NOW + timedelta(hours=1))
        if delete_source:
            await async_delete_history_source_data(hass, entry, "source-1")
        else:
            await async_delete_history_data(hass, entry)
        assert manager._state.segment_start == NOW + timedelta(hours=1)
        assert manager._state.candidate is None
        assert coordinator.calibration_factor == 1.0
    finally:
        await manager.async_stop()
        await history.async_stop()


async def test_old_archive_store_migrates_without_inventing_learning_basis(hass):
    """Version 1 bleibt nach der tatsächlichen Schemaerweiterung vollständig lesbar."""
    entry, _, history, _ = _managers(hass)
    history._archive.capture(
        _LocalCoordinator().data, NOW, NOW, _configuration_id(entry), (_source(),)
    )
    original = history._archive.to_dict()
    for record in original["records"]:
        for key in (
            "basis",
            "applied_factor",
            "applied_candidate_id",
            "candidate_factor",
            "candidate_id",
            "candidate_energy_kwh",
        ):
            record.pop(key, None)
    stored = {"archive": original, "last_fetched_at": NOW.isoformat()}
    await Store(hass, 1, f"pv_forecast.history.{entry.entry_id}").async_save(stored)
    await history.async_start()
    try:
        assert len(history._archive.records) == len(original["records"])
        assert all(record.basis is None for record in history._archive.records.values())
    finally:
        await history.async_stop()
    restored = await history._store.async_load()
    assert restored["archive"] == original


async def test_local_factor_changes_preserve_weather_age_and_error(hass):
    """Freigabe und Ausschalten ändern alle Erträge lokal ohne Abruf-/Fehlerreset."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "inverter_max_power_kw": 5.0}
    )
    client = AsyncMock()
    coordinator = PvForecastCoordinator(hass, entry, client)
    raw = calculate_forecast(
        (roof("a"),), {"a": (weather(),)}, 5.0, weather().start.date(), ZoneInfo("UTC")
    )
    coordinator.raw_data = raw
    coordinator.data = raw
    coordinator.last_update_success = False
    coordinator.last_update_success_time = datetime(2026, 8, 23, tzinfo=UTC)
    fetched_at = coordinator.last_update_success_time
    with patch.object(coordinator, "async_update_listeners") as updated:
        coordinator.async_set_calibration(0.8, "candidate-1")
        assert coordinator.raw_data is raw
        assert coordinator.data.total.today <= raw.total.today
        assert coordinator.calibration_factor == 0.8
        coordinator.async_set_calibration(1.0, None)
        assert coordinator.data is raw
        assert updated.call_count == 2
    assert coordinator.last_update_success is False
    assert coordinator.last_update_success_time == fetched_at
    client.async_fetch_roofs.assert_not_called()
    await coordinator.async_shutdown()
