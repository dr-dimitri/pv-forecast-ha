"""Zeitliche Trennung, Methodenvergleich und vorsichtige Tagesbandfreigabe."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.history import (
    ArchiveRecord,
    Assessment,
    SourceAssessment,
)
from custom_components.pv_forecast.measurements import SourceConfig
from custom_components.pv_forecast.models import ForecastCalibrationBasis
from custom_components.pv_forecast.uncertainty import evaluate_experience_band

SOURCE = SourceConfig("pv", "sensor.pv", "total", "AC-Gesamtanlage", "registry-pv")


def day_record(
    index: int,
    *,
    prediction: float = 20,
    actual: float | None = 20,
    horizon: str = "daily_previous_18",
    timezone: str = "UTC",
    limit: float | None = None,
    factor: float | None = None,
    base_date: date = date(2026, 1, 1),
) -> ArchiveRecord:
    """Ein rechtzeitig bekannter, vollständiger Tagesbeleg mit stabiler Messgrenze."""

    day = base_date + timedelta(days=index)
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    cutoff = datetime.combine(
        day - timedelta(days=1) if horizon == "daily_previous_18" else day,
        time(18) if horizon == "daily_previous_18" else time(6),
        zone,
    ).astimezone(UTC)
    assessment = Assessment(
        end + timedelta(minutes=5),
        actual,
        actual is not None,
        () if actual is not None else ("measurement_incomplete",),
        (SourceAssessment("pv", actual, ("segment-pv",), ()),),
    )
    return ArchiveRecord(
        f"day-{index}-{horizon}",
        start,
        end,
        day,
        horizon,
        cutoff,
        cutoff,
        cutoff,
        timezone,
        "configuration-1",
        (SOURCE,),
        prediction,
        (),
        assessment=assessment,
        calibrated_energy_kwh=prediction * factor if factor is not None else None,
        applied_factor=factor,
        applied_candidate_id=f"candidate-{index}" if factor is not None else None,
        basis=ForecastCalibrationBasis((), limit) if limit is not None else None,
    )


def records(**kwargs) -> list[ArchiveRecord]:
    """60 Trainingstage, ein nötiger Bekanntheitsabstand und 30 spätere Prüftage."""

    return [
        day_record(
            index,
            actual=20
            + (
                (-10 if index % 20 == 0 else 10 if index % 20 == 19 else index % 5 - 2)
                if index < 60
                else index % 3 - 1
            ),
            **kwargs,
        )
        for index in range(91)
    ]


def evaluate(values, target=None, **kwargs):
    target = target or day_record(92, actual=None)
    return evaluate_experience_band(values, target, as_of=target.cutoff, **kwargs)


@pytest.mark.parametrize("count", [0, 1, 59, 60, 89, 90])
def test_insufficient_or_not_timely_history_never_invents_bounds(count):
    """60 abgeschlossene Tage ersetzen weder spätere Prüfungen noch Bekanntheit."""

    result = evaluate(records()[:count])
    assert result["status"] == "unavailable"
    assert result["label"] == "Bandbreite noch nicht belastbar"
    assert result["lower_kwh"] is result["upper_kwh"] is None


def test_validated_band_reports_width_coverage_and_sample_uncertainty():
    """Ein nutzbares Tagesband bleibt transparent über Stichprobe und Grenzen."""

    result = evaluate(records())
    assert result["status"] == "available"
    assert (result["lower_kwh"], result["central_kwh"], result["upper_kwh"]) == (
        18,
        20,
        22,
    )
    assert result["training_count"] == 60
    assert result["validation_count"] == 30
    metrics = result["evaluation"]
    assert metrics["coverage_fraction"] == 1
    assert metrics["mean_width_kwh"] == 4
    assert metrics["reference_mean_width_kwh"] == 20
    assert metrics["winkler_score_kwh"] < metrics["reference_winkler_score_kwh"]
    assert metrics["coverage_wilson95"]["lower"] == pytest.approx(0.8864866)
    assert metrics["coverage_wilson95"]["indicative_only"] is True
    assert "independent_days" in metrics["coverage_wilson95"]["assumption"]


def test_future_measurements_and_later_requests_do_not_retrain_an_issued_band():
    """Nach dem historischen Ausgabezeitpunkt gewonnene Residuen bleiben draußen."""

    values = records()
    target = day_record(92, actual=None)
    original = evaluate(values, target)
    later = evaluate_experience_band(
        [*values, *(day_record(index, actual=1000) for index in range(92, 130))],
        target,
        as_of=target.cutoff + timedelta(days=40),
    )
    for key in ("lower_kwh", "upper_kwh", "training_period", "evaluation"):
        assert later[key] == original[key]


def test_training_uses_the_measurement_revision_known_before_the_first_test():
    """Eine spätere Messkorrektur schreibt das feste Training nicht rückwirkend um."""

    values = records()
    original = evaluate(values)
    initial = values[0].assessment
    corrected = replace(
        initial,
        actual_energy_kwh=900,
        assessed_at=values[80].end,
    )
    values[0] = replace(
        values[0], assessment=corrected, assessment_revisions=(initial,)
    )
    after_correction = evaluate(values)
    assert after_correction["evaluation"] == original["evaluation"]
    assert after_correction["upper_kwh"] == original["upper_kwh"]
    values[0] = replace(values[0], assessment_revisions=())
    missing_revision = evaluate(values)
    assert missing_revision["status"] == "unavailable"
    assert missing_revision["reasons"] == ["insufficient_timely_training_days"]


@pytest.mark.parametrize("change", ["configuration", "source", "model", "timezone"])
def test_incompatible_plant_source_or_method_change_separates_the_basis(change):
    """Gleich große Energiezahlen legitimieren keine andere Anlagen- oder Messgrenze."""

    values = records()
    target = day_record(92)
    if change == "configuration":
        target = replace(target, configuration_id="different-plant")
    elif change == "source":
        target = replace(
            target,
            measurement_sources=(replace(SOURCE, registry_id="other-registry"),),
        )
    elif change == "model":
        values = [replace(value, model_version="older-model") for value in values]
    else:
        values = [replace(value, timezone="Europe/Berlin") for value in values]
    result = evaluate(values, target)
    assert result["status"] == "unavailable"
    assert result["training_count"] == 0


def test_registry_rename_and_daily_factor_versions_preserve_compatible_method():
    """Damals angewendete Faktoren gehören zur gleichen Kalibrierungsmethode."""

    values = []
    for record in records():
        factor = 1 + (record.target_date.day % 10) / 100
        values.append(
            replace(
                record,
                calibrated_energy_kwh=record.raw_energy_kwh * factor,
                applied_factor=factor,
                applied_candidate_id=f"different-candidate-{record.record_id}",
                assessment=replace(
                    record.assessment,
                    actual_energy_kwh=record.assessment.actual_energy_kwh
                    + record.raw_energy_kwh * (factor - 1),
                ),
            )
        )
    target = replace(
        day_record(92, factor=1.2),
        measurement_sources=(replace(SOURCE, entity_id="sensor.renamed_pv"),),
    )
    result = evaluate(values, target)
    assert result["status"] == "available"
    assert result["variant"] == "applied_calibration_rule_1"
    assert result["training_count"] == 60
    assert result["lower_kwh"] == pytest.approx(22)
    assert result["upper_kwh"] == pytest.approx(26)
    values[0] = replace(values[0], calibrated_energy_kwh=None)
    assert evaluate(values, target)["status"] == "unavailable"


def test_raw_variant_keeps_the_original_raw_model_values():
    """Die separat gespeicherten Rohprognosen bleiben auch bei Automatik nutzbar."""

    values = [
        replace(
            record,
            calibrated_energy_kwh=record.raw_energy_kwh * 1.5,
            applied_factor=1.5,
            applied_candidate_id="applied",
        )
        for record in records()
    ]
    result = evaluate(values)
    assert result["variant"] == "raw_model"
    assert result["status"] == "available"
    assert result["lower_kwh"] == 18


def test_seasonal_shift_and_large_errors_fail_without_excluding_bad_test_days():
    """Große Frühlingsfehler bleiben Teil der nachfolgenden Wintermodellprüfung."""

    values = records()
    for index in range(61, 91):
        values[index] = replace(
            values[index],
            assessment=replace(values[index].assessment, actual_energy_kwh=30),
        )
    result = evaluate(values)
    assert result["status"] == "unavailable"
    assert result["validation_count"] == 30
    assert result["evaluation"]["coverage_fraction"] == 0
    assert "coverage_below_threshold" in result["reasons"]
    assert result["lower_kwh"] is result["upper_kwh"] is None


def test_coverage_alone_cannot_pass_a_worse_interval_score():
    """70 Prozent Treffer genügen nicht bei unverhältnismäßigen Fehlbeträgen."""

    values = records()
    for index in range(61, 70):
        values[index] = replace(
            values[index],
            assessment=replace(values[index].assessment, actual_energy_kwh=29),
        )
    result = evaluate(values)
    assert result["evaluation"]["coverage_fraction"] == 0.7
    assert result["reasons"] == ["interval_score_worse_than_reference"]
    assert result["status"] == "unavailable"


def test_zero_production_is_valid_evidence_and_never_creates_negative_energy():
    """Vollständig belegte Nulltage werden nicht als Datenlücken aussortiert."""

    values = [day_record(index, prediction=0, actual=0) for index in range(91)]
    result = evaluate(values, day_record(92, prediction=0, actual=None))
    assert result["status"] == "available"
    assert result["lower_kwh"] == result["upper_kwh"] == 0
    assert result["evaluation"]["mean_width_kwh"] == 0
    assert result["evaluation"]["coverage_fraction"] == 1


@pytest.mark.parametrize("missing", ["measurement", "weather", "deleted_source"])
def test_incomplete_measurements_or_inputs_are_not_zero_residuals(missing):
    """Eine unvollständige Basis kann die Mindeststichprobe nicht künstlich füllen."""

    values = records()
    if missing == "measurement":
        values[0] = replace(values[0], assessment=None)
    elif missing == "weather":
        values[0] = replace(values[0], quality_flags=("missing_gti",))
    else:
        values[0] = replace(values[0], deleted_sources=("pv",))
    result = evaluate(values)
    assert result["status"] == "unavailable"


def test_upper_ac_bound_uses_actual_dst_day_duration():
    """Der 23-Stunden-Tag erhält sein tatsächliches bekanntes AC-Energielimit."""

    target = day_record(87, timezone="Europe/Berlin", prediction=23, limit=1)
    # Der Zieltag ist 29.03.2026; genügend davor abgeschlossene Tage bereitstellen.
    values = records(timezone="Europe/Berlin", limit=1, base_date=date(2025, 12, 22))
    result = evaluate(values, target)
    assert target.target_date == date(2026, 3, 29)
    assert result["status"] == "available"
    assert result["physical_maximum_kwh"] == 23
    assert result["upper_kwh"] == 23


def test_coverage_is_measured_after_physical_clipping():
    """Die Freigabe darf nicht mit den ungekürzten theoretischen Grenzen werben."""

    values = [
        replace(
            record,
            raw_energy_kwh=23,
            assessment=replace(
                record.assessment,
                actual_energy_kwh=(
                    (record.assessment.actual_energy_kwh + 3) if index < 61 else 25
                ),
            ),
        )
        for index, record in enumerate(records(limit=1))
    ]
    result = evaluate(values, day_record(92, prediction=23, limit=1))
    assert result["evaluation"]["physical_bounds_applied"] is True
    assert result["evaluation"]["coverage_fraction"] == 0
    assert result["status"] == "unavailable"


def test_different_horizons_are_never_pooled_or_summed():
    """Ein Tagesband kann nicht auf einen anderen Stichtag oder Restfenster wandern."""

    result = evaluate(records(), day_record(92, horizon="daily_same_06"))
    assert result["status"] == "unavailable"
    assert result["training_count"] == 0
    hourly = replace(day_record(92), horizon="hourly_1h")
    assert evaluate(records(), hourly)["reasons"] == ["unsupported_horizon"]


def test_same_day_horizon_has_its_own_complete_training_and_validation():
    """Die 06-Uhr-Stichprobe wird unabhängig von den Vortagesständen geprüft."""

    result = evaluate(
        records(horizon="daily_same_06"),
        day_record(92, horizon="daily_same_06"),
    )
    assert result["status"] == "available"
    assert result["horizon"] == "daily_same_06"


def test_stale_history_and_unfrozen_target_never_produce_an_available_band():
    result = evaluate(records(), day_record(280))
    assert result["status"] == "unavailable"
    target = day_record(92)
    result = evaluate_experience_band(
        records(), target, as_of=target.cutoff - timedelta(seconds=1)
    )
    assert result["reasons"] == ["forecast_not_frozen"]


def test_an_unknown_target_model_does_not_gain_current_method_assumptions():
    """Unbekannte Modellverträge erhalten kein Band der aktuell geprüften Methode."""

    target = replace(day_record(92), model_version="unknown")
    assert evaluate(records(), target)["reasons"] == ["unsupported_model_version"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_target_numbers_do_not_escape_into_service_json(value):
    target = replace(day_record(92), raw_energy_kwh=value)
    result = evaluate(records(), target)
    assert result["status"] == "unavailable"
    assert result["central_kwh"] is None
