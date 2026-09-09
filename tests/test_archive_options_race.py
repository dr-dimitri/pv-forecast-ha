"""Abschluss eines laufenden Alt-Abrufs vor dem asynchronen Options-Reload."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.pv_forecast.history_runtime import (
    ArchiveManager,
    _configuration_id,
)

from .test_history_runtime import NOW, _Coordinator, _entry, _forecast, _source


@pytest.mark.parametrize("change", ["power", "limit", "horizon", "source"])
async def test_old_forecast_is_not_captured_under_changed_options(
    hass, freezer, change
):
    freezer.move_to(NOW)
    entry = _entry(hass)
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, entry, coordinator, None)
    await manager.async_start()
    original_id = _configuration_id(entry)
    try:
        original_forecast = coordinator.data
        original_records = set(manager._archive.records)
        options = deepcopy(dict(entry.options))
        if change == "power":
            options["roofs"][0]["installed_power_kwp"] = 20
        elif change == "limit":
            options["inverter_max_power_kw"] = 3
        elif change == "horizon":
            options["horizon_profiles"] = {"roof_1": [30] * 12}
        else:
            options["measurement_sources"] = [_source().to_dict()]
        hass.config_entries.async_update_entry(entry, options=options)
        # Ein vor der Optionsänderung gestarteter Abruf kommt noch vor dem
        # Entladen zurück: neue Abrufzeit, aber weiterhin die alten Erträge.
        freezer.move_to(NOW + timedelta(minutes=30))
        coordinator.last_update_success_time = NOW + timedelta(minutes=30)
        with patch.object(
            manager._archive, "capture", wraps=manager._archive.capture
        ) as capture:
            coordinator.update()
            assert coordinator.data is original_forecast
            assert _configuration_id(entry) != original_id
            capture.assert_not_called()
        assert set(manager._archive.records) == original_records
        assert all(
            record.configuration_id == original_id
            for record in manager._archive.records.values()
        )
    finally:
        await manager.async_stop()

    # Nach dem Reload wird die neue Konfiguration mit neu berechneten Daten
    # erfasst. Der gespeicherte Altbestand bleibt seiner ursprünglichen Basis treu.
    fresh = _Coordinator()
    fresh.data = _forecast(energy=2)
    fresh.last_update_success_time = NOW + timedelta(minutes=30)
    restarted = ArchiveManager(hass, entry, fresh, None)
    try:
        await restarted.async_start()
        records = tuple(restarted._archive.records.values())
        assert any(record.configuration_id == original_id for record in records)
        assert any(
            record.configuration_id == _configuration_id(entry)
            and record.raw_energy_kwh == 2
            for record in records
            if record.horizon == "hourly_1h"
        )
    finally:
        await restarted.async_stop()
    assert coordinator.listeners == fresh.listeners == []


async def test_renaming_does_not_interrupt_archival_capture(hass, freezer):
    freezer.move_to(NOW)
    entry = _entry(hass)
    coordinator = _Coordinator()
    manager = ArchiveManager(hass, entry, coordinator, None)
    await manager.async_start()
    try:
        original_id = _configuration_id(entry)
        options = deepcopy(dict(entry.options))
        options["roofs"][0]["name"] = "Neuer Dachname"
        hass.config_entries.async_update_entry(
            entry, options=options, title="Neuer Anlagenname"
        )
        freezer.move_to(NOW + timedelta(minutes=30))
        coordinator.last_update_success_time = NOW + timedelta(minutes=30)
        with patch.object(
            manager._archive, "capture", wraps=manager._archive.capture
        ) as capture:
            coordinator.update()
            capture.assert_called_once()
            assert capture.call_args.args[3] == original_id
    finally:
        await manager.async_stop()
