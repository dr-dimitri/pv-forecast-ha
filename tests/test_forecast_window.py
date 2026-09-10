"""Fenstervertrag: exakte Energie, begrenzte Raster und ehrliche Leerzustände."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from math import fsum
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.forecast_window import query_forecast_window
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    TotalForecastInterval,
)

START = datetime(2026, 9, 10, 8, tzinfo=UTC)


def forecast(powers=(2, 4, 0), start=START, zone="UTC", days=2):
    return ForecastResult(
        start.astimezone(ZoneInfo(zone)).date(),
        {},
        DailyYield(0, 0),
        tuple(
            TotalForecastInterval(
                start + timedelta(hours=i), start + timedelta(hours=i + 1), p, p
            )
            for i, p in enumerate(powers)
        ),
        forecast_days=days,
    )


def query(data=None, **kwargs):
    args = {"start": START, "end": START + timedelta(hours=2), **kwargs}
    now = args.pop("now", START)
    return query_forecast_window(
        data or forecast(),
        args.pop("zone", "UTC"),
        now,
        args.pop("fetched_at", now),
        args.pop("success", True),
        **args,
    )


def test_shifted_window_analytic_energy_and_power():
    result = query(
        start=START + timedelta(minutes=15), end=START + timedelta(hours=2, minutes=15)
    )
    assert result["energy_kwh"] == 5.5
    assert result["mean_ac_power_kw"] == 2.75
    assert result["scope"] == "total"
    assert result["coverage"]["complete"]
    assert "intervals" not in result
    assert result["uncertainty"]["reason"] == "unsupported_horizon"


@pytest.mark.parametrize("step", [5, 15, 30, 60])
def test_grid_conserves_energy_with_unrounded_seconds(step):
    result = query(
        start=START + timedelta(seconds=13, microseconds=123),
        end=START + timedelta(hours=2, seconds=13, microseconds=123),
        step_minutes=step,
    )
    assert result["status"] == "available"
    assert fsum(item["energy_kwh"] for item in result["intervals"]) == pytest.approx(
        result["energy_kwh"], rel=1e-12, abs=1e-9
    )
    assert result["intervals"][0]["start"].endswith("13.000123+00:00")


def test_half_hour_and_zero_are_available():
    result = query(step_minutes=30)
    assert [item["energy_kwh"] for item in result["intervals"]] == [1, 1, 2, 2]
    assert [item["mean_ac_power_kw"] for item in result["intervals"]] == [2, 2, 4, 4]
    result = query(forecast((0, 0)), step_minutes=30)
    assert result["status"] == "available"
    assert result["energy_kwh"] == result["mean_ac_power_kw"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fetched_at": None},
        {"success": False},
        {"fetched_at": START - timedelta(minutes=60, microseconds=1)},
        {"fetched_at": START + timedelta(microseconds=1)},
    ],
)
def test_stale_data_never_returns_operational_numbers(kwargs):
    result = query(step_minutes=30, **kwargs)
    assert result["reason"] == "stale_forecast"
    assert result["energy_kwh"] is result["mean_ac_power_kw"] is None
    assert result["intervals"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "left",
        "middle",
        "right",
        "duplicate",
        "overlap",
        "incomplete",
        "zero_duration",
        "reverse",
        "fallback",
    ],
)
def test_bad_coverage_or_quality(fault):
    data = forecast((2, 4, 6))
    items = list(data.total_intervals)
    if fault in ("left", "middle", "right"):
        items.pop(("left", "middle", "right").index(fault))
    elif fault == "duplicate":
        items.append(items[0])
    elif fault == "overlap":
        items[1] = replace(items[1], start=START + timedelta(minutes=30))
    elif fault == "incomplete":
        items[1] = replace(items[1], is_complete=False)
    elif fault in ("zero_duration", "reverse"):
        items.append(
            replace(items[0], end=START - timedelta(seconds=fault == "reverse"))
        )
    else:
        items[1] = replace(items[1], quality_flags=("missing_gti",))
    result = query(
        replace(data, total_intervals=tuple(items)),
        end=START + timedelta(hours=3),
        step_minutes=30,
    )
    assert result["reason"] == (
        "input_fallbacks" if fault == "fallback" else "incomplete_forecast"
    )
    assert result["energy_kwh"] is None
    assert result["intervals"] == []


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -1, 10**400]
)
@pytest.mark.parametrize("field", ["energy_kwh", "ac_power_kw"])
def test_invalid_interval_numbers(field, value):
    data = forecast()
    data = replace(
        data,
        total_intervals=(
            replace(data.total_intervals[0], **{field: value}),
            *data.total_intervals[1:],
        ),
    )
    assert query(data)["reason"] == "incomplete_forecast"


def test_sum_and_mean_overflow_are_unavailable():
    assert query(forecast((1e308, 1e308)))["reason"] == "incomplete_forecast"
    data = replace(
        forecast(),
        total_intervals=(
            TotalForecastInterval(START, START + timedelta(seconds=1), 1e308, 1e308),
        ),
    )
    assert (
        query(data, end=START + timedelta(seconds=1))["reason"] == "incomplete_forecast"
    )


def test_non_requested_gap_and_fallback_do_not_block_window():
    data = forecast((2, 4, 8), days=7)
    data = replace(
        data,
        total_intervals=(
            *data.total_intervals[:2],
            replace(data.total_intervals[2], quality_flags=("missing_gti",)),
        ),
    )
    assert query(data)["status"] == "available"
    assert (
        query(replace(data, total_intervals=data.total_intervals[:2]))["status"]
        == "available"
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"step_minutes": v} for v in (True, False, 0, 10, 30.0, "30", float("nan"))]
    + [
        {"end": START},
        {"end": START - timedelta(seconds=1)},
        {"start": START.replace(tzinfo=None)},
        {"end": START + timedelta(seconds=61), "step_minutes": 5},
        {"end": START + timedelta(minutes=337 * 5), "step_minutes": 5},
    ],
)
def test_invalid_request(kwargs):
    with pytest.raises(ValueError):
        query(**kwargs)


@pytest.mark.parametrize(
    "day,zone",
    [
        (date(2026, 3, 29), "Europe/Berlin"),
        (date(2026, 10, 25), "Europe/Berlin"),
        (date(2026, 9, 10), "Asia/Kathmandu"),
    ],
)
@pytest.mark.parametrize("days", [2, 7])
def test_local_horizon_dst_fold_midnight_and_quarter_zone(day, zone, days):
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=days), time.min, tz).astimezone(UTC)
    count = int((end - start).total_seconds() / 3600)
    data = forecast((1,) * count, start, zone, days)
    result = query(data, zone=zone, now=start, start=start, end=end, step_minutes=60)
    assert result["status"] == "available"
    assert result["energy_kwh"] == count
    assert len(result["intervals"]) == count
    assert len({i["start"] for i in result["intervals"]}) == count
    assert (
        query(data, zone=zone, now=start, start=start, end=end + timedelta(seconds=1))[
            "reason"
        ]
        == "outside_forecast"
    )
