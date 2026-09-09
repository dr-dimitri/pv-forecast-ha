"""Solarplanung: zusammenhängende Energie, UTC-Grenzen und ehrliche Leerzustände."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    TotalForecastInterval,
)
from custom_components.pv_forecast.planning import plan_solar_window

START = datetime(2026, 9, 9, 8, tzinfo=UTC)


def forecast(powers, start=START):
    intervals = tuple(
        TotalForecastInterval(
            start + timedelta(hours=index),
            start + timedelta(hours=index + 1),
            power,
            power,
        )
        for index, power in enumerate(powers)
    )
    return ForecastResult(start.date(), {}, DailyYield(0, 0), intervals)


def plan(data, **kwargs):
    options = {
        "duration_minutes": 120,
        "earliest_start": data.total_intervals[0].start,
        "latest_end": data.total_intervals[-1].end,
        **kwargs,
    }
    now = options.pop("now", data.total_intervals[0].start)
    fetched = options.pop("fetched_at", now)
    success = options.pop("last_update_success", True)
    zone = options.pop("timezone_name", "UTC")
    return plan_solar_window(data, zone, now, fetched, success, **options)


def test_contiguous_window_beats_single_peak():
    result = plan(forecast([0, 8, 0, 6, 6, 0]))
    assert result["status"] == "available"
    assert result["start"] == "2026-09-09T11:00:00+00:00"
    assert result["energy_kwh"] == 12
    assert result["uncertainty"]["reason"] == "unsupported_horizon"


def test_fractional_duration_and_shifted_boundary():
    result = plan(forecast([1, 5, 6, 0]), duration_minutes=90)
    assert result["start"] == "2026-09-09T09:30:00+00:00"
    assert result["end"] == "2026-09-09T11:00:00+00:00"
    assert result["energy_kwh"] == 8.5


def test_equal_windows_choose_earliest_absolute_start():
    result = plan(forecast([2, 2, 2]), duration_minutes=60)
    assert result["start"] == START.isoformat()


@pytest.mark.parametrize(
    ("powers", "kwargs", "reason"),
    [
        ([0, 0], {}, "no_solar_energy"),
        ([1, 1], {"duration_minutes": 121}, "infeasible_window"),
        ([1, 1], {"fetched_at": None}, "stale_forecast"),
        ([1, 1], {"last_update_success": False}, "stale_forecast"),
        ([1, 1], {"fetched_at": START - timedelta(minutes=61)}, "stale_forecast"),
        ([1, 1], {"fetched_at": START + timedelta(seconds=1)}, "stale_forecast"),
        ([1, 1], {"latest_end": START + timedelta(days=3)}, "outside_forecast"),
    ],
)
def test_no_false_recommendation(powers, kwargs, reason):
    result = plan(forecast(powers), **kwargs)
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["energy_kwh"] is None


def test_missing_interval_or_fallback_prevents_claim_of_best_window():
    data = forecast([2, 8, 2])
    gap = replace(data, total_intervals=data.total_intervals[::2])
    assert plan(gap)["reason"] == "incomplete_forecast"
    fallback = replace(
        data,
        total_intervals=(
            replace(data.total_intervals[0], quality_flags=("missing_gti",)),
            *data.total_intervals[1:],
        ),
    )
    assert plan(fallback)["reason"] == "input_fallbacks"


def test_hysteresis_keeps_valid_window_until_material_improvement():
    kwargs = {"duration_minutes": 60, "previous_start": START + timedelta(hours=1)}
    result = plan(forecast([0, 2, 2.08]), **kwargs)
    assert result["start"] == kwargs["previous_start"].isoformat()
    assert result["hysteresis_applied"]
    result = plan(forecast([0, 2, 3]), **kwargs)
    assert result["start"] == "2026-09-09T10:00:00+00:00"


def test_started_or_completed_consumer_is_never_rescheduled():
    data = forecast([1, 2, 4, 8])
    for minutes, status in ((30, "started"), (180, "completed")):
        result = plan(
            data, previous_start=START, now=START + timedelta(minutes=minutes)
        )
        assert result["status"] == status
        assert result["start"] == START.isoformat()
        assert result["energy_kwh"] is None


def test_started_window_remains_anchored_across_local_midnight():
    start = datetime(2026, 9, 9, 23, tzinfo=UTC)
    data = forecast([1, 1, 2], start)
    result = plan(data, previous_start=start, now=start + timedelta(hours=1))
    assert result["status"] == "started"
    assert result["start"] == start.isoformat()


@pytest.mark.parametrize("minutes", [30, 180])
def test_started_window_survives_rolling_search_boundaries(minutes):
    """Fortgeschriebene Suchgrenzen starten keine zweite Verbraucherplanung."""

    data = forecast([1, 2, 4, 8])
    now = START + timedelta(minutes=minutes)
    result = plan(
        data,
        previous_start=START,
        now=now,
        earliest_start=now,
        latest_end=now + timedelta(hours=1),
    )
    assert result["status"] == ("started" if minutes < 120 else "completed")
    assert result["start"] == START.isoformat()
    assert result["end"] == (START + timedelta(hours=2)).isoformat()
    assert result["energy_kwh"] is None


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_dst_days_keep_elapsed_duration_and_both_fold_hours(day):
    zone = ZoneInfo("Europe/Berlin")
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), zone
    ).astimezone(UTC)
    count = int((end - start).total_seconds() / 3600)
    assert count in (23, 25)
    data = forecast([1] * count, start)
    result = plan(data, duration_minutes=120, timezone_name="Europe/Berlin")
    assert result["status"] == "available"
    assert result["energy_kwh"] == 2
    assert datetime.fromisoformat(result["end"]) - datetime.fromisoformat(
        result["start"]
    ) == timedelta(hours=2)


def test_half_hour_zone_and_day_boundary():
    start = datetime(2026, 9, 8, 18, 30, tzinfo=UTC)
    data = forecast([1] * 48, start)
    result = plan(
        data,
        duration_minutes=90,
        earliest_start=start + timedelta(hours=23, minutes=30),
        latest_end=start + timedelta(hours=26),
        timezone_name="Asia/Kolkata",
    )
    assert result["status"] == "available"
    assert result["energy_kwh"] == 1.5


@pytest.mark.parametrize("duration", [True, 0, -1, 2881, 1.5, float("nan")])
def test_invalid_duration(duration):
    with pytest.raises(ValueError):
        plan(forecast([1, 1]), duration_minutes=duration)


def test_ambiguous_time_is_rejected():
    with pytest.raises(ValueError):
        plan(forecast([1, 1]), earliest_start=START.replace(tzinfo=None))
