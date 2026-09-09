"""Durchgehender lokaler Lernlauf mit echten Archivständen und Neustarts."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

from homeassistant.core import callback

from custom_components.pv_forecast.calculations import (
    apply_calibration,
    calculate_forecast,
)
from custom_components.pv_forecast.calibration_runtime import CalibrationManager
from custom_components.pv_forecast.history import Assessment, SourceAssessment
from custom_components.pv_forecast.history_runtime import (
    ArchiveManager,
    _configuration_id,
)
from custom_components.pv_forecast.models import WeatherInterval

from .helpers import roof
from .test_calibration_runtime import _LocalCoordinator
from .test_history_runtime import _entry, _source

START = datetime(2026, 9, 8, 17, tzinfo=UTC)


def _raw_forecast(day: date):
    """Zwei vollständige Tage mit je acht Sonnenstunden ergeben roh 10 kWh."""

    start = datetime.combine(day, time.min, UTC)
    intervals = tuple(
        WeatherInterval(
            start + timedelta(hours=hour),
            start + timedelta(hours=hour + 1),
            125.0 if 8 <= hour % 24 < 16 else 0.0,
            25.0,
        )
        for hour in range(48)
    )
    return calculate_forecast((roof(),), {"roof_1": intervals}, None, day, UTC)


class _ForecastCoordinator(_LocalCoordinator):
    """Nur Abruf und Timer ersetzen; die gemeinsame Energie bleibt echt berechnet."""

    def __init__(self, now):
        super().__init__()
        self.raw_data = _raw_forecast(now.date())
        self.data = self.raw_data
        self.last_update_success_time = now

    @callback
    def async_set_calibration(self, factor, candidate_id):
        if (factor, candidate_id) == (
            self.calibration_factor,
            self.calibration_candidate_id,
        ):
            return
        self.calibration_factor = factor
        self.calibration_candidate_id = candidate_id
        self.data = apply_calibration(self.raw_data, factor, None, UTC)
        self.update()

    @callback
    def publish(self, now):
        self.raw_data = _raw_forecast(now.date())
        self.data = apply_calibration(self.raw_data, self.calibration_factor, None, UTC)
        self.last_update_success_time = now
        self.update()


def _assess_completed_days(history, now):
    """Nur abgeschlossene Tage mit bestätigter vollständiger AC-Messung belegen."""

    for record_id, record in tuple(history._archive.records.items()):
        if (
            record.horizon != "daily_previous_18"
            or record.end > now
            or (record.assessment and record.assessment.valid)
        ):
            continue
        assessment = Assessment(
            now,
            8.0,
            True,
            (),
            (SourceAssessment("source-1", 8.0, ("source-segment-1",), ()),),
        )
        revisions = record.assessment_revisions
        if record.assessment is not None:
            revisions = (*revisions, record.assessment)[-3:]
        history._archive.records[record_id] = replace(
            record, assessment=assessment, assessment_revisions=revisions
        )
        history._dirty = True


async def test_complete_learning_lifecycle_uses_frozen_archive_and_real_revisions(
    hass, freezer, aioclient_mock
):
    """30 Lerntage, 14 spätere Prüftage, Neustart, Modi und eine Messkorrektur."""

    freezer.move_to(START)
    entry = _entry(hass, source=_source())
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "calibration_mode": "auto"}
    )

    def managers(now):
        coordinator = _ForecastCoordinator(now)
        history = ArchiveManager(hass, entry, coordinator, None)
        manager = CalibrationManager(hass, entry, coordinator, history)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator, history=history, calibration=manager
        )
        return coordinator, history, manager

    coordinator, history, manager = managers(START)
    now = START

    async def restart(mode):
        nonlocal coordinator, history, manager
        previous_coordinator = coordinator
        await manager.async_stop()
        await history.async_stop()
        assert previous_coordinator.listeners == []
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "calibration_mode": mode}
        )
        coordinator, history, manager = managers(now)
        await history.async_start()
        await manager.async_start()
        await hass.async_block_till_done()

    try:
        await history.async_start()
        await manager.async_start()
        await hass.async_block_till_done()
        assert manager.snapshot()["effective_factor"] == 1.0
        assert manager.snapshot()["training_days"] == 0
        assert manager._state.configuration_id == _configuration_id(entry)

        learned_at = None
        for offset in range(1, 47):
            now = START + timedelta(days=offset)
            freezer.move_to(now)
            _assess_completed_days(history, now)
            # Die Runtime liest selbst die echten Archivbelege und Fingerprints.
            manager.async_reconcile()
            coordinator.publish(now)
            await hass.async_block_till_done()
            if manager._state.candidate is not None and learned_at is None:
                learned_at = manager._state.candidate.created_at
                assert manager.snapshot()["training_days"] == 30
                assert manager.snapshot()["validation_days"] == 0
                assert manager.snapshot()["status"] == "testing"
                assert coordinator.calibration_factor == 1.0
            if manager.snapshot()["validation_days"] < 14:
                assert coordinator.calibration_factor == 1.0

        status = manager.snapshot()
        assert status["status"] == "approved"
        assert status["training_days"] == 30
        assert status["validation_days"] == 14
        assert status["candidate_factor"] == 0.8
        assert status["effective_factor"] == 0.8
        assert status["raw_mae_kwh"] == 2.0
        assert status["calibrated_mae_kwh"] == 0.0
        assert coordinator.raw_data.total.today == 10.0
        assert coordinator.data.total.today == 8.0
        assert {
            item.dc_power_kw for item in coordinator.data.roofs["roof_1"].intervals
        } == {0.0, 1.25}

        candidate = manager._state.candidate
        training_ids = {item.record_id for item in candidate.training_references}
        validation_ids = {
            item.record_id for item in manager._state.validation_references
        }
        assert len(training_ids) == 30
        assert len(validation_ids) == 14
        assert training_ids.isdisjoint(validation_ids)
        for record_id in validation_ids:
            record = history._archive.records[record_id]
            assert record.cutoff > learned_at
            assert learned_at <= record.observed_at <= record.cutoff
            assert record.candidate_id == candidate.candidate_id
            assert record.raw_energy_kwh == 10.0
            assert record.candidate_energy_kwh == 8.0
            assert record.basis is not None

        await restart("auto")
        assert manager.snapshot()["status"] == "approved"
        assert manager._state.candidate.candidate_id == candidate.candidate_id
        assert manager._state.segment_start == START
        assert coordinator.calibration_factor == 0.8
        assert coordinator.data.total.today == 8.0

        await restart("observe")
        assert manager.snapshot()["status"] == "approved"
        assert manager.snapshot()["candidate_factor"] == 0.8
        assert coordinator.calibration_factor == 1.0
        assert coordinator.data is coordinator.raw_data
        assert manager.capture_parameters()["trial_factor"] == 0.8

        await restart("off")
        assert manager.snapshot()["status"] == "off"
        assert coordinator.calibration_factor == 1.0
        assert coordinator.data.total.today == 10.0
        assert manager.capture_parameters() == {}

        await restart("auto")
        assert coordinator.calibration_factor == 0.8
        corrected_id = next(iter(training_ids))
        original = history._archive.records[corrected_id]
        revised = replace(
            original.assessment,
            actual_energy_kwh=7.0,
            sources=(SourceAssessment("source-1", 7.0, ("source-segment-1",), ()),),
        )
        history._archive.records[corrected_id] = replace(
            original,
            assessment=revised,
            assessment_revisions=(
                *original.assessment_revisions,
                original.assessment,
            )[-3:],
        )
        history._dirty = True
        manager.async_reconcile()
        assert manager.snapshot()["status"] == "invalidated"
        assert manager.snapshot()["reasons"] == ["evidence_changed"]
        assert coordinator.calibration_factor == 1.0
        assert coordinator.data is coordinator.raw_data
        assert coordinator.data.total.today == 10.0
        assert manager.capture_parameters()["trial_factor"] is None
        assert aioclient_mock.call_count == 0
    finally:
        await manager.async_stop()
        await history.async_stop()
    assert coordinator.listeners == []
