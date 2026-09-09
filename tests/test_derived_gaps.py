"""Integral-Lücken dürfen keine beobachteten Energie- oder Lernbelege werden."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from custom_components.pv_forecast.history import (
    Assessment,
    HistoryArchive,
    SourceAssessment,
)
from custom_components.pv_forecast.history_runtime import ArchiveManager
from custom_components.pv_forecast.measurement_helpers import (
    async_resolve_measurement_helpers,
)
from custom_components.pv_forecast.measurement_runtime import MeasurementManager
from custom_components.pv_forecast.measurements import SourceConfig, SourceHistory
from custom_components.pv_forecast.uncertainty import _assessment_at

from .test_history import DAY, SOURCE, capture_day, forecast
from .test_history_runtime import _Coordinator
from .test_history_runtime import _entry as archive_entry
from .test_measurement_adapters import POWER, draft, ksem
from .test_measurement_flow import _entry


@pytest.mark.parametrize("derived", [True, False])
@pytest.mark.parametrize("gap", ["restart", "upstream_gap", "unknown"])
def test_only_real_counter_can_prove_energy_across_gap(derived, gap):
    """Nur echte Zähler belegen Energie auch über eine unbeobachtete Pause."""
    source = replace(SOURCE, derived_energy=derived)
    history = SourceHistory(source, "UTC", 20)
    start = datetime(2026, 9, 9, 12, tzinfo=UTC)
    history.add_reading(start, 0, "kWh")
    history.mark_gap(gap)
    history.add_reading(start + timedelta(minutes=10), 0.3, "kWh")
    history.add_reading(start + timedelta(minutes=20), 0.5, "kWh")
    for restored in (
        history,
        SourceHistory.from_dict(source, history.to_dict(), "UTC", 20),
    ):
        result = restored.snapshot(
            start, start + timedelta(minutes=20), start + timedelta(minutes=20)
        )
        assert result["energy_kwh"] == pytest.approx(0.2 if derived else 0.5)
        assert result["energy_complete"] is not derived
        assert result["complete"] is False
        assert ("derived_measurement_gap" in result["quality_flags"]) is derived


@pytest.mark.parametrize(
    "minutes,horizon", [(60, "hourly_1h"), (1440, "daily_previous_18")]
)
async def test_native_helper_gap_is_excluded_from_measurement_and_archive(
    hass, minutes, horizon
):
    """Die vom nativen Helfer interpolierte Pause bleibt unbeobachtet."""
    start = datetime(2026, 9, 9, tzinfo=UTC)
    end = start + timedelta(minutes=minutes)
    with freeze_time(start - timedelta(minutes=2), real_asyncio=True) as clock:
        _, sensor = ksem(hass)
        sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
        entry = _entry(hass, sources)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "time_zone": "UTC"}
        )
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        try:
            for minute in (-1, 0, 1, 2, *range(20, minutes + 1)):
                clock.move_to(start + timedelta(minutes=minute))
                hass.states.async_set(sensor.entity_id, 1000, POWER)
                await hass.async_block_till_done()
            snapshot = manager.snapshot(start, end, end)
            assert snapshot["total_energy"]["energy_kwh"] == pytest.approx(
                minutes / 60 - 0.3, abs=0.001
            )
            assert snapshot["total_energy"]["energy_complete"] is False
            windows = await manager.async_interval_windows([(start, end)], end)
            assert windows[0]["energy_kwh"] is None
            outlook = manager.day_outlook(
                forecast(), end - timedelta(minutes=1), end - timedelta(minutes=1), True
            )
            assert outlook["status"] == "unavailable"
            assert outlook["reason"] == "incomplete_measurements"
            archive = HistoryArchive("UTC")
            observed = start - timedelta(hours=1 if minutes == 60 else 6)
            source = SourceConfig.from_dict(sources[0])
            archive.capture(
                forecast(DAY - timedelta(days=1)), observed, observed, "a", [source]
            )
            record = next(
                record
                for record in archive.records.values()
                if record.start == start and record.horizon == horizon
            )
            archive.assess(record.record_id, snapshot, end)
            assessed = archive.records[record.record_id].assessment
            assert not assessed.valid
            assert assessed.actual_energy_kwh is None
            assert "derived_measurement_gap" in assessed.reasons
        finally:
            await manager.async_stop()


def old_gap_archive():
    """Einen vor der Korrektur gespeicherten, fälschlich gültigen Beleg nachbilden."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    old = Assessment(
        record.end,
        1,
        True,
        (),
        (
            SourceAssessment(
                SOURCE.source_id,
                1,
                ("old-segment",),
                ("derived_energy", "gap", "upstream_gap"),
            ),
        ),
    )
    record = replace(
        record,
        measurement_sources=(replace(SOURCE, derived_energy=True),),
        assessment=old,
    )
    archive.records[record.record_id] = record
    return HistoryArchive.from_dict(archive.to_dict(), "UTC"), record


def test_stored_bad_assessment_is_revised_without_raw_readings():
    """Abgelaufene Rohpunkte schützen keine nachweislich unbeobachtete Integralmenge."""
    archive, record = old_gap_archive()
    now = record.end + timedelta(days=8)
    assert archive.invalidate_derived_gaps(now)
    corrected = archive.records[record.record_id]
    assert corrected.raw_energy_kwh == record.raw_energy_kwh
    assert corrected.assessment_revisions == (record.assessment,)
    assert not corrected.assessment.valid
    assert corrected.assessment.actual_energy_kwh is None
    assert not archive.invalidate_derived_gaps(now + timedelta(minutes=1))
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert restored.records[record.record_id] == corrected
    # Auch frühere Prüfungen dürfen keine unbelegte Revision verwenden.
    assert _assessment_at(corrected, record.end + timedelta(days=1)) is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_archive_start_corrects_bad_assessment_before_learning(
    hass, freezer, enabled
):
    """Laden korrigiert vor der Auswertung; reguläres Speichern bewahrt die Revision."""
    archive, record = old_gap_archive()
    now = record.end + timedelta(days=8)
    freezer.move_to(now)
    manager = ArchiveManager(
        hass, archive_entry(hass, enabled=enabled), _Coordinator(), None
    )
    await manager._store.async_save({"archive": archive.to_dict()})
    with patch.object(manager._store, "async_delay_save") as schedule:
        await manager.async_start()
        assert not manager._archive.records[record.record_id].assessment.valid
        if enabled:
            schedule.assert_called_once()
        await manager.async_stop()
    saved = await manager._store.async_load()
    restored = HistoryArchive.from_dict(saved["archive"], "UTC")
    assert not restored.records[record.record_id].assessment.valid
