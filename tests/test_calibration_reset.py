"""Regression für einen lokalen Reset kurz vor dem Archivstichtag."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.pv_forecast.calibration import CalibrationCandidate

from .test_calibration_runtime import _managers
from .test_history import forecast
from .test_history_runtime import NOW


async def test_reset_before_cutoff_removes_applied_archive_factor(hass, freezer):
    """Ein Reset vor 18 Uhr muss auch den noch offenen Archivstand zurücksetzen."""
    freezer.move_to(NOW)
    _, coordinator, history, manager = _managers(hass, mode="auto")
    coordinator.data = coordinator.raw_data = forecast(NOW.date(), dc_power=1)
    await history.async_start()
    await manager.async_start()
    await hass.async_block_till_done()
    # Die Freigabe ist Voraussetzung dieses Lifecycle-Tests. Ihre 30/14-Tage-
    # Berechnung wird getrennt geprüft und während des Resets nicht ausgeführt.
    with patch.object(manager, "async_reconcile"):
        try:
            manager._state.status = "approved"
            manager._state.candidate = CalibrationCandidate(
                "approved", 0.8, NOW - timedelta(days=20), ()
            )
            coordinator.async_set_calibration(0.8, "approved")
            target = next(
                item
                for item in history._archive.records.values()
                if item.horizon == "daily_previous_18"
            )
            assert target.calibrated_energy_kwh == pytest.approx(19.2)
            freezer.move_to(NOW + timedelta(minutes=59, seconds=30))
            await manager.async_reset()
            assert coordinator.calibration_factor == 1.0
            # Bis zum Stichtag gibt es keinen Wetterabruf. Der nächste lokale
            # Minutentakt läuft kurz nach 18 Uhr und darf nichts rückwirkend ändern.
            freezer.move_to(NOW + timedelta(hours=1, seconds=1))
            coordinator.update()
            frozen = history._archive.records[target.record_id]
            assert frozen.raw_energy_kwh == target.raw_energy_kwh
            assert frozen.fetched_at == target.fetched_at
            assert frozen.calibrated_energy_kwh is None
            assert frozen.applied_candidate_id is None
        finally:
            await manager.async_stop()
            await history.async_stop()
