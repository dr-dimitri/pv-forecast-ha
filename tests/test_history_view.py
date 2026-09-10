"""Historische Tage ohne kurzlebige Messdaten oder rückwirkende Modelländerung."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.history import ArchiveRecord, Assessment
from custom_components.pv_forecast.history_view import build_archive_day_view

from .test_history import SOURCE


def records(day="2026-08-10", zone="Europe/Berlin"):
    """Ganze UTC-Stunden einschließlich über Mitternacht reichender Ränder."""
    target = date.fromisoformat(day)
    start = datetime.combine(target, time.min, ZoneInfo(zone)).astimezone(UTC)
    end = datetime.combine(
        target + timedelta(days=1), time.min, ZoneInfo(zone)
    ).astimezone(UTC)
    cursor = start.replace(minute=0)
    result = []
    while cursor < end:
        right = cursor + timedelta(hours=1)
        result.append(
            ArchiveRecord(
                str(cursor),
                cursor,
                right,
                target,
                "hourly_1h",
                cursor - timedelta(hours=1),
                cursor - timedelta(hours=1, minutes=10),
                cursor - timedelta(hours=1),
                zone,
                "current",
                (SOURCE,),
                2,
                (),
                assessment=Assessment(end + timedelta(hours=1), 1, True, (), ()),
                calibrated_energy_kwh=3,
                applied_factor=1.5,
            )
        )
        cursor = right
    return result, start, end


def view(items, day="2026-08-10", **kwargs):
    return build_archive_day_view(
        items,
        datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=30),
        "current",
        items[0].timezone if items else "UTC",
        date=day,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("day", "zone", "count", "hours"),
    [
        ("2026-03-29", "Europe/Berlin", 23, 23),
        ("2026-10-25", "Europe/Berlin", 25, 25),
        ("2026-08-10", "Asia/Kathmandu", 25, 24),
    ],
)
def test_absolute_days_and_partial_measurements(day, zone, count, hours):
    """DST und Viertelstunden teilen nur Prognosen, niemals positive Messung."""
    items, start, end = records(day, zone)
    result = view(items, day)
    assert result["status"] == "complete"
    assert len(result["intervals"]) == count
    assert sum(i["energy_kwh"] for i in result["intervals"]) == 3 * hours
    assert result["start"] == start.isoformat()
    assert result["end"] == end.isoformat()
    assert result["daily_measurement"]["energy_kwh"] is None
    if zone == "Asia/Kathmandu":
        for item in (result["intervals"][0], result["intervals"][-1]):
            assert item["measurement"]["energy_kwh"] is None
            assert item["measurement"]["whole_interval_energy_kwh"] == 1
        items = [
            replace(i, assessment=replace(i.assessment, actual_energy_kwh=0))
            for i in items
        ]
        assert view(items, day)["coverage"]["measurement_fraction"] == 1
    else:
        assert result["coverage"]["measurement_fraction"] == 1


def test_revisions_deletion_horizons_and_daily_assessment():
    """30 Tage alte Belege behalten ihren Faktor; gelöschte Istwerte bleiben leer."""
    items, start, end = records()
    original = items[0]
    corrected = replace(
        original,
        assessment=replace(original.assessment, actual_energy_kwh=4),
        assessment_revisions=(original.assessment,),
    )
    daily = replace(
        original,
        record_id="daily",
        start=start,
        end=end,
        horizon="daily_previous_18",
        raw_energy_kwh=48,
        calibrated_energy_kwh=72,
        assessment=replace(original.assessment, actual_energy_kwh=20),
    )
    result = view(
        [
            corrected,
            *items[1:],
            daily,
            replace(original, record_id="3h", horizon="hourly_3h"),
        ]
    )
    first = result["intervals"][0]
    assert first["raw_energy_kwh"] == 2 and first["energy_kwh"] == 3
    assert first["measurement"]["energy_kwh"] == 4
    assert first["measurement"]["previous_revision_count"] == 1
    assert result["daily_measurement"]["energy_kwh"] == 20
    deleted = view([replace(corrected, deleted_sources=(SOURCE.source_id,))])
    assert deleted["intervals"][0]["measurement"]["energy_kwh"] is None
    assert deleted["intervals"][0]["measurement"]["reasons"] == ["deleted_sources"]
    assert (
        len(
            view([*items, replace(original, horizon="hourly_3h")], horizon="hourly_3h")[
                "intervals"
            ]
        )
        == 1
    )


def test_contexts_are_explicit_and_keep_original_zone():
    current, _, _ = records()
    old, start, _ = records(zone="Asia/Kathmandu")
    old = [replace(i, configuration_id="old") for i in old]
    combined = [*current, *old]
    assert len(view(combined)["intervals"]) == 24
    selected = view(combined, configuration_id="old")
    assert selected["start"] == start.isoformat()
    assert selected["timezone"] == "Asia/Kathmandu"
    assert len(selected["contexts"]) == 2
    incompatible = replace(current[0], model_version="other")
    assert view([incompatible, *current[1:]])["reason"] == "ambiguous_context"


@pytest.mark.parametrize(
    "selection",
    [
        {"date": "2026-09-10"},
        {"date": "2026-01-01"},
        {"date": "2026-02-30"},
        {"date": "20260810"},
        {"date": "2026-08-10", "configuration_id": "missing"},
        {"date": "2026-08-10", "horizon": "daily_previous_18"},
    ],
)
def test_selection_rejected(selection):
    items, _, _ = records()
    with pytest.raises(ValueError):
        build_archive_day_view(
            items,
            datetime(2026, 9, 10, tzinfo=UTC),
            "current",
            "Europe/Berlin",
            **selection,
        )


def test_empty_gaps_retention_and_overlaps():
    assert view([])["status"] == "empty"
    items, _, _ = records()
    partial = view(items[:2], retention_truncated=True)
    assert partial["status"] == "partial" and partial["retention_truncated"]
    assert view([items[0], items[0]])["reason"] == "ambiguous_intervals"
