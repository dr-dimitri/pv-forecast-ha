"""Einheitliche Banddaten aus tatsächlichen festen Tagesständen ohne Live-Transfer."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.uncertainty_data import current_experience_bands

from .test_uncertainty import day_record, records


def archive_with_records(values) -> HistoryArchive:
    archive = HistoryArchive("UTC")
    archive.records = {record.record_id: record for record in values}
    return archive


def test_missing_current_forecast_is_explicit_and_does_not_gain_arbitrary_bounds():
    archive = archive_with_records(records())
    result = current_experience_bands(
        archive, datetime(2026, 4, 4, 17, tzinfo=UTC), "configuration-1"
    )
    assert result["days"]["tomorrow"]["reasons"] == ["no_frozen_forecast"]
    assert result["days"]["tomorrow"]["upper_kwh"] is None
    assert result["remaining_today"]["reasons"] == ["unsupported_horizon"]
    assert result["next_60_minutes"]["reasons"] == ["unsupported_horizon"]


def test_tomorrow_band_uses_its_frozen_prediction_instead_of_a_live_point_value():
    target = day_record(92, prediction=21, actual=None)
    archive = archive_with_records([*records(), target])
    before = archive.to_dict()
    result = current_experience_bands(archive, target.cutoff, "configuration-1")
    day = result["days"]["tomorrow"]
    assert day["status"] == "available"
    assert day["horizon"] == "daily_previous_18"
    assert day["central_kwh"] == 21
    assert (day["lower_kwh"], day["upper_kwh"]) == (19, 23)
    assert day["cutoff"] == target.cutoff.isoformat()
    assert result["basis"] == "frozen_daily_forecast"
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert archive.to_dict() == before


def test_today_switches_at_six_to_its_own_fixed_horizon():
    previous = day_record(92, actual=None)
    same = day_record(92, horizon="daily_same_06", actual=None)
    archive = archive_with_records(
        [*records(), *records(horizon="daily_same_06"), previous, same]
    )
    before = current_experience_bands(
        archive, same.cutoff - timedelta(seconds=1), "configuration-1"
    )
    after = current_experience_bands(archive, same.cutoff, "configuration-1")
    assert before["days"]["today"]["horizon"] == "daily_previous_18"
    assert after["days"]["today"]["horizon"] == "daily_same_06"
    assert after["days"]["today"]["status"] == "available"


def test_a_changed_plant_cannot_reuse_an_old_frozen_central_value():
    target = day_record(92, actual=None)
    archive = archive_with_records([*records(), target])
    result = current_experience_bands(archive, target.cutoff, "changed-plant")
    assert result["days"]["tomorrow"]["reasons"] == ["no_frozen_forecast"]
    assert result["days"]["tomorrow"]["central_kwh"] is None


def test_current_invalid_basis_and_archive_truncation_are_visible():
    target = replace(day_record(92), quality_flags=("missing_gti",))
    archive = archive_with_records([*records(), target])
    archive.retention_truncated = True
    result = current_experience_bands(archive, target.cutoff, "configuration-1")
    assert result["retention_truncated"] is True
    assert result["days"]["tomorrow"]["reasons"] == ["target_basis_invalid"]
    assert result["days"]["tomorrow"]["upper_kwh"] is None


def test_naive_view_timestamps_are_rejected():
    with pytest.raises(ValueError, match="eindeutigen Zeitpunkt"):
        current_experience_bands(HistoryArchive("UTC"), datetime(2026, 4, 4), "a")
