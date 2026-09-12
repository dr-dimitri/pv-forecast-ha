"""Issue #172: durchgehender Morgenpfad bis zur wirksamen AC-Zeitreihe."""

import asyncio
import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import callback

from custom_components.pv_forecast.history import (
    ArchiveRecord,
    Assessment,
    SourceAssessment,
)
from custom_components.pv_forecast.history_runtime import (
    ArchiveManager,
    _configuration_id,
    async_delete_history_data,
    async_delete_history_source_data,
)
from custom_components.pv_forecast.morning_runtime import MorningManager

from .test_history_runtime import _Coordinator, _entry, _source
from .test_morning import DAWN, case


class Coordinator(_Coordinator):
    def __init__(self):
        super().__init__()
        self.calibration_factor = 1.0
        self.calibration_candidate_id = None
        self.morning_coefficient = 0.0
        self.morning_candidate_id = None
        self.origin = "live"
        self.raw_data = self.data

    @callback
    def async_set_morning(self, coefficient, candidate_id):
        self.morning_coefficient, self.morning_candidate_id = coefficient, candidate_id


def managers(hass, *, mode="auto"):
    entry = _entry(hass, source=_source())
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "morning_mode": mode}
    )
    coordinator = Coordinator()
    measurements = SimpleNamespace(
        running=True, identity_unresolved=(), async_snapshot=AsyncMock()
    )
    history = ArchiveManager(hass, entry, coordinator, measurements)
    history._loaded = history._running = True
    manager = MorningManager(hass, entry, coordinator, history)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, history=history, morning=manager
    )
    return entry, coordinator, history, manager


def record_for(entry, source, item):
    return ArchiveRecord(
        record_id=item.record_id,
        start=item.basis.intervals[0].start,
        end=item.basis.intervals[-1].end,
        target_date=item.target_date,
        horizon="daily_previous_18",
        cutoff=item.cutoff,
        fetched_at=item.cutoff,
        observed_at=item.cutoff,
        timezone="UTC",
        configuration_id=_configuration_id(entry),
        measurement_sources=(source,),
        raw_energy_kwh=item.day_actual_kwh,
        quality_flags=(),
        basis=item.basis,
    )


def source_evidence(item, source):
    return {
        "sources": [
            {
                "kind": "total",
                "source_id": source.source_id,
                "identity_unresolved": False,
                "segments": {"one": source.to_dict()},
                "deltas": [
                    {
                        "start": i.start.isoformat(),
                        "end": i.end.isoformat(),
                        "energy_kwh": i.energy_kwh,
                        "quality_flags": [],
                        "segment_id": "one",
                    }
                    for i in item.measured
                ],
            }
        ]
    }


@pytest.fixture(autouse=True)
def fixed_clock(freezer, monkeypatch):
    freezer.move_to(DAWN - timedelta(days=2))
    monkeypatch.setattr(
        "custom_components.pv_forecast.morning_runtime.morning_start",
        lambda day, *_: DAWN.replace(year=day.year, month=day.month, day=day.day),
    )


async def fill_days(entry, coordinator, history, manager, freezer, *, count=46):
    source = _source()

    async def evidence(start, end, now):
        offset = (start.date() - DAWN.date()).days
        return source_evidence(case(offset), source)

    history.measurements.async_snapshot.side_effect = evidence
    for offset in range(count):
        item = case(offset)
        record = record_for(entry, source, item)
        freezer.move_to(item.cutoff)
        coordinator.last_update_success_time = item.cutoff
        await manager._async_assess()
        history._archive.records[item.record_id] = record
        await manager._async_reconcile(item.cutoff)
        manager._capture(item.cutoff)
        history._archive.records[item.record_id] = replace(
            record,
            assessment=Assessment(
                record.end,
                item.day_actual_kwh,
                True,
                (),
                (
                    SourceAssessment(
                        source.source_id, item.day_actual_kwh, ("one",), ()
                    ),
                ),
            ),
        )
    now = record.end + timedelta(minutes=1)
    freezer.move_to(now)
    coordinator.last_update_success_time = now
    await manager._async_assess()
    return now


@pytest.mark.parametrize("mode", ["observe", "auto"])
async def test_full_prospective_pipeline_captures_measurements_and_releases_only_auto(
    hass, freezer, mode
):
    entry, coordinator, history, manager = managers(hass, mode=mode)
    # Der Quellenvertrag ist hier absichtlich ein echtes lokales Testsensor-Entity.
    hass.states.async_set("sensor.pv_energy", 0, {"unit_of_measurement": "kWh"})
    await manager.async_start()
    try:
        if manager._task:
            await manager._task
        assert manager.prerequisites_met
        await fill_days(entry, coordinator, history, manager, freezer)
        report = manager.snapshot()
        assert report["status"] == "approved", report
        assert report["training_days"] == 30 and report["validation_days"] == 14
        assert (
            report["candidate"]["time_mae_minutes"]
            < report["baseline"]["time_mae_minutes"]
        )
        assert (coordinator.morning_coefficient > 0) == (mode == "auto")
        assert "baseline" not in manager.snapshot(public=True)
        assert manager.snapshot(public=True)["uncertainty"]["status"] == "unavailable"
        assert len(manager._records) <= 90
    finally:
        await manager.async_stop()
    stored = await manager._store.async_load()
    assert stored["learning"]["candidate"] is not None
    assert stored["records"]
    assert not coordinator.listeners


async def test_malformed_restored_store_is_not_overwritten_or_applied(hass):
    _, coordinator, _, manager = managers(hass)
    await manager._store.async_save({"context": "bad"})
    await manager.async_start()
    assert manager.snapshot()["status"] == "storage_unavailable"
    assert coordinator.morning_coefficient == 0
    assert await manager._store.async_load() == {"context": "bad"}


async def test_off_does_not_capture_historic_or_current_candidates(hass):
    _, coordinator, _, manager = managers(hass, mode="off")
    await manager.async_start()
    try:
        assert manager.snapshot()["status"] == "off"
        assert manager._records == {} and coordinator.morning_coefficient == 0
    finally:
        await manager.async_stop()


async def test_already_passed_cutoff_is_never_reconstructed(hass, freezer):
    entry, coordinator, history, manager = managers(hass)
    item = case()
    history._archive.records[item.record_id] = record_for(entry, _source(), item)
    freezer.move_to(item.cutoff + timedelta(seconds=1))
    manager._capture(item.cutoff + timedelta(seconds=1))
    assert manager._records == {}
    coordinator.origin = "restored"
    manager._capture(item.cutoff)
    assert manager._records == {}


async def test_actual_bad_time_evidence_revokes_an_old_saved_trace(hass, freezer):
    entry, coordinator, history, manager = managers(hass)
    hass.states.async_set("sensor.pv_energy", 0)
    await manager.async_start()
    try:
        if manager._task:
            await manager._task
        now = await fill_days(entry, coordinator, history, manager, freezer, count=1)
        assert manager._records["0"]["measured"]
        evidence = source_evidence(case(), _source())
        evidence["sources"][0]["deltas"][10]["quality_flags"] = ["gap"]
        history.measurements.async_snapshot.side_effect = None
        history.measurements.async_snapshot.return_value = evidence
        freezer.move_to(now + timedelta(minutes=5))
        await manager._async_assess()
        assert manager._records["0"]["measured"] == []
    finally:
        await manager.async_stop()


async def test_context_change_revokes_profile_and_preserves_global_calibration(
    hass, freezer
):
    entry, coordinator, history, manager = managers(hass)
    hass.states.async_set("sensor.pv_energy", 0)
    await manager.async_start()
    try:
        if manager._task:
            await manager._task
        await fill_days(entry, coordinator, history, manager, freezer)
        assert coordinator.morning_coefficient > 0
        coordinator.calibration_factor = 1.2
        manager._updated()
        assert coordinator.morning_coefficient == 0
        assert coordinator.calibration_factor == 1.2
        assert manager._learning.candidate is None
    finally:
        await manager.async_stop()


async def test_day_correction_invalidates_specific_training_evidence(hass, freezer):
    entry, coordinator, history, manager = managers(hass)
    hass.states.async_set("sensor.pv_energy", 0)
    await manager.async_start()
    try:
        if manager._task:
            await manager._task
        now = await fill_days(entry, coordinator, history, manager, freezer)
        record = history._archive.records["0"]
        history._archive.records["0"] = replace(
            record,
            assessment=replace(
                record.assessment,
                actual_energy_kwh=record.assessment.actual_energy_kwh + 1,
            ),
        )
        await manager._async_reconcile(now)
        assert coordinator.morning_coefficient == 0
        assert manager._learning.report["status"] == "evidence_changed"
    finally:
        await manager.async_stop()


async def test_source_and_archive_deletion_remove_morning_proof(hass):
    entry, coordinator, history, manager = managers(hass)
    manager.async_reset = AsyncMock()
    history.async_delete_data = AsyncMock()
    history.async_delete_measurement_source = AsyncMock()
    await async_delete_history_data(hass, entry)
    assert manager.async_reset.await_count == 1
    await async_delete_history_source_data(hass, entry, _source().source_id)
    assert manager.async_reset.await_count == 2
    assert coordinator.morning_coefficient == 0


async def test_real_coordinator_rebuilds_same_shared_series_without_weather_call(
    hass, monkeypatch
):
    from custom_components.pv_forecast.coordinator import PvForecastCoordinator

    from .test_history import forecast

    entry = _entry(hass, source=_source())
    client = SimpleNamespace(async_fetch_roofs=AsyncMock())
    coordinator = PvForecastCoordinator(hass, entry, client)
    raw = forecast(DAWN.date(), dc_power=1)
    coordinator.data = coordinator.raw_data = raw
    coordinator.last_update_success_time = DAWN
    coordinator.last_update_success = False
    coordinator.origin = "restored"
    monkeypatch.setattr(
        "custom_components.pv_forecast.morning.morning_start",
        lambda day, *_: DAWN.replace(year=day.year, month=day.month, day=day.day),
    )
    coordinator.async_set_morning(0.5, "approved-id")
    assert coordinator.data != raw
    assert coordinator.raw_data is raw
    assert coordinator.last_update_success_time == DAWN
    assert coordinator.last_update_success is False and coordinator.origin == "restored"
    client.async_fetch_roofs.assert_not_called()
    coordinator.async_set_morning(0, None)
    assert coordinator.data is raw


async def test_reset_callback_cannot_reapply_a_previously_approved_profile(
    hass, freezer
):
    entry, coordinator, history, manager = managers(hass)
    hass.states.async_set("sensor.pv_energy", 0)
    await manager.async_start()
    try:
        if manager._task:
            await manager._task
        await fill_days(entry, coordinator, history, manager, freezer)
        original = coordinator.async_set_morning

        @callback
        def notify(coefficient, candidate_id):
            changed = coefficient != coordinator.morning_coefficient
            original(coefficient, candidate_id)
            if changed:
                manager._updated()

        coordinator.async_set_morning = notify
        await manager.async_reset()
        assert coordinator.morning_coefficient == 0
        assert manager._learning.candidate is None
        assert not manager._records
    finally:
        await manager.async_stop()


async def test_restart_preserves_context_after_global_calibration_starts(hass, freezer):
    entry, coordinator, history, manager = managers(hass)
    hass.states.async_set("sensor.pv_energy", 0)
    coordinator.calibration_factor = 1.2
    began = manager._learning.began
    await manager.async_start()
    await manager.async_stop()
    freezer.move_to(DAWN)
    coordinator.calibration_factor = 1
    restored = MorningManager(hass, entry, coordinator, history)
    coordinator.calibration_factor = 1.2
    await restored.async_start()
    try:
        assert restored._learning.began == began
    finally:
        await restored.async_stop()


def test_native_store_limit_includes_ha_wrapper_and_indentation(hass):
    from homeassistant.util.file import WriteError

    from custom_components.pv_forecast.morning_runtime import MAX_BYTES, _store

    with pytest.raises(WriteError):
        _store(hass, "bounded")._write_prepared_data("w", "x" * (MAX_BYTES + 1))


@pytest.mark.parametrize("change", ["configuration", "source", "measurement"])
async def test_executor_discards_stale_evidence_and_retries_without_blocking_ha(
    hass, monkeypatch, change
):
    from custom_components.pv_forecast.morning import MorningLearning

    _, coordinator, history, manager = managers(hass)
    hass.states.async_set("sensor.pv_energy", 0)
    await manager.async_start()
    if manager._task:
        await manager._task
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    eventloop_thread = threading.get_ident()
    workers = []
    original = MorningLearning.update
    frozen_learning = manager._learning

    def blocked_update(learning, cases, now, today):
        workers.append(threading.get_ident())
        assert workers[-1] != eventloop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        original(learning, cases, now, today)

    monkeypatch.setattr(MorningLearning, "update", blocked_update)
    try:
        manager._updated(force=True)
        first_task = manager._task
        await asyncio.wait_for(entered.wait(), 5)
        # Diese HA-Änderung erreicht uns, während der CPU-Worker angehalten ist.
        if change == "configuration":
            coordinator.calibration_factor = 1.2
        elif change == "source":
            history.measurements.identity_unresolved = ("source_changed",)
        manager._updated(force=True)
        replacement = manager._learning
        if change == "configuration":
            assert replacement is not frozen_learning
        release.set()
        await first_task
        assert coordinator.morning_coefficient == 0
        if change == "source":
            assert manager._learning is replacement
            assert manager.snapshot()["status"] == "prerequisites_missing"
        else:
            # Auch eine reine Messrevision löst einen zweiten Snapshot aus.
            if manager._task:
                await manager._task
            assert len(workers) >= 2
            assert manager.snapshot()["status"] == "insufficient_training"
    finally:
        release.set()
        await manager.async_stop()
