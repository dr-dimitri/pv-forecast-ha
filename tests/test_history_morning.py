"""Tatsächliche Morgenwirkung ohne Umdeutung der unveränderten Archivbasis."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pv_forecast.calculations import calibrated_energy
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.morning import MorningState
from custom_components.pv_forecast.short_term import build_trial
from custom_components.pv_forecast.uncertainty import evaluate_experience_band

from .test_history import DAY, SOURCE, capture_day, evidence, forecast, report_after
from .test_history_view import records as view_records
from .test_history_view import view
from .test_uncertainty import day_record, evaluate
from .test_uncertainty import records as band_records


def morning_forecast(factor=0.8):
    """Nach globalem Faktor 0,5 vier Morgenstunden mit eigener Wirkung bereitstellen."""
    baseline = forecast(DAY - timedelta(days=1), 2, dc_power=4)
    return replace(
        baseline,
        total_intervals=tuple(
            (
                replace(
                    interval,
                    energy_kwh=2 * factor,
                    ac_power_kw=2 * factor,
                )
                if 6 <= interval.start.hour < 10
                else interval
            )
            for interval in baseline.total_intervals
        ),
    )


def capture_morning(archive, **kwargs):
    return capture_day(
        archive,
        power=3,
        dc_power=4,
        inverter_max_power_kw=3,
        applied_factor=0.5,
        applied_candidate_id="global-geprueft",
        morning_forecast=morning_forecast(),
        morning_factor=0.8,
        morning_candidate_id="morgen-geprueft",
        **kwargs,
    )


def morning_state(source=SOURCE):
    """Ein tatsächlich am Vortag eingefrorenes Morgenprofil mit bestätigter Quelle."""
    now = datetime(2026, 9, 8, 18, tzinfo=UTC)
    state = MorningState("configuration-a", now - timedelta(hours=1))
    assert state.capture(
        forecast(DAY - timedelta(days=1), dc_power=1),
        now,
        now,
        (source,),
        latitude=52.52,
        longitude=13.41,
    )
    return state


def test_applied_morning_keeps_raw_basis_global_factor_and_exact_effective_energy():
    """Rohmodell, globaler Faktor und tatsächliche Tagesenergie bleiben getrennt."""
    archive = HistoryArchive("UTC")
    record = capture_morning(archive)
    assert record.raw_energy_kwh == 72
    assert record.calibrated_energy_kwh == 48
    assert record.applied_factor == 0.5
    assert calibrated_energy(record.basis, 1) == 72
    assert record.morning == {
        "factor": 0.8,
        "candidate_id": "morgen-geprueft",
        "energy_kwh": pytest.approx(46.4),
    }
    assert record.effective_energy_kwh == pytest.approx(46.4)
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert restored.to_dict() == archive.to_dict()
    assert restored.records[record.record_id].effective_energy_kwh == pytest.approx(
        46.4
    )


def test_applied_morning_report_compares_with_same_global_baseline():
    """Der Morgenvergleich beansprucht keine Wirkung des globalen Faktors."""
    archive = HistoryArchive("UTC")
    record = capture_morning(archive)
    archive.assess(record.record_id, evidence(record, 46.4), record.end)
    report = archive.snapshot(report_after(), 7)["horizons"]["daily_previous_18"]
    assert report["mae_kwh"] == pytest.approx(25.6)
    assert report["calibrated_comparison"]["calibrated_mae_kwh"] == pytest.approx(1.6)
    assert report["morning_comparison"] == {
        "method": "morning_rule_1",
        "count": 1,
        "baseline_mae_kwh": pytest.approx(1.6),
        "applied_mae_kwh": 0,
        "baseline_bias_kwh": pytest.approx(1.6),
        "applied_bias_kwh": 0,
    }


def test_morning_change_is_observed_locally_before_cutoff_and_frozen_afterwards():
    """Lokale Freigaben ändern Abrufzeit und nachträglich fällige Prognosen nicht."""
    archive = HistoryArchive("UTC")
    fetched = datetime(2026, 9, 8, 17, 30, tzinfo=UTC)
    raw = forecast(DAY - timedelta(days=1), 3, dc_power=4)
    baseline = {
        "inverter_max_power_kw": 3,
        "applied_factor": 0.5,
        "applied_candidate_id": "global-geprueft",
    }
    archive.capture(raw, fetched, fetched, "a", [SOURCE], **baseline)
    record = next(
        item for item in archive.records.values() if item.horizon == "daily_previous_18"
    )
    options = {
        **baseline,
        "morning_forecast": morning_forecast(),
        "morning_factor": 0.8,
        "morning_candidate_id": "morgen-geprueft",
    }
    observed = fetched + timedelta(minutes=15)
    archive.capture(raw, fetched, observed, "a", [SOURCE], **options)
    frozen = archive.records[record.record_id]
    assert frozen.morning is not None
    assert frozen.observed_at == observed
    assert frozen.fetched_at == fetched
    assert frozen.basis == record.basis
    archive.capture(
        raw,
        fetched,
        fetched + timedelta(minutes=31),
        "a",
        [SOURCE],
        **{**options, "morning_forecast": morning_forecast(0.9)},
    )
    assert archive.records[record.record_id] == frozen


def test_missing_morning_coverage_never_records_raw_energy_as_applied():
    """Eine unvollständige Wirkserie erzeugt keinen erfundenen Archivstand."""
    archive = HistoryArchive("UTC")
    now = datetime(2026, 9, 8, 18, tzinfo=UTC)
    archive.capture(
        forecast(DAY - timedelta(days=1), dc_power=1),
        now,
        now,
        "a",
        [SOURCE],
        morning_forecast=replace(morning_forecast(), total_intervals=()),
        morning_factor=0.8,
        morning_candidate_id="morgen-geprueft",
    )
    assert not archive.records


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"factor": 0.8, "candidate_id": "a"},
        {"factor": 0.8, "candidate_id": "a", "energy_kwh": -1},
        {"factor": 0.8, "candidate_id": "a", "energy_kwh": float("nan")},
        {"factor": 0.8, "candidate_id": "a", "energy_kwh": True},
        {"factor": 1.21, "candidate_id": "a", "energy_kwh": 1},
        {"factor": 0.8, "candidate_id": "", "energy_kwh": 1},
    ],
)
def test_invalid_stored_morning_effect_is_rejected(invalid):
    """Unvollständige, unendliche oder unzulässige Morgenwerte werden nicht geladen."""
    archive = HistoryArchive("UTC")
    capture_morning(archive)
    data = archive.to_dict()
    data["records"][0]["morning"] = invalid
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(data, "UTC")


def test_old_records_do_not_gain_reconstructed_morning_effect():
    """Ein voriger Archivvertrag bleibt ohne erfundenen Lern- oder Wirkstand lesbar."""
    archive = HistoryArchive("UTC")
    record = capture_day(archive)
    data = archive.to_dict()
    assert "morning" not in data
    for item in data["records"]:
        item.pop("morning")
    restored = HistoryArchive.from_dict(data, "UTC")
    assert restored.morning is None
    assert restored.records[record.record_id].morning is None
    assert restored.records[record.record_id].effective_energy_kwh == 24


def test_nested_morning_effect_does_not_modify_prepared_storage_snapshot():
    """Verschachtelte Morgenwerte bleiben von älteren Schreibgenerationen getrennt."""
    archive = HistoryArchive("UTC")
    record = capture_morning(archive)
    previous = archive.storage_snapshot()
    frozen = deepcopy(previous)
    record.morning["energy_kwh"] = 47
    assert archive.storage_snapshot() != previous
    assert previous == frozen
    public = record.to_dict()
    public["morning"]["energy_kwh"] = 1
    assert record.effective_energy_kwh == 47


def test_historical_view_uses_frozen_morning_energy_at_partial_midnight():
    """Eine lokale Teilstundengrenze teilt auch die damals wirksame Morgenenergie."""
    items, _, _ = view_records(zone="Asia/Kathmandu")
    items = [
        replace(
            record,
            morning={"factor": 0.8, "candidate_id": "a", "energy_kwh": 2.4},
        )
        for record in items
    ]
    result = view(items)
    assert sum(item["energy_kwh"] for item in result["intervals"]) == pytest.approx(
        24 * 2.4
    )
    assert result["intervals"][0]["morning"]["energy_kwh"] == pytest.approx(1.8)
    assert result["intervals"][0]["calibrated_energy_kwh"] == pytest.approx(2.25)


def test_morning_method_has_no_inherited_experience_band_or_short_term_candidate():
    """Ein globales Erfahrungsband belegt keine Sicherheit der Morgenmethode."""
    target = replace(
        day_record(92),
        morning={"factor": 0.8, "candidate_id": "a", "energy_kwh": 19},
    )
    result = evaluate_experience_band(band_records(), target, as_of=target.cutoff)
    assert result["status"] == "unavailable"
    assert result["variant"] == "applied_morning_rule_1"
    assert result["reasons"] == ["morning_method_not_validated"]
    assert result["lower_kwh"] is result["upper_kwh"] is None

    archive = HistoryArchive("UTC")
    record = capture_morning(archive)
    hourly = next(
        item for item in archive.records.values() if item.horizon == "hourly_1h"
    )
    assert build_trial(tuple(archive.records.values()), hourly, set())["reason"] == (
        "unsuitable_forecast_basis"
    )
    assert record.morning is not None


def test_morning_cases_are_not_learned_as_unchanged_global_calibration():
    """Die eigenen Morgenbelege ersetzen keine Stichprobe einer anderen Methode."""
    values = [
        replace(
            record,
            morning={"factor": 0.8, "candidate_id": "a", "energy_kwh": 19},
        )
        for record in band_records(factor=1.0)
    ]
    result = evaluate(values, target=day_record(92, factor=1.0))
    assert result["status"] == "unavailable"
    assert result["training_count"] == 0
    assert result["excluded_counts"]["different_forecast_method"] == len(values)


def test_optional_morning_state_roundtrip_does_not_mutate_prepared_metadata():
    """Morgenbelege behalten ihre eigene Zeitzone und Schreibgeneration."""
    archive = HistoryArchive("UTC", morning=morning_state())
    capture_day(archive)
    original = archive.storage_snapshot()
    frozen = deepcopy(original)
    restored = HistoryArchive.from_dict(archive.to_dict(), "Europe/Berlin")
    assert restored.morning.to_dict() == archive.morning.to_dict()
    assert restored.morning.timezone.key == "UTC"
    archive.morning.cases[DAY.isoformat()]["global_factor"] = 1.2
    assert archive.storage_snapshot() != original
    assert original == frozen


def test_morning_only_sources_keep_upstream_permissions_and_are_deleted():
    """Quellenrechte gelten auch ohne normalen Record für erhaltene Morgenprofile."""
    source = replace(
        SOURCE,
        derived_energy=True,
        upstream_entity_id="sensor.pv_power",
        upstream_registry_id="registry-pv-power",
        helper_entry_id="integral-helfer",
    )
    archive = HistoryArchive("UTC", morning=morning_state(source))
    assert archive.external_sources() == (
        {"entity_id": "sensor.pv", "registry_id": "registry-pv"},
        {"entity_id": "sensor.pv_power", "registry_id": "registry-pv-power"},
    )
    assert archive.delete_measurement_source(source.source_id)
    assert not archive.morning.cases
    assert archive.external_sources() == ()
    assert not archive.delete_measurement_source(source.source_id)


def test_archive_prunes_morning_profiles_and_counts_their_bytes():
    """Morgenprofile zählen zur Archivgröße und bleiben nicht länger als erlaubt."""
    archive = HistoryArchive("UTC", morning=morning_state())
    capture_day(archive)
    now = report_after()
    size = archive._storage_size()
    assert size > HistoryArchive("UTC")._storage_size() + 1000
    assert archive.prune(now, max_bytes=size - 500)
    assert archive._storage_size() <= size - 500
    assert archive.morning.cases
    assert archive.prune(now + timedelta(days=97))
    assert not archive.morning.cases
    assert archive.morning.retention_truncated


def test_unknown_morning_contract_does_not_load_as_empty_state():
    """Eine unbekannte Morgenregel verhindert das stille Überschreiben ihrer Belege."""
    archive = HistoryArchive("UTC", morning=morning_state())
    data = archive.to_dict()
    data["morning"]["rule_version"] = 999
    with pytest.raises(ValueError):
        HistoryArchive.from_dict(data, "UTC")
