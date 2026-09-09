"""Feste Stundenbänder nach Vorlauf, Ortsstunde und getrennten Tagen prüfen."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.uncertainty import evaluate_experience_band
from custom_components.pv_forecast.uncertainty_data import current_experience_bands

from .test_uncertainty import day_record
from .test_uncertainty_data import archive_with_records


def hour_record(index, *, lead=1, hour=12, fold=0, timezone="UTC"):
    """Ein bekannter Stundenfall mit einem kleinen, wiederkehrenden Residuum."""

    record = day_record(index, prediction=2, actual=2 + (index % 3 - 1) / 10)
    start = datetime.combine(
        record.target_date, time(hour, fold=fold), ZoneInfo(timezone)
    ).astimezone(UTC)
    end = start + timedelta(hours=1)
    cutoff = start - timedelta(hours=lead)
    return replace(
        record,
        record_id=f"hour-{index}-{lead}-{hour}-{fold}",
        start=start,
        end=end,
        cutoff=cutoff,
        fetched_at=cutoff,
        observed_at=cutoff,
        timezone=timezone,
        horizon=f"hourly_{lead}h",
        assessment=replace(record.assessment, assessed_at=end + timedelta(minutes=5)),
    )


@pytest.mark.parametrize("lead", [1, 3])
def test_hourly_band_uses_sixty_training_and_thirty_later_matching_days(lead):
    records = [hour_record(index, lead=lead) for index in range(90)]
    target = hour_record(90, lead=lead)
    result = evaluate_experience_band(records, target, as_of=target.cutoff)
    assert result["status"] == "available"
    assert result["rule_version"] == 2
    assert result["training_count"] == 60
    assert result["validation_count"] == 30
    assert (result["lower_kwh"], result["upper_kwh"]) == pytest.approx((1.9, 2.1))
    assert result["start"] == target.start.isoformat()
    assert result["end"] == target.end.isoformat()
    assert result["local_slot"] == [12, 0, 0]


def test_many_hours_from_a_few_days_do_not_replace_independent_later_days():
    records = [
        hour_record(index, hour=hour) for index in range(5) for hour in range(24)
    ]
    target = hour_record(90)
    result = evaluate_experience_band(records, target, as_of=target.cutoff)
    assert result["status"] == "unavailable"
    assert result["training_count"] == 5
    assert result["upper_kwh"] is None


def test_hourly_leads_and_daily_residuals_are_not_pooled():
    records = [hour_record(index, lead=3) for index in range(90)]
    records += [day_record(index) for index in range(90)]
    target = hour_record(90)
    result = evaluate_experience_band(records, target, as_of=target.cutoff)
    assert result["training_count"] == 0
    assert result["status"] == "unavailable"


def test_fold_hour_cannot_reuse_the_first_local_hour_sample():
    zone = ZoneInfo("Europe/Berlin")
    target = hour_record(297, hour=2, fold=1, timezone=zone.key)
    assert target.start.astimezone(zone).date() == date(2026, 10, 25)
    records = [hour_record(i, hour=2, timezone=zone.key) for i in range(207, 297)]
    result = evaluate_experience_band(records, target, as_of=target.cutoff)
    assert result["local_slot"] == [2, 0, 1]
    assert result["training_count"] == 0


def test_later_measurement_correction_is_not_used_for_hourly_training():
    records = [hour_record(index) for index in range(90)]
    target = hour_record(90)
    initial = records[0].assessment
    records[0] = replace(
        records[0],
        assessment=replace(initial, assessed_at=records[70].end, actual_energy_kwh=500),
        assessment_revisions=(initial,),
    )
    result = evaluate_experience_band(records, target, as_of=target.cutoff)
    assert result["status"] == "available"
    records[0] = replace(records[0], assessment_revisions=())
    result = evaluate_experience_band(records, target, as_of=target.cutoff)
    assert result["reasons"] == ["insufficient_timely_training_days"]


def test_view_exposes_only_frozen_future_hours_and_preserves_rolling_empty_states():
    target = hour_record(90)
    future_unfrozen = hour_record(90, hour=16)
    past = hour_record(90, hour=8)
    archive = archive_with_records(
        [*(hour_record(i) for i in range(90)), target, future_unfrozen, past]
    )
    before = archive.to_dict()
    result = current_experience_bands(archive, target.cutoff, "configuration-1")
    assert len(result["frozen_hours"]) == 1
    assert result["frozen_hours"][0]["status"] == "available"
    assert result["frozen_hours"][0]["central_kwh"] == target.raw_energy_kwh
    assert result["next_60_minutes"]["upper_kwh"] is None
    assert result["remaining_today"]["upper_kwh"] is None
    assert archive.to_dict() == before
    archive.prune(target.cutoff)
    assert (
        current_experience_bands(archive, target.cutoff, "configuration-1")[
            "frozen_hours"
        ][0]["status"]
        == "available"
    )


def test_partial_hour_has_no_hourly_band():
    target = hour_record(90)
    target = replace(target, end=target.end - timedelta(minutes=30))
    result = evaluate_experience_band([], target, as_of=target.cutoff)
    assert result["reasons"] == ["target_basis_invalid"]
