"""Tests der gemeinsamen Gesamtzeitreihe und ihrer gleitenden Planungsfenster."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.calculations import (
    aggregate_energy_for_day,
    calculate_forecast,
    calculate_planning_values,
)
from custom_components.pv_forecast.models import ForecastResult, WeatherInterval

from .helpers import TIMEZONE, roof, weather

DAY = date(2026, 8, 23)


def _complete_forecast(
    day: date = DAY,
    timezone: tzinfo = TIMEZONE,
    power: float = 1,
    powers: dict[int, float] | None = None,
) -> ForecastResult:
    """Zwei lokale Tage mit konstanten oder einzeln gesetzten UTC-Stunden bauen."""

    first = datetime.combine(day, time.min, timezone).astimezone(UTC)
    last = datetime.combine(day + timedelta(days=2), time.min, timezone).astimezone(UTC)
    first = first.replace(minute=0, second=0, microsecond=0)
    count = int((last - first).total_seconds() / 3600)
    if first + timedelta(hours=count) < last:
        count += 1
    points = tuple(
        WeatherInterval(
            first + timedelta(hours=index),
            first + timedelta(hours=index + 1),
            1000 * (powers.get(index, power) if powers is not None else power),
            25,
        )
        for index in range(count)
    )
    return calculate_forecast((roof(power=1),), {"roof_1": points}, None, day, timezone)


def test_total_intervals_match_clipped_roofs_and_daily_sums() -> None:
    """Dachbeiträge, Gesamtintervalle und Tageswerte teilen geclippte Energien."""

    first = weather(end=datetime(2026, 8, 23, 12, tzinfo=TIMEZONE))
    second = weather(end=datetime(2026, 8, 24, 12, tzinfo=TIMEZONE))
    result = calculate_forecast(
        (roof("a", power=6), roof("b", power=5)),
        {"a": (first, second), "b": (first, second)},
        8,
        DAY,
        TIMEZONE,
    )

    assert len(result.total_intervals) == 2
    for index, interval in enumerate(result.total_intervals):
        assert interval.ac_power_kw == pytest.approx(8)
        assert interval.energy_kwh == pytest.approx(
            sum(item.intervals[index].energy_kwh for item in result.roofs.values())
        )
        assert interval.is_complete
        assert interval.quality_flags == ()
    assert result.total.today == pytest.approx(8)
    assert result.total.tomorrow == pytest.approx(8)


@pytest.mark.parametrize("zone", ["Asia/Kolkata", "Asia/Kathmandu"])
def test_fractional_offset_clips_only_outer_boundaries(zone: str) -> None:
    """Randenergie wird beschnitten, die innere Mitternacht teilt keine Stunde."""

    timezone = ZoneInfo(zone)
    result = _complete_forecast(timezone=timezone)
    start = datetime.combine(DAY, time.min, timezone).astimezone(UTC)
    midnight = datetime.combine(DAY + timedelta(days=1), time.min, timezone).astimezone(
        UTC
    )
    end = datetime.combine(DAY + timedelta(days=2), time.min, timezone).astimezone(UTC)

    assert result.total_intervals[0].start == start
    assert result.total_intervals[-1].end == end
    assert len(result.total_intervals) == 49
    assert any(item.start < midnight < item.end for item in result.total_intervals)
    assert sum(item.energy_kwh for item in result.total_intervals) == pytest.approx(48)
    assert result.total.today == pytest.approx(24)
    assert result.total.tomorrow == pytest.approx(24)
    assert aggregate_energy_for_day(result.total_intervals, DAY, timezone) == 24
    for item in result.total_intervals:
        assert item.energy_kwh == pytest.approx(
            item.ac_power_kw * (item.end - item.start).total_seconds() / 3600
        )


def test_next_hour_uses_partial_intervals_and_current_mean_power() -> None:
    """Um 10:30 ergeben 2 kW bis 11 Uhr und 4 kW danach genau 3 kWh."""

    forecast = _complete_forecast(power=0, powers={10: 2, 11: 4})
    now = datetime(2026, 8, 23, 10, 30, tzinfo=TIMEZONE)
    planning = calculate_planning_values(forecast, now, TIMEZONE)

    assert planning.next_60_minutes_kwh == pytest.approx(3)
    assert planning.remaining_today_kwh == pytest.approx(5)
    assert planning.power_now_kw == 2
    assert planning.peak_today == datetime(2026, 8, 23, 9, tzinfo=UTC)
    assert planning.peak_today_complete
    # Der Beginn des nächsten halb offenen Intervalls gehört schon zu diesem.
    exact = calculate_planning_values(
        forecast, now.replace(hour=11, minute=0), TIMEZONE
    )
    assert exact.power_now_kw == 4
    assert exact.next_60_minutes_kwh == 4


def test_peak_uses_whole_day_and_earliest_equal_absolute_start() -> None:
    """Ein verstrichenes Maximum bleibt sichtbar; gleiche Spitzen wählen früher."""

    forecast = _complete_forecast(power=0, powers={5: 4, 10: 4})
    now = datetime(2026, 8, 23, 18, tzinfo=TIMEZONE)
    planning = calculate_planning_values(forecast, now, TIMEZONE)

    assert planning.remaining_today_kwh == 0
    assert planning.peak_today == datetime(2026, 8, 23, 3, tzinfo=UTC)


def test_zero_day_is_covered_without_peak() -> None:
    """Ein vollständig abgedeckter Nulltag ist von fehlenden Daten unterscheidbar."""

    forecast = _complete_forecast(power=0)
    planning = calculate_planning_values(
        forecast, datetime(2026, 8, 23, 10, 30, tzinfo=TIMEZONE), TIMEZONE
    )

    assert planning.remaining_today_kwh == 0
    assert planning.next_60_minutes_kwh == 0
    assert planning.power_now_kw == 0
    assert planning.peak_today is None
    assert planning.peak_today_complete


def test_missing_coverage_does_not_turn_into_zero_energy() -> None:
    """Eine Lücke sperrt betroffene Fenster, aber nicht vorhandene laufende Werte."""

    forecast = _complete_forecast(power=0)
    forecast = replace(
        forecast,
        total_intervals=forecast.total_intervals[:11] + forecast.total_intervals[12:],
    )
    planning = calculate_planning_values(
        forecast, datetime(2026, 8, 23, 10, 30, tzinfo=TIMEZONE), TIMEZONE
    )

    assert planning.remaining_today_kwh is None
    assert planning.next_60_minutes_kwh is None
    assert planning.power_now_kw == 0
    assert planning.peak_today is None
    assert not planning.peak_today_complete


def test_missing_roof_marks_interval_incomplete() -> None:
    """Ein fehlendes Dach verhindert auch bei null vorhandenem GTI die Vollabdeckung."""

    forecast = calculate_forecast(
        (roof("a"), roof("b")),
        {"a": (weather(0),)},
        None,
        DAY,
        TIMEZONE,
    )
    interval = forecast.total_intervals[0]
    planning = calculate_planning_values(forecast, interval.start, TIMEZONE)

    assert not interval.is_complete
    assert interval.quality_flags == ("missing_roof_data",)
    assert interval.energy_kwh == 0
    assert planning.power_now_kw is None
    assert planning.next_60_minutes_kwh is None


def test_midnight_reinterprets_today_without_changing_forecast_data() -> None:
    """Beim Tageswechsel bleiben vorhandene Daten nutzbar bis zu ihrer echten Grenze."""

    forecast = _complete_forecast()
    before = calculate_planning_values(
        forecast, datetime(2026, 8, 23, 23, 30, tzinfo=TIMEZONE), TIMEZONE
    )
    after = calculate_planning_values(
        forecast, datetime(2026, 8, 24, 0, tzinfo=TIMEZONE), TIMEZONE
    )
    expired = calculate_planning_values(
        forecast, datetime(2026, 8, 25, 0, tzinfo=TIMEZONE), TIMEZONE
    )

    assert before.remaining_today_kwh == pytest.approx(0.5)
    assert before.next_60_minutes_kwh == 1
    assert after.remaining_today_kwh == 24
    assert after.next_60_minutes_kwh == 1
    assert after.peak_today == datetime(2026, 8, 23, 22, tzinfo=UTC)
    assert expired.remaining_today_kwh is None
    assert expired.next_60_minutes_kwh is None
    assert expired.power_now_kw is None
    assert expired.peak_today is None
    assert not expired.peak_today_complete


@pytest.mark.parametrize(
    ("day", "hours", "hour", "fold", "remaining"),
    [
        (date(2026, 3, 29), 23, 1, 0, 21.5),
        (date(2026, 10, 25), 25, 2, 0, 22.5),
        (date(2026, 10, 25), 25, 2, 1, 21.5),
    ],
)
def test_dst_windows_always_use_elapsed_utc_time(
    day: date, hours: int, hour: int, fold: int, remaining: float
) -> None:
    """Gleitende Fenster dauern bei beiden Zeitumstellungen exakt 60 Minuten."""

    forecast = _complete_forecast(day=day)
    now = datetime.combine(day, time(hour, 30), TIMEZONE).replace(fold=fold)
    planning = calculate_planning_values(forecast, now, TIMEZONE)

    assert forecast.total.today == hours
    assert planning.remaining_today_kwh == pytest.approx(remaining)
    assert planning.next_60_minutes_kwh == 1
    assert planning.power_now_kw == 1
    assert planning.peak_today_complete


def test_autumn_peak_keeps_earliest_fold() -> None:
    """Die frühere der beiden gleich hohen 02-Uhr-Stunden gewinnt absolut."""

    forecast = _complete_forecast(day=date(2026, 10, 25), power=0, powers={2: 4, 3: 4})
    now = datetime(2026, 10, 25, 12, tzinfo=TIMEZONE)
    planning = calculate_planning_values(forecast, now, TIMEZONE)

    assert planning.peak_today == datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert planning.peak_today.astimezone(TIMEZONE).fold == 0


def test_fractional_boundary_peak_compares_mean_power() -> None:
    """Eine beschnittene starke Randstunde verliert nicht wegen ihres Beschnitts."""

    timezone = ZoneInfo("Asia/Kathmandu")
    forecast = _complete_forecast(timezone=timezone, power=1, powers={0: 1.2})
    now = datetime.combine(DAY, time(12), timezone)
    planning = calculate_planning_values(forecast, now, timezone)

    assert forecast.total_intervals[0].energy_kwh == pytest.approx(0.9)
    assert planning.peak_today == datetime.combine(DAY, time.min, timezone).astimezone(
        UTC
    )


def test_last_hour_window_requires_full_remaining_coverage() -> None:
    """Am letzten Prognoserand ist ein halbes Fenster keine vollständige Stunde."""

    forecast = _complete_forecast()
    now = datetime(2026, 8, 24, 23, 30, tzinfo=TIMEZONE)
    planning = calculate_planning_values(forecast, now, TIMEZONE)

    assert planning.remaining_today_kwh == pytest.approx(0.5)
    assert planning.next_60_minutes_kwh is None
    assert planning.power_now_kw == 1
