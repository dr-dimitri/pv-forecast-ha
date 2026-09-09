"""Offline-Prüfungen der vorab festgelegten Prognose- und Bewertungsstichtage."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast import history as history_module
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.measurements import SourceConfig, SourceHistory
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    TotalForecastInterval,
)

HOUR = timedelta(hours=1)
DAY = date(2026, 9, 9)
SOURCE = SourceConfig(
    "pv", "sensor.pv", "total", "Gesamte AC-PV ohne Speicher", "registry-pv"
)


def forecast(
    day: date = DAY, power: float = 1, timezone: str = "UTC"
) -> ForecastResult:
    """Zwei lokale Tage mit UTC-Stunden und begrenzten Randintervallen."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=2), time.min, zone).astimezone(UTC)
    cursor = start.replace(minute=0, second=0, microsecond=0)
    intervals = []
    while cursor < end:
        left, right = max(start, cursor), min(end, cursor + HOUR)
        intervals.append(
            TotalForecastInterval(
                left, right, power * (right - left).total_seconds() / 3600, power
            )
        )
        cursor += HOUR
    return ForecastResult(day, {}, DailyYield(0, 0), tuple(intervals))


def capture_day(
    archive: HistoryArchive, target: date = DAY, power: float = 1, **kwargs
):
    """Die Vortagsprognose am festen Stichtag erfassen."""
    observed = datetime.combine(
        target - timedelta(days=1), time(18), archive.timezone
    ).astimezone(UTC)
    archive.capture(
        forecast(target - timedelta(days=1), power, archive.timezone.key),
        observed,
        observed,
        "configuration-a",
        [SOURCE],
        **kwargs,
    )
    return next(
        record
        for record in archive.records.values()
        if record.target_date == target and record.horizon == "daily_previous_18"
    )


def evidence(record, amount: float = 1, source: SourceConfig = SOURCE):
    """Echte Zählerdifferenzen über dasselbe absolute Zielfenster verwenden."""
    history = SourceHistory(source, record.timezone, 1.7e308)
    history.add_reading(record.start, 0, "kWh")
    history.add_reading(record.end, amount, "kWh")
    return {
        "start": record.start.isoformat(),
        "end": record.end.isoformat(),
        "sources": [history.snapshot(record.start, record.end, record.end)],
    }


def report_after(target=DAY):
    return datetime.combine(target + timedelta(days=1), time(12), UTC)


def test_daily_latest_timely_forecast_is_frozen_after_cutoff() -> None:
    """Die am Abend bessere Prognose ersetzt keinen bereits fälligen Stichtag."""
    archive = HistoryArchive("UTC")
    observed = datetime(2026, 9, 8, 17, 30, tzinfo=UTC)
    archive.capture(
        forecast(DAY - timedelta(days=1), 1), observed, observed, "a", [SOURCE]
    )
    latest = datetime(2026, 9, 8, 18, tzinfo=UTC)
    archive.capture(forecast(DAY - timedelta(days=1), 2), latest, latest, "a", [SOURCE])
    record = next(
        item
        for item in archive.records.values()
        if item.horizon == "daily_previous_18" and item.target_date == DAY
    )
    late = latest + HOUR
    archive.capture(forecast(DAY - timedelta(days=1), 3), late, late, "a", [SOURCE])
    assert archive.records[record.record_id] == record
    assert record.raw_energy_kwh == 48
    assert record.fetched_at == latest
    with pytest.raises(FrozenInstanceError):
        record.raw_energy_kwh = 99


def test_delayed_replay_cannot_reopen_a_cutoff_already_observed_as_past() -> None:
    """Eine alte Beobachtung öffnet keinen bereits vergangenen Stichtag."""
    archive = HistoryArchive("UTC")
    first = datetime(2026, 9, 8, 17, tzinfo=UTC)
    archive.capture(forecast(DAY - timedelta(days=1), 1), first, first, "a", [SOURCE])
    record = next(
        item
        for item in archive.records.values()
        if item.horizon == "daily_previous_18" and item.target_date == DAY
    )
    later = first + 2 * HOUR
    archive.capture(forecast(DAY - timedelta(days=1), 3), later, later, "a", [SOURCE])
    replay = first + HOUR
    archive = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    archive.capture(forecast(DAY - timedelta(days=1), 2), replay, replay, "a", [SOURCE])
    assert archive.records[record.record_id].raw_energy_kwh == 24


def test_raw_retention_does_not_erase_a_valid_archived_assessment() -> None:
    """Weggefallene Rohpunkte widerlegen keine bereits archivierte Messung."""
    archive = HistoryArchive("UTC")
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    archive.capture(forecast(), now, now, "a", [SOURCE])
    record = next(
        item for item in archive.records.values() if item.horizon == "hourly_1h"
    )
    measured = SourceHistory(SOURCE, "UTC", 20)
    measured.add_reading(record.start, 0, "kWh")
    measured.add_reading(record.end, 12, "kWh")
    original = {
        "start": record.start.isoformat(),
        "end": record.end.isoformat(),
        "sources": [measured.snapshot(record.start, record.end, record.end)],
    }
    archive.assess(record.record_id, original, record.end)
    previous = archive.records[record.record_id].assessment
    measured.add_reading(record.end + HOUR, 13, "kWh")
    measured.prune(record.start, max_readings=2)
    expired = {
        **original,
        "sources": [measured.snapshot(record.start, record.end, record.end + HOUR)],
    }
    assert expired["sources"][0]["energy_kwh"] is None
    archive.assess(record.record_id, expired, record.end + HOUR)
    assert archive.records[record.record_id].assessment == previous


@pytest.mark.parametrize(
    ("fetched", "observed"),
    [
        (
            datetime(2026, 9, 8, 15, 59, tzinfo=UTC),
            datetime(2026, 9, 8, 18, tzinfo=UTC),
        ),
        (datetime(2026, 9, 8, 18, tzinfo=UTC), datetime(2026, 9, 8, 18, 1, tzinfo=UTC)),
    ],
)
def test_missing_or_late_capture_never_backfills_daily_history(
    fetched, observed
) -> None:
    """Abrufzeit allein reicht nicht: auch die Beobachtung muss rechtzeitig sein."""
    archive = HistoryArchive("UTC")
    archive.capture(forecast(DAY - timedelta(days=1)), fetched, observed, "a", [SOURCE])
    assert not any(
        item.horizon == "daily_previous_18" and item.target_date == DAY
        for item in archive.records.values()
    )


def test_hourly_horizons_are_selected_separately() -> None:
    """Eine und drei Stunden Vorlauf sind getrennte Vergleichsstichproben."""
    archive = HistoryArchive("UTC")
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    archive.capture(forecast(), now, now, "a", [SOURCE])
    one = [
        record for record in archive.records.values() if record.horizon == "hourly_1h"
    ]
    three = [
        record for record in archive.records.values() if record.horizon == "hourly_3h"
    ]
    assert {record.start.hour for record in one} == {9, 10}
    assert {record.start.hour for record in three} == {11, 12}
    assert all(record.cutoff == record.start - HOUR for record in one)
    assert all(record.cutoff == record.start - 3 * HOUR for record in three)


def test_hourly_partial_boundaries_do_not_create_rollover_duplicates() -> None:
    """Teilstundenzonen erzeugen beim Tageswechsel keine doppelten Stundenziele."""
    archive = HistoryArchive("Asia/Kolkata")
    now = datetime(2026, 9, 8, 17, tzinfo=UTC)
    archive.capture(
        forecast(DAY - timedelta(days=1), timezone="Asia/Kolkata"),
        now,
        now,
        "a",
        [SOURCE],
    )
    before = {
        record.record_id
        for record in archive.records.values()
        if record.horizon.startswith("hourly") and record.start.hour == 18
    }
    later = datetime(2026, 9, 8, 18, 30, tzinfo=UTC)
    archive.capture(forecast(DAY, timezone="Asia/Kolkata"), later, later, "a", [SOURCE])
    assert before
    assert before <= archive.records.keys()
    assert all(
        record.start.minute == 0 and record.end - record.start == HOUR
        for record in archive.records.values()
        if record.horizon.startswith("hourly")
    )


def test_daily_forecast_uses_local_fractional_boundaries() -> None:
    """Die Tagesprognose verwendet dieselben anteiligen UTC-Grenzen wie die Prognose."""
    archive = HistoryArchive("Asia/Kolkata")
    record = capture_day(archive)
    assert record.start.hour == 18 and record.start.minute == 30
    assert record.raw_energy_kwh == 24


@pytest.mark.parametrize(
    ("target", "hours", "expected"),
    [(date(2026, 3, 29), 23, 167), (date(2026, 10, 25), 25, 169)],
)
def test_dst_day_and_expected_week_keep_absolute_hours(target, hours, expected) -> None:
    """DST verändert Tagesenergie und erwartete Stichprobe um die reale Stunde."""
    archive = HistoryArchive("Europe/Berlin")
    record = capture_day(archive, target)
    assert (record.end - record.start).total_seconds() == hours * 3600
    assert record.raw_energy_kwh == hours
    result = archive.snapshot(report_after(target), 7)
    assert result["horizons"]["hourly_1h"]["count_expected"] == expected
    assert result["horizons"]["daily_previous_18"]["count_expected"] == 7


def test_incomplete_forecast_is_not_archived_as_complete_daily_value() -> None:
    """Eine fehlende Prognosestunde ist kein stiller Nullbeitrag."""
    archive = HistoryArchive("UTC")
    now = datetime(2026, 9, 8, 18, tzinfo=UTC)
    original = forecast(DAY - timedelta(days=1))
    broken = replace(original, total_intervals=original.total_intervals[:-1])
    archive.capture(broken, now, now, "a", [SOURCE])
    assert not any(
        item.horizon == "daily_previous_18" and item.target_date == DAY
        for item in archive.records.values()
    )


def test_observed_zero_error_and_bias_use_kwh_not_percentage_accuracy() -> None:
    """Gültige Nullfehler und vorzeichenbehaftete Fehler werden ehrlich gezählt."""
    archive = HistoryArchive("UTC")
    first = capture_day(archive, DAY, 0)
    second = capture_day(archive, DAY + timedelta(days=1), 1)
    archive.assess(first.record_id, evidence(first, 0), first.end)
    archive.assess(second.record_id, evidence(second, 20), second.end)
    result = archive.snapshot(report_after(DAY + timedelta(days=1)), 7)["horizons"][
        "daily_previous_18"
    ]
    assert result["count_valid"] == 2
    assert result["coverage"] == 2 / 7
    assert result["mae_kwh"] == 2
    assert result["bias_kwh"] == 2
    assert result["calibrated_comparison"]["count"] == 0
    assert result["calibrated_comparison"]["calibrated_mae_kwh"] is None


def test_extreme_forecast_errors_remain_in_the_sample() -> None:
    """Ungewöhnlich große Fehler werden nicht zur Verbesserung der Kennzahl entfernt."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive, power=0)
    archive.assess(record.record_id, evidence(record, 1e308), record.end)
    result = archive.snapshot(report_after(), 7)["horizons"]["daily_previous_18"]
    assert result["count_valid"] == 1
    assert result["mae_kwh"] == 1e308
    assert result["bias_kwh"] == -1e308


def test_later_measurement_correction_is_a_bounded_revision() -> None:
    """Korrekturen revisionieren ausschließlich die datierte Bewertung."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    for amount in range(1, 6):
        measured = evidence(record, amount)
        archive.assess(record.record_id, measured, record.end + amount * HOUR)
    stored = archive.records[record.record_id]
    assert stored.raw_energy_kwh == record.raw_energy_kwh
    assert stored.assessment.actual_energy_kwh == 5
    assert [item.actual_energy_kwh for item in stored.assessment_revisions] == [2, 3, 4]
    assert archive.assess(record.record_id, measured, record.end + 6 * HOUR) is False


def test_new_source_does_not_score_an_old_forecast() -> None:
    """Eine Zuordnungs-ID überträgt Messungen nicht auf einen Ersatzsensor."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    changed = replace(SOURCE, registry_id="replacement")
    archive.assess(record.record_id, evidence(record, 1, changed), record.end)
    assert (
        "measurement_boundary_changed"
        in archive.records[record.record_id].assessment.reasons
    )
    assert archive.records[record.record_id].assessment.sources[0].energy_kwh is None


def test_stale_current_status_does_not_reject_proven_historical_energy() -> None:
    """Der aktuelle Sensorzustand entwertet keine belegte alte Zählerdifferenz."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    measured = evidence(record, 2)
    measured["sources"][0]["quality_flags"].append("stale")
    archive.assess(record.record_id, measured, record.end + 2 * HOUR)
    assert archive.records[record.record_id].assessment.valid is True


def test_unresolved_current_entity_does_not_erase_verified_historical_identity() -> (
    None
):
    """Historische Registrysegmente bleiben trotz später fehlender Entity belegbar."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    measured = evidence(record, 2)
    measured["sources"][0]["identity_unresolved"] = True
    archive.assess(record.record_id, measured, record.end)
    assert archive.records[record.record_id].assessment.valid is True
    assert archive.records[record.record_id].assessment.actual_energy_kwh == 2
    assert {
        "entity_id": SOURCE.entity_id,
        "registry_id": SOURCE.registry_id,
    } in archive.external_sources()


def test_daily_coarse_delta_is_valid_but_hourly_cutting_is_not() -> None:
    """Eine grobe Tagesdifferenz kann belegt sein, ohne Stunden zu erfinden."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    measured = evidence(record, 5)
    assert measured["sources"][0]["complete"] is False
    assert measured["sources"][0]["energy_complete"] is True
    archive.assess(record.record_id, measured, record.end)
    assert archive.records[record.record_id].assessment.valid is True
    measured["sources"][0]["energy_complete"] = False
    measured["sources"][0]["quality_flags"].append("daily_correction")
    archive.assess(record.record_id, measured, record.end + HOUR)
    assert archive.records[record.record_id].assessment.valid is False


def test_source_purge_removes_revisions_and_survives_restart() -> None:
    """Gelöschte Messkopien kommen beim nächsten Takt oder Reload nicht zurück."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    archive.assess(record.record_id, evidence(record, 1), record.end)
    archive.assess(record.record_id, evidence(record, 2), record.end + HOUR)
    assert archive.delete_measurement_source(SOURCE.source_id) is True
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert (
        restored.assess(record.record_id, evidence(record, 3), record.end + 2 * HOUR)
        is False
    )
    stored = restored.records[record.record_id]
    assert stored.assessment is None
    assert stored.assessment_revisions == ()
    assert stored.raw_energy_kwh == record.raw_energy_kwh
    assert stored.deleted_sources == (SOURCE.source_id,)
    assert restored.external_sources() == ()
    fresh = capture_day(restored, DAY + timedelta(days=1))
    restored.assess(fresh.record_id, evidence(fresh, 4), fresh.end)
    assert restored.records[fresh.record_id].assessment.valid is True


def test_physical_configuration_change_invalidates_only_affected_periods() -> None:
    """Vergangene vollständige Bewertungen bleiben beim späteren Umbau bestehen."""
    archive = HistoryArchive("UTC")
    old = capture_day(archive, DAY)
    future = capture_day(archive, DAY + timedelta(days=1))
    archive.assess(old.record_id, evidence(old, 1), old.end)
    archive.note_configuration("changed", future.start + HOUR)
    archive.assess(future.record_id, evidence(future, 2), future.end)
    assert archive.records[old.record_id].assessment.valid is True
    assert (
        "configuration_changed" in archive.records[future.record_id].assessment.reasons
    )


def test_external_comparison_uses_identical_valid_intervals_and_records_age() -> None:
    """Der Fremdvergleich verwendet nur die gemeinsame verfügbare Stichprobe."""
    archive = HistoryArchive("UTC")
    observed = datetime(2026, 9, 8, 18, tzinfo=UTC)
    comparison = {
        DAY.isoformat(): {
            "energy_kwh": 22,
            "entity_id": "sensor.foreign",
            "registry_id": "foreign",
            "scope": "Gleiche AC-PV-Anlage",
            "reported_at": (observed - HOUR).isoformat(),
            "observed_at": observed.isoformat(),
        }
    }
    first = capture_day(archive, DAY, 1, comparison=comparison)
    second = capture_day(archive, DAY + timedelta(days=1), 10)
    archive.assess(first.record_id, evidence(first, 20), first.end)
    archive.assess(second.record_id, evidence(second, 20), second.end)
    paired = archive.snapshot(report_after(DAY + timedelta(days=1)), 7)["horizons"][
        "daily_previous_18"
    ]["existing_comparison"]
    assert paired["count"] == 1
    assert paired["raw_mae_kwh"] == 4
    assert paired["existing_mae_kwh"] == 2
    assert paired["mean_own_age_seconds"] == 0
    assert paired["mean_existing_age_seconds"] == 3600
    assert {
        "entity_id": "sensor.foreign",
        "registry_id": "foreign",
    } in archive.external_sources()


def test_retention_count_and_bytes_remove_oldest_records_first() -> None:
    """Harte Grenzen sind sichtbar und verlieren zuerst die ältesten Zieldatensätze."""
    archive = HistoryArchive("UTC")
    capture_day(archive, DAY)
    capture_day(archive, DAY + timedelta(days=1))
    assert len(archive.records) > 1
    archive.prune(report_after(DAY + timedelta(days=1)), max_records=1)
    assert len(archive.records) == 1
    assert archive.retention_truncated is True
    archive.prune(report_after(DAY + timedelta(days=1)), max_bytes=500)
    assert len(json.dumps(archive.to_dict(), ensure_ascii=False).encode()) <= 500
    assert archive.records == {}


def test_byte_purge_serializes_the_complete_archive_at_most_twice(monkeypatch) -> None:
    """Viele entfernte Datensätze verursachen keine quadratische Vollserialisierung."""
    archive = HistoryArchive("UTC")
    for offset in range(12):
        capture_day(archive, DAY + timedelta(days=offset))
    complete_serializations = 0
    original_dumps = json.dumps

    def counted_dumps(value, *args, **kwargs):
        nonlocal complete_serializations
        if isinstance(value, dict) and "records" in value:
            complete_serializations += 1
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(history_module.json, "dumps", counted_dumps)
    archive.prune(report_after(DAY + timedelta(days=12)), max_bytes=500)
    assert archive.records == {}
    assert complete_serializations <= 2


def test_multiple_sources_are_summed_only_after_per_source_validation() -> None:
    """Ein gültiger Gesamtwert benötigt jeden ausdrücklich getrennten Erzeuger."""
    archive = HistoryArchive("UTC")
    second = replace(
        SOURCE, source_id="second", entity_id="sensor.second", registry_id="second"
    )
    observed = datetime(2026, 9, 8, 18, tzinfo=UTC)
    archive.capture(
        forecast(DAY - timedelta(days=1)), observed, observed, "a", [SOURCE, second]
    )
    record = next(
        item for item in archive.records.values() if item.horizon == "daily_previous_18"
    )
    first_evidence = evidence(record, 2)
    second_evidence = evidence(record, 3, second)
    first_evidence["sources"].extend(second_evidence["sources"])
    archive.assess(record.record_id, first_evidence, record.end)
    assert archive.records[record.record_id].assessment.actual_energy_kwh == 5
    assert len(archive.records[record.record_id].assessment.sources) == 2


def test_daily_and_hourly_retention_are_separate() -> None:
    """Tagesbewertungen bleiben nach dem Ablauf der Stundenaufbewahrung erhalten."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    archive.prune(record.end + timedelta(days=91))
    assert record.record_id in archive.records
    assert all(item.horizon.startswith("daily") for item in archive.records.values())
    archive.prune(record.end + timedelta(days=366))
    assert archive.records == {}


def test_retention_keeps_the_oldest_complete_local_reporting_day() -> None:
    """Der Mittagstakt schneidet den ältesten lokalen Berichtstag nicht halb ab."""
    archive = HistoryArchive("UTC")
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    archive.capture(forecast(), now, now, "a", [SOURCE])
    record = next(
        item for item in archive.records.values() if item.horizon == "hourly_1h"
    )
    archive.prune(datetime.combine(DAY + timedelta(days=90), time(12), UTC))
    assert record.record_id in archive.records


@pytest.mark.parametrize("days", [7, 30, 90])
def test_report_expected_count_includes_missing_initial_history(days) -> None:
    """Ein neues Archiv verkleinert den Erwartungsnenner nicht künstlich."""
    archive = HistoryArchive("UTC")
    report = archive.snapshot(report_after(), days)
    assert report["horizons"]["daily_previous_18"]["count_expected"] == days
    assert report["horizons"]["hourly_1h"]["count_expected"] == days * 24
    assert report["horizons"]["daily_previous_18"]["mae_kwh"] is None
    assert report["horizons"]["daily_previous_18"]["coverage"] == 0


def test_assessment_sum_and_all_revisions_survive_storage_roundtrip() -> None:
    """Messsumme, Stichtag und Revisionen behalten beim Neustart ihre Identität."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    archive.assess(record.record_id, evidence(record, 2), record.end)
    archive.assess(record.record_id, evidence(record, 3), record.end + HOUR)
    assert (
        HistoryArchive.from_dict(archive.to_dict(), "UTC").to_dict()
        == archive.to_dict()
    )
    stored = deepcopy(archive.to_dict())
    target = next(
        item for item in stored["records"] if item["record_id"] == record.record_id
    )
    target["assessment"]["actual_energy_kwh"] = 999
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(stored, "UTC")


@pytest.mark.parametrize(
    "corruption", ["duplicate", "cutoff", "nan", "timezone", "calibration", "revision"]
)
def test_corrupt_archive_is_rejected(corruption) -> None:
    """Beschädigte lokale Stichtage dürfen keine rückwirkende Prognose vortäuschen."""
    archive = HistoryArchive("UTC")
    capture_day(archive)
    stored = deepcopy(archive.to_dict())
    if corruption == "duplicate":
        stored["records"].append(stored["records"][0])
    elif corruption == "cutoff":
        stored["records"][0]["cutoff"] = "2030-01-01T00:00:00+00:00"
    elif corruption == "nan":
        stored["records"][0]["raw_energy_kwh"] = float("nan")
    elif corruption == "timezone":
        stored["timezone"] = "Europe/Berlin"
    elif corruption == "calibration":
        stored["records"][0]["calibrated_energy_kwh"] = 1
    else:
        stored["records"][0]["assessment_revisions"] = [{"valid": True}]
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(stored, "UTC")
