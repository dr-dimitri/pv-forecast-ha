"""Messpräfix, geschätzte Brücke und Resttag dürfen sich nicht überschneiden."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.measurements import SourceConfig, SourceHistory
from custom_components.pv_forecast.outlook import build_day_outlook

from .test_solar_window import forecast

START = datetime(2026, 9, 9, tzinfo=UTC)
NOON = START + timedelta(hours=12)


def measurements(source_id="a", timezone="UTC", start=START, max_interval_minutes=60):
    history = SourceHistory(
        SourceConfig(
            source_id,
            f"sensor.{source_id}",
            "total",
            "AC-PV",
            max_interval_minutes=max_interval_minutes,
        ),
        timezone,
        20,
    )
    history.add_reading(start, 100, "kWh")
    return history


def outlook(histories, *, now=NOON, data=None, fetched_at=NOON, **kwargs):
    return build_day_outlook(
        data or forecast([1] * 24, START),
        histories,
        "UTC",
        now,
        fetched_at,
        True,
        **kwargs,
    )


def test_eight_measured_plus_twelve_future_make_twenty():
    history = measurements()
    history.add_reading(NOON, 108, "kWh")
    result = outlook([history])
    assert result["status"] == "available"
    assert result["measured_kwh"] == 8
    assert result["bridge_kwh"] == 0
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] == 20
    assert result["correction"] == "off"


def test_unmeasured_bridge_is_forecast_and_never_actual():
    history = measurements()
    last = NOON - timedelta(minutes=30)
    history.add_reading(last, 108, "kWh")
    result = outlook([history])
    assert result["measured_until"] == last.isoformat()
    assert result["measured_kwh"] == 8
    assert result["bridge_kwh"] == 0.5
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] == 20.5


def test_silent_counter_keeps_outlook_with_explicit_measurement_age():
    history = measurements()
    last = NOON - timedelta(hours=6)
    for minute in range(5, 361, 5):
        history.add_reading(START + timedelta(minutes=minute), 100 + minute / 60, "kWh")
    result = outlook([history])
    assert result["schema_version"] == 1
    assert result["status"] == "available"
    assert result["reason"] is None
    assert result["measured_until"] == last.isoformat()
    assert result["measurement_age_minutes"] == 360
    assert result["measurement_stale"] is True
    assert result["measurement_quality_flags"] == ["stale"]
    assert result["forecast_quality_flags"] == []
    assert result["quality_flags"] == ["stale"]
    assert result["measured_kwh"] == 6
    assert result["bridge_kwh"] == 6
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] == 24


@pytest.mark.parametrize("max_interval_minutes", [5, 30, 60, 180])
@pytest.mark.parametrize("overdue_seconds", [-60, 0, 1])
def test_measurement_staleness_uses_confirmed_source_threshold(
    max_interval_minutes, overdue_seconds
):
    history = measurements(max_interval_minutes=max_interval_minutes)
    age = timedelta(minutes=max_interval_minutes, seconds=overdue_seconds)
    history.add_reading(NOON - age, 108, "kWh")
    result = outlook([history])
    assert result["status"] == "available"
    assert result["measurement_age_minutes"] == age.total_seconds() / 60
    assert result["measurement_stale"] is (overdue_seconds > 0)
    assert ("stale" in result["measurement_quality_flags"]) is (overdue_seconds > 0)


def test_sources_use_their_own_freshness_limit_and_preserve_common_boundary():
    first = measurements(max_interval_minutes=5)
    second = measurements("b", max_interval_minutes=60)
    common = NOON - timedelta(minutes=30)
    first.add_reading(common, 103, "kWh")
    second.add_reading(common, 104, "kWh")
    first.add_reading(NOON - timedelta(minutes=2), 104, "kWh")
    result = outlook([first, second])
    assert result["measurement_age_minutes"] == 30
    assert result["measurement_stale"] is False
    assert "stale" not in result["measurement_quality_flags"]
    assert result["measured_until"] == common.isoformat()
    assert result["measured_kwh"] == 7

    # Gleiche Messungen, jetzt eine überschrittene individuelle Meldefrist.
    later = NOON + timedelta(minutes=4)
    result = outlook([first, second], now=later, fetched_at=later)
    assert result["measurement_age_minutes"] == 34
    assert result["measurement_stale"] is True
    assert result["status"] == "available"
    assert result["measured_until"] == common.isoformat()
    assert result["measured_kwh"] == 7


def test_fresh_offset_sources_do_not_become_stale_from_old_common_boundary():
    first, second = measurements(max_interval_minutes=5), measurements(
        "b", max_interval_minutes=5
    )
    common = NOON - timedelta(hours=2)
    first.add_reading(common, 104, "kWh")
    second.add_reading(common, 103, "kWh")
    first.add_reading(NOON - timedelta(minutes=1), 105, "kWh")
    second.add_reading(NOON - timedelta(minutes=2), 104, "kWh")
    result = outlook([first, second])
    assert result["measurement_age_minutes"] == 120
    assert result["measurement_stale"] is False
    assert "stale" not in result["quality_flags"]
    assert result["bridge_kwh"] == 2


def test_measurement_and_forecast_quality_remain_distinguishable():
    history = measurements()
    history.add_reading(NOON - timedelta(hours=2), 108, "kWh")
    data = forecast([1] * 24, START)
    data = replace(
        data,
        total_intervals=tuple(
            replace(interval, quality_flags=("missing_temperature",))
            for interval in data.total_intervals
        ),
    )
    result = outlook([history], data=data)
    assert result["status"] == "unavailable"
    assert result["reason"] == "input_fallbacks"
    assert result["measurement_stale"] is True
    assert "stale" in result["measurement_quality_flags"]
    assert "missing_temperature" not in result["measurement_quality_flags"]
    assert result["forecast_quality_flags"] == ["missing_temperature"]
    assert result["quality_flags"] == sorted(
        {*result["measurement_quality_flags"], "missing_temperature"}
    )

    stale_forecast = outlook([history], data=data, fetched_at=NOON - timedelta(hours=2))
    assert stale_forecast["reason"] == "stale_forecast"
    assert stale_forecast["measurement_stale"] is True
    assert stale_forecast["forecast_quality_flags"] == ["missing_temperature"]
    # Die bisherige Liste wird an frühen Rückgaben nicht nachträglich erweitert.
    assert stale_forecast["quality_flags"] == result["measurement_quality_flags"]


def test_disjoint_sources_need_common_exact_measurement_boundary():
    first, second = measurements(), measurements("b")
    first.add_reading(NOON - timedelta(hours=1), 104, "kWh")
    second.add_reading(NOON - timedelta(hours=1), 103, "kWh")
    first.add_reading(NOON, 105, "kWh")
    second.add_reading(NOON - timedelta(minutes=15), 104, "kWh")
    result = outlook([first, second])
    assert result["measured_until"] == (NOON - timedelta(hours=1)).isoformat()
    assert result["measured_kwh"] == 7
    assert result["bridge_kwh"] == 1
    assert result["total_kwh"] == 20


def test_missing_prefix_and_missing_common_boundary_remain_unknown():
    first, second = measurements(), measurements("b")
    first.add_reading(NOON, 108, "kWh")
    assert outlook([first, second])["reason"] == "no_common_measurement_boundary"
    missing = measurements(start=START + timedelta(hours=1))
    missing.add_reading(NOON, 108, "kWh")
    result = outlook([missing])
    assert result["reason"] == "incomplete_measurements"
    assert result["total_kwh"] is None
    assert result["remaining_kwh"] == 12


def test_current_view_never_adds_yield_from_previous_location():
    history = measurements()
    history.bind_location("old", "UTC", START)
    history.add_reading(START + timedelta(hours=6), 108, "kWh")
    history.bind_location("new", "UTC", START + timedelta(hours=7))
    history.add_reading(START + timedelta(hours=7), 108, "kWh")
    history.add_reading(NOON, 110, "kWh")
    result = outlook([history.current_location_view()])
    assert result["status"] == "unavailable"
    assert result["reason"] == "incomplete_measurements"
    assert result["measured_kwh"] is None
    assert result["total_kwh"] is None


def test_counter_decrease_and_source_change_do_not_create_complete_day():
    history = measurements()
    history.add_reading(START + timedelta(hours=6), 104, "kWh")
    history.add_reading(START + timedelta(hours=7), 0, "kWh")
    history.add_reading(NOON, 4, "kWh")
    assert outlook([history])["reason"] == "incomplete_measurements"


def test_stale_forecast_keeps_measurement_but_withholds_total():
    history = measurements()
    history.add_reading(NOON, 108, "kWh")
    result = outlook([history], fetched_at=NOON - timedelta(hours=2))
    assert result["reason"] == "stale_forecast"
    assert result["measured_kwh"] == 8
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] is None
    assert outlook([history], identity_unresolved=True)["total_kwh"] is None


def test_forecast_gap_does_not_become_zero_bridge():
    history = measurements()
    history.add_reading(NOON - timedelta(hours=1), 108, "kWh")
    data = forecast([1] * 24, START)
    data = replace(
        data, total_intervals=data.total_intervals[:11] + data.total_intervals[12:]
    )
    result = outlook([history], data=data)
    assert result["reason"] == "incomplete_forecast"
    assert result["bridge_kwh"] is None
    assert result["remaining_kwh"] == 12


@pytest.mark.parametrize("powers", [[0] * 24, [2] * 6 + [0] * 6 + [2] * 12])
def test_zero_production_and_changing_weather_never_create_a_factor(powers):
    history = measurements()
    history.add_reading(NOON, 100, "kWh")
    result = outlook([history], data=forecast(powers, START))
    assert result["total_kwh"] == sum(powers[12:])
    assert result["correction"] == "off"


@pytest.mark.parametrize("day", [datetime(2026, 3, 29), datetime(2026, 10, 25)])
def test_dst_outlook_keeps_the_actual_local_day_length(day):
    zone = ZoneInfo("Europe/Berlin")
    start = day.replace(tzinfo=zone).astimezone(UTC)
    end = (day + timedelta(days=1)).replace(tzinfo=zone).astimezone(UTC)
    count = int((end - start).total_seconds() / 3600)
    now = start + timedelta(hours=12)
    history = measurements(timezone="Europe/Berlin", start=start)
    history.add_reading(now, 108, "kWh")
    result = build_day_outlook(
        forecast([1] * count, start), [history], "Europe/Berlin", now, now, True
    )
    assert result["status"] == "available"
    assert result["total_kwh"] == 8 + count - 12
