"""Asynchrone Zähler melden exakte Nullgrenzen ohne erfundene Energieanteile."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.measurement_windows import async_interval_windows
from custom_components.pv_forecast.measurements import (
    SourceConfig,
    SourceHistory,
    aggregate_energy,
)
from custom_components.pv_forecast.outlook import build_day_outlook

from .test_history import forecast

DAY = date(2026, 9, 9)
CASES = [
    (DAY, "UTC", 24),
    (date(2026, 3, 29), "Europe/Berlin", 23),
    (date(2026, 10, 25), "Europe/Berlin", 25),
    (DAY, "Asia/Kolkata", 24),
    (DAY, "Asia/Kathmandu", 24),
]
MINUTE = timedelta(minutes=1)


def _bounds(day, timezone):
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(day, time.min, zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def _history(timezone="UTC", source_id="pv", *, kind="total", derived=False):
    return SourceHistory(
        SourceConfig(
            source_id,
            f"sensor.{source_id}",
            kind,
            "Bestätigte AC-PV-Erzeugung",
            derived_energy=derived,
            max_interval_minutes=2,
        ),
        timezone,
        20,
    )


def _day_history(day, timezone, *, phase=7, until=None, source_id="pv", derived=False):
    """Minütlich zwölf kWh zwischen 06 und 18 Uhr, mit beobachteten Nullnächten."""
    start, end = _bounds(day, timezone)
    zone = ZoneInfo(timezone)
    production_start = datetime.combine(day, time(6), zone).astimezone(UTC)
    production_end = datetime.combine(day, time(18), zone).astimezone(UTC)
    values = _history(timezone, source_id, derived=derived)
    cursor = start - MINUTE + timedelta(seconds=phase)
    final = until if until is not None else end + timedelta(seconds=phase)
    while cursor <= final:
        produced = 12 * min(
            1.0,
            max(0.0, (cursor - production_start) / (production_end - production_start)),
        )
        values.add_reading(cursor, 100 + produced, "kWh")
        cursor += MINUTE
    return values


def _assess_day(histories, day, timezone, now):
    archive = HistoryArchive(timezone)
    observed = datetime.combine(
        day - timedelta(days=1), time(18), ZoneInfo(timezone)
    ).astimezone(UTC)
    archive.capture(
        forecast(day - timedelta(days=1), timezone=timezone),
        observed,
        observed,
        "same-configuration",
        [history.source for history in histories],
    )
    record = next(
        record
        for record in archive.records.values()
        if record.horizon == "daily_previous_18" and record.target_date == day
    )
    evidence = {
        "start": record.start.isoformat(),
        "end": record.end.isoformat(),
        "sources": [
            history.snapshot(record.start, record.end, now) for history in histories
        ],
    }
    archive.assess(record.record_id, evidence, now)
    return archive.records[record.record_id].assessment


@pytest.mark.parametrize(("day", "timezone", "hours"), CASES)
@pytest.mark.parametrize("derived", [False, True])
async def test_shifted_polling_keeps_complete_days_across_local_time_boundaries(
    day, timezone, hours, derived
):
    """Gesunde Nullplateaus belegen Randsekunden auch an DST- und Teilstundentagen."""
    history = _day_history(day, timezone, derived=derived)
    start, end = _bounds(day, timezone)
    now = end + MINUTE
    assert end - start == timedelta(hours=hours)
    assert all(reading.timestamp not in (start, end) for reading in history.readings)
    original = history.to_dict()
    snapshot = history.snapshot(start, end, now)
    assert snapshot["energy_kwh"] == pytest.approx(12)
    assert snapshot["energy_complete"] is True
    assert snapshot["complete"] is True
    assert snapshot["coverage_seconds"] == hours * 3600
    assert "boundary_gap" not in snapshot["quality_flags"]

    windows = [(start, end), (start, start + timedelta(hours=1))]
    result = await async_interval_windows([history], windows, now)
    assert [item["energy_complete"] for item in result] == [True, True]
    assert [item["energy_kwh"] for item in result] == pytest.approx([12, 0])
    assessment = _assess_day([history], day, timezone, now)
    assert assessment.valid is True
    assert assessment.actual_energy_kwh == pytest.approx(12)
    assert history.to_dict() == original


@pytest.mark.parametrize(("day", "timezone", "_hours"), CASES)
def test_shifted_polling_allows_a_measured_day_prefix(day, timezone, _hours):
    """Die Tagesaussicht trennt trotz :07-Meldung Messung und Restprognose."""
    now = datetime.combine(day, time(12), ZoneInfo(timezone)).astimezone(UTC)
    history = _day_history(day, timezone, until=now)
    last = history.last_valid_reading
    assert last.timestamp == now - timedelta(seconds=53)
    result = build_day_outlook(
        forecast(day, timezone=timezone), [history], timezone, now, now, True
    )
    assert result["status"] == "available"
    assert result["measured_until"] == last.timestamp.isoformat()
    assert result["measured_kwh"] == pytest.approx(6 - 53 / 3600)
    assert result["bridge_kwh"] == pytest.approx(53 / 3600)
    assert result["remaining_kwh"] == pytest.approx(12)
    assert result["total_kwh"] == pytest.approx(18)


async def test_positive_differences_crossing_window_bounds_are_never_divided():
    """Asynchron gemessene positive Energie wird nicht passend auf Stunden verteilt."""
    history = _day_history(DAY, "UTC")
    start = datetime.combine(DAY, time(9), UTC)
    end = start + timedelta(hours=1)
    now = end + MINUTE
    snapshot = history.snapshot(start, end, now)
    assert snapshot["energy_complete"] is False
    assert "boundary_gap" in snapshot["quality_flags"]
    assert snapshot["energy_kwh"] < 1
    [window] = await async_interval_windows([history], [(start, end)], now)
    assert window["energy_complete"] is False
    assert window["energy_kwh"] is None


@pytest.mark.parametrize("gap", ["unknown", "restart", "stale"])
async def test_zero_difference_across_actual_gap_does_not_complete_window(gap):
    """Eine beobachtete Nullmenge darf fehlende Feinabdeckung nicht verdecken."""
    history = _history()
    start = datetime.combine(DAY, time(9), UTC)
    end = start + MINUTE
    before = start - (timedelta(minutes=3) if gap == "stale" else MINUTE / 2)
    history.add_reading(before, 100, "kWh")
    if gap == "unknown":
        history.add_reading(start - timedelta(seconds=10), "unknown", "kWh")
    elif gap == "restart":
        history.mark_gap("restart")
    history.add_reading(start + MINUTE / 2, 100, "kWh")
    history.add_reading(end + MINUTE / 2, 100, "kWh")
    now = end + MINUTE
    snapshot = history.snapshot(start, end, now)
    assert snapshot["energy_complete"] is False
    assert snapshot["complete"] is False
    [window] = await async_interval_windows([history], [(start, end)], now)
    assert window["energy_complete"] is False
    assert window["energy_kwh"] is None


@pytest.mark.parametrize("kind", ["total", "daily"])
async def test_zero_difference_from_reset_is_not_projected_over_window_boundary(kind):
    """Resetmarkierungen werden nicht wie ein unveränderter Zähler behandelt."""
    history = _history(kind=kind)
    start = datetime.combine(DAY, time(9), UTC)
    end = start + MINUTE
    history.add_reading(start - MINUTE, 10, "kWh")
    history.add_reading(start + MINUTE / 2, 0, "kWh", last_reset=start - MINUTE / 2)
    history.add_reading(end + MINUTE / 2, 0, "kWh")
    now = end + MINUTE
    assert history.snapshot(start, end, now)["energy_complete"] is False
    [window] = await async_interval_windows([history], [(start, end)], now)
    assert window["energy_complete"] is False
    assert window["energy_kwh"] is None


async def test_daily_reset_does_not_invent_the_unobserved_previous_day_closing():
    """Ein täglich zurückgesetzter Sensor ersetzt keinen endgültigen Vortagswert."""
    history = _history(kind="daily")
    start, end = _bounds(DAY, "UTC")
    for minute in range(1441):
        timestamp = start + minute * MINUTE
        value = min(12, max(0, (minute - 360) / 60)) if timestamp < end else 0
        history.add_reading(timestamp, value, "kWh")
    now = end + MINUTE
    snapshot = history.snapshot(start, end, now)
    assert snapshot["energy_kwh"] == pytest.approx(12)
    assert snapshot["energy_complete"] is False
    [window] = await async_interval_windows([history], [(start, end)], now)
    assert window["energy_kwh"] is None
    assessment = _assess_day([history], DAY, "UTC", now)
    assert assessment.valid is False
    assert assessment.actual_energy_kwh is None
    assert "measurement_incomplete" in assessment.reasons


async def test_daily_correction_cannot_be_repaired_by_zero_boundary_projection():
    """Korrigierte Tageswerte bleiben auch bei unveränderten Randwerten ungültig."""
    history = _history(kind="daily")
    start = datetime.combine(DAY, time(9), UTC)
    end = start + MINUTE
    history.add_reading(start - MINUTE, 10, "kWh")
    history.add_reading(start - MINUTE / 2, 9, "kWh")
    history.add_reading(start + MINUTE / 2, 9, "kWh")
    history.add_reading(end + MINUTE / 2, 9, "kWh")
    snapshot = history.snapshot(start, end, end + MINUTE)
    assert snapshot["energy_complete"] is False
    assert "daily_correction" in snapshot["quality_flags"]
    [window] = await async_interval_windows([history], [(start, end)], end + MINUTE)
    assert window["energy_kwh"] is None


async def test_independent_polling_phases_complete_days_without_splitting_daytime():
    """Nullnächte erlauben Tagessummen, aber keine erfundenen positiven Teilmengen."""
    histories = [
        _day_history(DAY, "Europe/Berlin", phase=7, source_id="first"),
        _day_history(DAY, "Europe/Berlin", phase=19, source_id="second"),
    ]
    start, end = _bounds(DAY, "Europe/Berlin")
    now = end + MINUTE
    total = aggregate_energy(histories, start, end, now)
    assert total["energy_complete"] is True
    assert total["energy_kwh"] == pytest.approx(24)
    [window] = await async_interval_windows(histories, [(start, end)], now)
    assert window["energy_complete"] is True
    assert window["energy_kwh"] == pytest.approx(24)
    assert _assess_day(histories, DAY, "Europe/Berlin", now).actual_energy_kwh == 24

    noon = datetime.combine(DAY, time(12), ZoneInfo("Europe/Berlin")).astimezone(UTC)
    current = [
        _day_history(DAY, "Europe/Berlin", phase=7, source_id="first", until=noon),
        _day_history(DAY, "Europe/Berlin", phase=19, source_id="second", until=noon),
    ]
    outlook = build_day_outlook(
        forecast(DAY, timezone="Europe/Berlin"),
        current,
        "Europe/Berlin",
        noon,
        noon,
        True,
    )
    assert outlook["status"] == "unavailable"
    assert outlook["reason"] == "no_common_measurement_boundary"
    assert outlook["total_kwh"] is None
