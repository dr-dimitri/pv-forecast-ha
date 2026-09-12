"""Modellierte Einflüsse gegen tatsächlich vorhandene AC-Zeitreihen prüfen."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.calculations import (
    aggregate_energy_for_day,
    apply_calibration,
    apply_inverter_limits,
)
from custom_components.pv_forecast.explanation import (
    build_explanation,
    explanation_view,
)
from custom_components.pv_forecast.models import (
    AcInverterGroup,
    DailyYield,
    ForecastResult,
    RoofForecast,
    RoofForecastInterval,
    TotalForecastInterval,
)
from custom_components.pv_forecast.morning import apply_morning, morning_window

from .helpers import roof


def basis(
    powers=None,
    limit=None,
    groups=(),
    zone="UTC",
    day=date(2026, 9, 10),
    forecast_days=2,
):
    """Konstante DC-Beiträge und ihre produktive Begrenzung im Prognosehorizont."""
    powers = powers if powers is not None else {"a": 10.0}
    timezone = ZoneInfo(zone)
    left = datetime.combine(day, time.min, timezone).astimezone(UTC)
    right = datetime.combine(
        day + timedelta(days=forecast_days), time.min, timezone
    ).astimezone(UTC)
    cursor = left.replace(minute=0)
    boundaries = []
    while cursor < right:
        boundaries.append((cursor, cursor + timedelta(hours=1)))
        cursor += timedelta(hours=1)
    ac = apply_inverter_limits(powers, limit, groups)
    roofs = {
        key: RoofForecast(
            roof(key),
            tuple(
                RoofForecastInterval(a, b, dc, ac[key], ac[key]) for a, b in boundaries
            ),
            DailyYield(0, 0),
        )
        for key, dc in powers.items()
    }
    intervals = tuple(
        TotalForecastInterval(
            max(a, left),
            min(b, right),
            sum(ac.values()) * (min(b, right) - max(a, left)).total_seconds() / 3600,
            sum(ac.values()),
        )
        for a, b in boundaries
    )
    daily = DailyYield(
        aggregate_energy_for_day(intervals, day, timezone),
        aggregate_energy_for_day(intervals, day + timedelta(days=1), timezone),
    )
    return ForecastResult(
        day,
        roofs,
        daily,
        intervals,
        inverter_groups=groups,
        forecast_days=forecast_days,
    )


def explain(raw, factor=1, limit=None, zone="UTC"):
    effective = apply_calibration(raw, factor, limit, ZoneInfo(zone))
    snapshot = build_explanation(raw, effective, factor, limit, zone)
    now = datetime.combine(raw.local_date, time(12), ZoneInfo(zone)).astimezone(UTC)
    result = explanation_view(snapshot, raw, effective, zone, now, now, True)
    return snapshot, result, effective


def test_factor_before_limit_and_effective_difference():
    raw = basis(limit=8)
    snapshot, result, effective = explain(raw, 1.2, 8)
    first = snapshot.intervals[0]
    assert (
        first.before_calibration_kwh,
        first.calibration_delta_kwh,
        first.group_clipping_kwh,
        first.total_clipping_kwh,
        first.effective_kwh,
    ) == (10, 2, 0, 4, 8)
    assert result["status"] == "available"
    assert result["totals"]["effective_kwh"] == effective.total.today == 192
    assert result["totals"]["raw_model_kwh"] == 192
    assert result["totals"]["effective_minus_raw_kwh"] == 0
    assert result["totals"]["calibration_delta_kwh"] == 48


def test_groups_unassigned_and_additional_plant_limit():
    groups = (
        AcInverterGroup("g1", "Gruppe 1", 6, ("a", "b")),
        AcInverterGroup("g2", "Gruppe 2", 3, ("c",)),
    )
    raw = basis({"a": 4, "b": 4, "c": 4, "d": 2}, 10, groups)
    snapshot, result, _ = explain(raw, 1.2, 10)
    first = snapshot.intervals[0]
    assert first.before_calibration_kwh == 14
    assert first.calibration_delta_kwh == pytest.approx(2.8)
    assert first.group_clipping_kwh == pytest.approx(5.4)
    assert first.total_clipping_kwh == pytest.approx(1.4)
    assert first.effective_kwh == pytest.approx(10)
    stages = apply_inverter_limits(
        {"a": 4, "b": 4, "c": 4, "d": 2}, 10, groups, include_stages=True
    )
    assert dict(stages.grouped) == {"a": 3, "b": 3, "c": 3, "d": 2}
    assert dict(stages.effective) == apply_inverter_limits(
        {"a": 4, "b": 4, "c": 4, "d": 2}, 10, groups
    )
    with pytest.raises(TypeError):
        stages.grouped["d"] = 0
    assert result["status"] == "available"


@pytest.mark.parametrize("factor", [0.5, 0.9, 1, 1.5])
def test_unclipped_signed_factor_and_bit_identical_raw(factor):
    raw = basis()
    snapshot, result, effective = explain(raw, factor)
    assert snapshot.reason is None
    assert snapshot.intervals[0].calibration_delta_kwh == pytest.approx(
        10 * (factor - 1)
    )
    assert snapshot.intervals[0].group_clipping_kwh == 0
    assert snapshot.intervals[0].total_clipping_kwh == 0
    if factor == 1:
        assert effective is raw
        assert [i["energy_kwh"] for i in result["raw_intervals"]] == [
            i.energy_kwh for i in raw.total_intervals[:24]
        ]


@pytest.mark.parametrize(
    ("day", "zone", "hours"),
    [
        (date(2026, 3, 29), "Europe/Berlin", 23),
        (date(2026, 10, 25), "Europe/Berlin", 25),
        (date(2026, 9, 10), "Asia/Kathmandu", 24),
    ],
)
def test_day_projection_preserves_dst_and_quarter_hours(day, zone, hours):
    raw = basis(zone=zone, day=day)
    snapshot, result, effective = explain(raw, 1.2, zone=zone)
    assert result["totals"]["effective_kwh"] == 12 * hours
    assert result["totals"]["raw_model_kwh"] == 10 * hours
    now = datetime.combine(day, time(12), ZoneInfo(zone)).astimezone(UTC)
    with patch(
        "custom_components.pv_forecast.explanation.apply_inverter_limits",
        side_effect=AssertionError("Lesen rechnet keine PV"),
    ):
        tomorrow = explanation_view(
            snapshot, raw, effective, zone, now, now, True, day="tomorrow"
        )
    assert tomorrow["status"] == "available"
    assert tomorrow["totals"]["effective_kwh"] == effective.total.tomorrow


@pytest.mark.parametrize("forecast_days", [3, 7])
@pytest.mark.parametrize(
    "target_day,zone,tomorrow_hours",
    [
        (date(2026, 9, 12), "UTC", 24),
        (date(2026, 3, 29), "Europe/Berlin", 23),
        (date(2026, 10, 25), "Europe/Berlin", 25),
        (date(2026, 9, 12), "Asia/Kathmandu", 24),
    ],
)
def test_multiday_explanation_follows_current_days_after_midnight(
    forecast_days, target_day, zone, tomorrow_hours
):
    """Heute und morgen lesen dieselbe geclippte Generation auch am dritten Tag."""
    timezone = ZoneInfo(zone)
    groups = (
        AcInverterGroup("g1", "Gruppe 1", 6, ("a", "b")),
        AcInverterGroup("g2", "Gruppe 2", 3, ("c",)),
    )
    raw = basis(
        {"a": 4, "b": 4, "c": 4, "d": 2},
        10,
        groups,
        zone,
        target_day - timedelta(days=2),
        forecast_days,
    )
    snapshot, _, effective = explain(raw, 1.2, 10, zone)
    now = datetime.combine(
        target_day - timedelta(days=1), time(0, 15), timezone
    ).astimezone(UTC)
    fetched_at = now - timedelta(minutes=30)
    with patch(
        "custom_components.pv_forecast.explanation.apply_inverter_limits",
        side_effect=AssertionError("Lesen rechnet keine PV"),
    ):
        for selected, target, hours in (
            ("today", target_day - timedelta(days=1), 24),
            ("tomorrow", target_day, tomorrow_hours),
        ):
            result = explanation_view(
                snapshot, raw, effective, zone, now, fetched_at, True, day=selected
            )
            start = datetime.combine(target, time.min, timezone).astimezone(UTC)
            end = datetime.combine(
                target + timedelta(days=1), time.min, timezone
            ).astimezone(UTC)
            assert result["status"] == "available"
            assert result["reason"] is None
            assert result["complete"] is True
            assert result["stale"] is False
            assert result["fetched_at"] == fetched_at.isoformat()
            assert result["date"] == target.isoformat()
            assert result["start"] == start.isoformat()
            assert result["end"] == end.isoformat()
            assert result["intervals"][0]["start"] == start.isoformat()
            assert result["intervals"][-1]["end"] == end.isoformat()
            totals = result["totals"]
            for key, hourly_value in {
                "before_calibration_kwh": 14,
                "calibration_delta_kwh": 2.8,
                "group_clipping_kwh": 5.4,
                "total_clipping_kwh": 1.4,
                "effective_kwh": 10,
                "raw_model_kwh": 10,
            }.items():
                assert totals[key] == pytest.approx(hourly_value * hours)
            if selected == "tomorrow" and tomorrow_hours == 25:
                starts = {item["start"] for item in result["intervals"]}
                assert "2026-10-25T00:00:00+00:00" in starts
                assert "2026-10-25T01:00:00+00:00" in starts
            if zone == "Asia/Kathmandu":
                assert result["intervals"][0]["effective_kwh"] == 7.5
                assert result["intervals"][-1]["effective_kwh"] == 2.5
    assert snapshot.raw is raw
    assert snapshot.effective is effective
    assert snapshot.factor == 1.2


@pytest.mark.parametrize("problem", ["zero", "gap", "incomplete"])
def test_later_day_distinguishes_zero_from_missing_coverage(problem):
    """Ein belegter Nulltag bleibt erklärbar, fehlende UTC-Abdeckung nicht."""
    raw = basis({"a": 0}, forecast_days=7)
    target = raw.local_date + timedelta(days=5)
    start = datetime.combine(target, time.min, UTC)
    if problem != "zero":
        damaged = next(i for i in raw.total_intervals if i.start == start)
        raw = replace(
            raw,
            total_intervals=tuple(
                replace(i, is_complete=False) if i is damaged else i
                for i in raw.total_intervals
                if problem != "gap" or i is not damaged
            ),
        )
    snapshot, _, effective = explain(raw)
    now = start - timedelta(hours=23)
    result = explanation_view(
        snapshot, raw, effective, "UTC", now, now, True, day="tomorrow"
    )
    assert snapshot.reason is None
    if problem == "zero":
        assert result["status"] == "available"
        assert result["reason"] is None
        assert result["complete"] is True
        assert result["totals"]["effective_kwh"] == 0
        assert result["totals"]["effective_minus_raw_percent"] is None
    else:
        assert result["status"] == "unavailable"
        assert result["reason"] == "incomplete_coverage"
        assert result["complete"] is False
        assert "totals" not in result
        assert "intervals" not in result


@pytest.mark.parametrize("forecast_days", [2, 3, 7])
def test_day_outside_snapshot_stays_incomplete(forecast_days):
    """Ein abgelaufener Horizont erfindet keine Erklärung für den fehlenden Tag."""
    raw = basis(forecast_days=forecast_days)
    snapshot, _, effective = explain(raw)
    now = datetime.combine(
        raw.local_date + timedelta(days=forecast_days - 1), time(1), UTC
    )
    result = explanation_view(
        snapshot, raw, effective, "UTC", now, now, True, day="tomorrow"
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "incomplete_coverage"
    assert result["complete"] is False


@pytest.mark.parametrize(
    "selected,offset,stored_day",
    [("today", 0, "today"), ("tomorrow", 0, "tomorrow"), ("today", 1, "tomorrow")],
)
def test_existing_daily_total_remains_checked_against_interval_energy(
    selected, offset, stored_day
):
    """Widersprüchliche gespeicherte Tageskennzahlen bleiben ein echter Bilanzfehler."""
    raw = basis(forecast_days=7)
    effective = replace(
        raw,
        total=replace(raw.total, **{stored_day: getattr(raw.total, stored_day) + 1}),
    )
    snapshot = build_explanation(raw, effective, 1, None, "UTC")
    now = datetime.combine(raw.local_date + timedelta(days=offset), time(1), UTC)
    result = explanation_view(
        snapshot, raw, effective, "UTC", now, now, True, day=selected
    )
    assert snapshot.reason is None
    assert result["complete"] is True
    assert result["status"] == "unavailable"
    assert result["reason"] == "inconsistent_daily_total"
    assert "totals" not in result


def test_zero_fallback_restored_and_horizon_metadata():
    raw = basis({"a": 0})
    raw = replace(
        raw,
        horizon_shading=True,
        total_intervals=tuple(
            replace(i, quality_flags=("temperature_fallback",))
            for i in raw.total_intervals
        ),
    )
    snapshot, result, effective = explain(raw)
    assert result["totals"]["effective_minus_raw_percent"] is None
    assert result["quality_flags"] == ["temperature_fallback"]
    assert "horizon_profile" in result["assumptions"]
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    restored = explanation_view(
        snapshot,
        raw,
        effective,
        "UTC",
        now,
        now - timedelta(hours=3),
        False,
        origin="restored",
    )
    assert restored["origin"] == "restored" and restored["last_update_success"] is False
    assert restored["status"] == "available"


def test_missing_incompatible_and_nonfinite_basis_never_overwrites_forecast():
    raw = basis()
    assert build_explanation(None, raw, 1, None, "UTC").reason == "missing_raw_basis"
    inconsistent = replace(
        raw,
        total_intervals=(
            replace(raw.total_intervals[0], energy_kwh=99),
            *raw.total_intervals[1:],
        ),
    )
    assert (
        build_explanation(raw, inconsistent, 1, None, "UTC").reason
        == "incompatible_raw_basis"
    )
    snapshot, _, effective = explain(raw)
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    assert (
        explanation_view(snapshot, replace(raw), effective, "UTC", now, now, True)[
            "reason"
        ]
        == "incompatible_generation"
    )
    huge = basis({"a": 1e307}, limit=8)
    assert explain(huge, 1.5, 8)[1]["status"] == "unavailable"
    assert raw.total_intervals[0].energy_kwh == 10


@pytest.mark.parametrize(
    "zone,day,hours",
    [
        ("UTC", date(2026, 9, 10), 24),
        ("Europe/Berlin", date(2026, 3, 29), 23),
        ("Europe/Berlin", date(2026, 10, 25), 25),
        ("Asia/Kathmandu", date(2026, 9, 10), 24),
    ],
)
@pytest.mark.parametrize("morning_factor", [0.8, 1.2])
def test_morning_contribution_preserves_raw_global_and_both_clipping_stages(
    zone, day, hours, morning_factor
):
    """Geteilte Sonnenaufgangsgrenzen erhalten Tagesbilanz und Rohenergie."""
    timezone = ZoneInfo(zone)
    groups = (
        AcInverterGroup("g1", "Gruppe 1", 6, ("a", "b")),
        AcInverterGroup("g2", "Gruppe 2", 3, ("c",)),
    )
    raw = basis({"a": 4, "b": 4, "c": 4, "d": 2}, 10, groups, zone, day)
    windows = tuple(
        morning_window(day + timedelta(days=offset), timezone, 52.52, 13.41)
        for offset in range(raw.forecast_days)
    )
    effective = apply_morning(raw, morning_factor, 1.2, 10, timezone, 52.52, 13.41)
    snapshot = build_explanation(
        raw,
        effective,
        1.2,
        10,
        zone,
        morning_factor=morning_factor,
        morning_windows=windows,
    )
    assert snapshot.reason is None
    assert len(snapshot.intervals) == len(effective.total_intervals)
    assert len(effective.total_intervals) > len(raw.total_intervals)
    now = datetime.combine(day, time(12), timezone).astimezone(UTC)
    with patch(
        "custom_components.pv_forecast.explanation.apply_inverter_limits",
        side_effect=AssertionError("Lesen rechnet keine PV"),
    ):
        result = explanation_view(snapshot, raw, effective, zone, now, now, True)
    assert result["status"] == "available"
    assert result["morning_factor"] == morning_factor
    totals = result["totals"]
    assert totals["before_calibration_kwh"] == pytest.approx(14 * hours)
    assert totals["morning_delta_kwh"] == pytest.approx(14 * (morning_factor - 1) * 4)
    assert totals["calibration_delta_kwh"] == pytest.approx(
        (14 * hours + totals["morning_delta_kwh"]) * 0.2
    )
    assert totals["raw_model_kwh"] == pytest.approx(raw.total.today)
    assert sum(item["energy_kwh"] for item in result["raw_intervals"]) == pytest.approx(
        raw.total.today
    )
    assert totals["effective_kwh"] == pytest.approx(effective.total.today)
    assert totals["group_clipping_kwh"] > 0
    assert totals["total_clipping_kwh"] > 0
    for item in result["intervals"]:
        assert (
            item["before_calibration_kwh"]
            + item["morning_delta_kwh"]
            + item["calibration_delta_kwh"]
            - item["group_clipping_kwh"]
            - item["total_clipping_kwh"]
        ) == pytest.approx(item["effective_kwh"])


def test_disabled_morning_keeps_original_explanation_values_exact():
    """Faktor 1 verändert weder Originalintervalle noch bestehende Bilanzwerte."""
    raw = basis({"a": 2.718281828}, 3)
    prior = apply_calibration(raw, 1.13, 3, ZoneInfo("UTC"))
    effective = apply_morning(raw, 1, 1.13, 3, ZoneInfo("UTC"), 52.52, 13.41)
    assert effective == prior
    snapshot = build_explanation(raw, effective, 1.13, 3, "UTC")
    assert snapshot == build_explanation(
        raw, effective, 1.13, 3, "UTC", morning_factor=1, morning_windows=()
    )
    assert all(item.morning_delta_kwh == 0 for item in snapshot.intervals)


@pytest.mark.parametrize("problem", ["missing_windows", "wrong_factor", "raw_dc"])
def test_morning_explanation_rejects_a_different_factor_window_or_dc_basis(problem):
    """Eine korrekte Endsumme genügt bei anderer Modellgrundlage weiterhin nicht."""
    zone = ZoneInfo("UTC")
    raw = basis({"a": 1})
    effective = apply_morning(raw, 0.8, 1.1, None, zone, 52.52, 13.41)
    windows = tuple(
        morning_window(raw.local_date + timedelta(days=offset), zone, 52.52, 13.41)
        for offset in range(raw.forecast_days)
    )
    if problem == "raw_dc":
        altered = replace(
            effective.roofs["a"],
            intervals=tuple(
                replace(item, dc_power_kw=item.dc_power_kw + 0.1)
                for item in effective.roofs["a"].intervals
            ),
        )
        effective = replace(effective, roofs={"a": altered})
    snapshot = build_explanation(
        raw,
        effective,
        1.1,
        None,
        "UTC",
        morning_factor=0.9 if problem == "wrong_factor" else 0.8,
        morning_windows=() if problem == "missing_windows" else windows,
    )
    assert snapshot.reason == "incompatible_raw_basis"
