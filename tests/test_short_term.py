"""Prospektive Kandidaten, begrenzte Wirkung und gleiche spätere Messpaare."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from custom_components.pv_forecast.history import (
    ArchiveRecord,
    Assessment,
    HistoryArchive,
    SourceAssessment,
    _record_id,
)
from custom_components.pv_forecast.models import (
    ForecastBasisInterval,
    ForecastCalibrationBasis,
)
from custom_components.pv_forecast.short_term import (
    build_trial,
    trial_report,
    validate_trial,
)

from .test_history import SOURCE, forecast

DAY = date(2026, 9, 9)


def record(day=DAY, hour=13, *, lead=1, ratio=0.8, limit=None):
    start = datetime.combine(day, time(hour), UTC)
    end = start + timedelta(hours=1)
    cutoff = start - timedelta(hours=lead)
    horizon = f"hourly_{lead}h"
    return ArchiveRecord(
        _record_id(horizon, start, end),
        start,
        end,
        day,
        horizon,
        cutoff,
        cutoff,
        cutoff,
        "UTC",
        "configuration-a",
        (SOURCE,),
        min(2, limit) if limit else 2,
        (),
        assessment=Assessment(
            end + timedelta(minutes=1),
            2 * ratio,
            True,
            (),
            (SourceAssessment(SOURCE.source_id, 2 * ratio, ("segment",), ()),),
        ),
        basis=ForecastCalibrationBasis((ForecastBasisInterval(start, end, 2),), limit),
    )


def inputs(day=DAY, **kwargs):
    return [record(day, hour=hour, **kwargs) for hour in (8, 9, 10)]


def test_candidate_is_bounded_decays_and_keeps_original_forecast_untouched():
    target = record()
    original = target.to_dict()
    trial = build_trial(inputs(), target, set())
    assert trial["status"] == "recorded"
    assert trial["factor"] == pytest.approx(0.8)
    assert trial["energy_kwh"] == pytest.approx(1.7)
    assert validate_trial(trial) == trial
    assert target.to_dict() == original
    late = replace(record(hour=19), observed_at=target.observed_at)
    assert build_trial(inputs(), late, set())["energy_kwh"] == 2
    tomorrow = record(day=DAY + timedelta(days=1))
    tomorrow = replace(tomorrow, observed_at=target.observed_at)
    assert build_trial(inputs(), tomorrow, set())["reason"] == "outside_current_day"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("gap", "insufficient_recent_measurements"),
        ("stale", "insufficient_recent_measurements"),
        ("later_assessment", "insufficient_recent_measurements"),
        ("source", "insufficient_recent_measurements"),
        ("reset", "insufficient_recent_measurements"),
        ("near_zero", "insufficient_reference_energy"),
        ("extreme", "unclear_extreme"),
        ("excluded", "unsuitable_forecast_basis"),
    ],
)
def test_unsafe_measurements_never_create_a_candidate(change, reason):
    values, target, excluded = inputs(), record(), set()
    if change == "gap":
        values.pop(1)
    elif change == "stale":
        target = record(hour=16)
    elif change == "later_assessment":
        values[0] = replace(
            values[0], assessment=replace(values[0].assessment, assessed_at=target.end)
        )
    elif change == "source":
        values[0] = replace(
            values[0], measurement_sources=(replace(SOURCE, registry_id="different"),)
        )
    elif change == "reset":
        values[0] = replace(
            values[0],
            assessment=replace(
                values[0].assessment,
                sources=(
                    replace(
                        values[0].assessment.sources[0],
                        quality_flags=("counter_reset",),
                    ),
                ),
            ),
        )
    elif change == "near_zero":
        values = [replace(item, raw_energy_kwh=0.01) for item in values]
    elif change == "extreme":
        values = inputs(ratio=0.1)
    elif change == "excluded":
        excluded = {DAY}
    assert build_trial(values, target, excluded)["reason"] == reason


def test_short_factor_precedes_group_and_total_clipping_and_preserves_calibration():
    target = record(limit=1.5)
    target = replace(
        target,
        basis=replace(
            target.basis,
            group_limits=(("device", 1.2),),
            intervals=(
                replace(
                    target.basis.intervals[0],
                    group_dc_power_kw=(2,),
                    ungrouped_dc_power_kw=0,
                ),
            ),
        ),
        applied_factor=1.1,
    )
    trial = build_trial(inputs(ratio=1.2), target, set())
    assert trial["status"] == "recorded"
    assert trial["energy_kwh"] == 1.2
    assert target.applied_factor == 1.1


def test_noon_window_integrates_the_fading_effect_without_touching_past_energy():
    target = record(hour=12)
    end = datetime.combine(DAY + timedelta(days=1), time.min, UTC)
    target = replace(
        target,
        horizon="daily_remaining_12",
        cutoff=target.start,
        fetched_at=target.start,
        observed_at=target.start,
        end=end,
        raw_energy_kwh=24,
        basis=ForecastCalibrationBasis(
            tuple(
                ForecastBasisInterval(
                    target.start + timedelta(hours=i),
                    target.start + timedelta(hours=i + 1),
                    2,
                )
                for i in range(12)
            ),
            None,
        ),
    )
    trial = build_trial(inputs(), target, set())
    assert trial["energy_kwh"] == pytest.approx(22.8)


def test_report_requires_thirty_completed_days_and_never_applies_a_factor():
    values = []
    for offset in range(30):
        day = DAY - timedelta(days=offset)
        recent = inputs(day)
        target = record(day)
        trial = build_trial(recent, target, set())
        target = replace(
            target,
            short_term=trial,
            assessment=replace(
                target.assessment, actual_energy_kwh=trial["energy_kwh"]
            ),
        )
        values.extend([*recent, target])
    now = datetime.combine(DAY + timedelta(days=1), time.min, UTC)
    result = trial_report(values, now, "configuration-a", set())
    horizon = result["horizons"]["hourly_1h"]
    assert horizon["days"] == 30
    assert horizon["criterion_met"] is True
    assert horizon["candidate_mae_kwh"] == 0
    assert horizon["baseline_mae_kwh"] == pytest.approx(0.3)
    assert result["applied"] is False
    values[0] = replace(
        values[0], assessment=replace(values[0].assessment, actual_energy_kwh=1)
    )
    result = trial_report(values, now, "configuration-a", set())
    assert result["horizons"]["hourly_1h"]["days"] == 29
    assert result["horizons"]["hourly_1h"]["criterion_met"] is False
    assert (
        result["horizons"]["hourly_1h"]["exclusions"]["changed_or_missing_evidence"]
        == 1
    )


def test_archive_freezes_candidates_and_noon_window_only_after_explicit_opt_in():
    archive = HistoryArchive("UTC")
    archive.records = {item.record_id: item for item in inputs()}
    now = datetime.combine(DAY, time(12), UTC)
    data = forecast(DAY, power=2, dc_power=2)
    archive.capture(data, now, now, "configuration-a", [SOURCE])
    assert not any(item.short_term for item in archive.records.values())
    assert not any(
        item.horizon == "daily_remaining_12" for item in archive.records.values()
    )
    archive.capture(
        data, now, now, "configuration-a", [SOURCE], short_term_enabled=True
    )
    noon = next(
        item
        for item in archive.records.values()
        if item.horizon == "daily_remaining_12" and item.target_date == DAY
    )
    assert noon.short_term["energy_kwh"] == pytest.approx(22.8)
    assert noon.start == now
    before = deepcopy(noon.to_dict())
    archive.capture(
        forecast(DAY, power=3, dc_power=3),
        now + timedelta(minutes=1),
        now + timedelta(minutes=1),
        "configuration-a",
        [SOURCE],
        short_term_enabled=True,
    )
    assert archive.records[noon.record_id].to_dict() == before
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert restored.records[noon.record_id].short_term == noon.short_term
    restored.delete_measurement_source(SOURCE.source_id)
    assert all(item.short_term is None for item in restored.records.values())


@pytest.mark.parametrize("mutation", ["version", "nan", "evidence"])
def test_corrupted_trial_data_is_rejected(mutation):
    trial = build_trial(inputs(), record(), set())
    if mutation == "version":
        trial["rule_version"] = 99
    elif mutation == "nan":
        trial["energy_kwh"] = float("nan")
    else:
        trial["evidence"].pop()
    with pytest.raises(ValueError):
        validate_trial(trial)


@pytest.mark.parametrize("zone", ["Europe/Berlin", "Asia/Kolkata"])
def test_noon_capture_keeps_local_bounds_and_requires_timely_forecast(zone):
    from zoneinfo import ZoneInfo

    archive = HistoryArchive(zone)
    day = date(2026, 10, 25)
    noon = datetime.combine(day, time(12), ZoneInfo(zone)).astimezone(UTC)
    archive.capture(
        forecast(day, timezone=zone, dc_power=1),
        noon,
        noon,
        "configuration-a",
        [SOURCE],
        short_term_enabled=True,
    )
    target = next(
        item
        for item in archive.records.values()
        if item.horizon == "daily_remaining_12" and item.target_date == day
    )
    assert target.start == noon
    assert target.end.astimezone(ZoneInfo(zone)).hour == 0
    assert (
        HistoryArchive.from_dict(archive.to_dict(), zone).records[target.record_id]
        == target
    )
    late = HistoryArchive(zone)
    late.capture(
        forecast(day, timezone=zone),
        noon,
        noon + timedelta(seconds=1),
        "configuration-a",
        [SOURCE],
        short_term_enabled=True,
    )
    assert not any(
        item.horizon == "daily_remaining_12" and item.target_date == day
        for item in late.records.values()
    )
