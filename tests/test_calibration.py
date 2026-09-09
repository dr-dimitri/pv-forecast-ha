"""Offline-Nachweise für getrennte Lerntage und tatsächlich spätere Prüfungen."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.calculations import (
    apply_calibration,
    calculate_forecast,
    calibrated_energy,
    fit_calibration_factor,
    forecast_basis,
)
from custom_components.pv_forecast.calibration import (
    CalibrationDay,
    CalibrationState,
    evaluate_calibration,
)
from custom_components.pv_forecast.models import (
    ForecastBasisInterval,
    ForecastCalibrationBasis,
    WeatherInterval,
)

from .helpers import TIMEZONE, roof, weather

SEGMENT = datetime(2026, 1, 1, tzinfo=UTC)
LEARNED = datetime(2026, 2, 1, tzinfo=UTC)


def basis(
    day: date, energy: float = 10, limit: float | None = None
) -> ForecastCalibrationBasis:
    start = datetime.combine(day, time.min, UTC)
    return ForecastCalibrationBasis(
        (ForecastBasisInterval(start, start + timedelta(days=1), energy / 24),), limit
    )


def day_record(day: date, actual: float = 8, candidate=None) -> CalibrationDay:
    raw_basis = basis(day)
    start = raw_basis.intervals[0].start
    return CalibrationDay(
        record_id=day.isoformat(),
        target_date=day,
        start=start,
        end=start + timedelta(days=1),
        cutoff=start - timedelta(hours=6),
        observed_at=start - timedelta(hours=7),
        configuration_id="plant",
        basis=raw_basis,
        raw_energy_kwh=10,
        actual_energy_kwh=actual,
        valid=True,
        quality_flags=(),
        evidence_fingerprint=f"{day}:revision-1",
        candidate_id=candidate.candidate_id if candidate else None,
        candidate_energy_kwh=(
            calibrated_energy(raw_basis, candidate.factor) if candidate else None
        ),
    )


def training_days() -> list[CalibrationDay]:
    return [
        day_record(date(2026, 1, 2) + timedelta(days=offset)) for offset in range(30)
    ]


def trained() -> tuple[CalibrationState, list[CalibrationDay]]:
    state = CalibrationState("plant", SEGMENT)
    days = training_days()
    state.update(days, LEARNED, "plant", SEGMENT)
    assert state.status == "testing"
    assert state.candidate.factor == 0.8
    return state, days


def approved() -> tuple[CalibrationState, list[CalibrationDay], datetime]:
    state, days = trained()
    days.extend(
        day_record(date(2026, 2, 2) + timedelta(days=offset), candidate=state.candidate)
        for offset in range(14)
    )
    now = datetime(2026, 2, 16, tzinfo=UTC)
    state.update(days, now, "plant", SEGMENT)
    assert state.status == "approved"
    return state, days, now


def test_fitting_finds_bounded_factor_and_prefers_one_for_ties() -> None:
    """Systematische Fehler korrigieren; ein gesättigtes Limit ist kein Lernbeleg."""
    raw = basis(date(2026, 1, 1), 10)
    assert fit_calibration_factor([(raw, 8)] * 30) == 0.8
    assert fit_calibration_factor([(raw, 12)] * 30) == 1.2
    assert fit_calibration_factor([(raw, 100)] * 30) == 1.5
    assert fit_calibration_factor([(raw, 0)] * 30) == 0.5
    capped = basis(date(2026, 1, 1), 240, 5)
    assert fit_calibration_factor([(capped, 120)] * 30) == 1.0


def test_factor_is_applied_before_limit_with_raw_dc_preserved() -> None:
    """10 kW vor 5-kW-Limit bleiben auch mit Faktor 0,8 bei 5 kW."""
    raw = calculate_forecast(
        (roof(),), {"roof_1": (weather(),)}, 5, date(2026, 8, 23), TIMEZONE
    )
    changed = apply_calibration(raw, 0.8, 5, TIMEZONE)
    assert changed.total.today == 5
    assert changed.roofs["roof_1"].intervals[0].dc_power_kw == 10
    assert changed.roofs["roof_1"].intervals[0].ac_power_kw == 5
    raw_basis = forecast_basis(raw, weather().start, weather().end, 5)
    assert calibrated_energy(raw_basis, 0.8) == 5
    assert ForecastCalibrationBasis.from_dict(raw_basis.to_dict()) == raw_basis


def test_factor_preserves_roof_shares_and_shared_series() -> None:
    """Alle Dächer erhalten denselben Faktor und dieselbe globale Begrenzung."""
    roofs = (roof("a", power=6), roof("b", power=4))
    raw = calculate_forecast(
        roofs, {"a": (weather(),), "b": (weather(),)}, 9, date(2026, 8, 23), TIMEZONE
    )
    changed = apply_calibration(raw, 1.2, 9, TIMEZONE)
    assert changed.total.today == pytest.approx(9)
    assert changed.roofs["a"].daily.today == pytest.approx(5.4)
    assert changed.roofs["b"].daily.today == pytest.approx(3.6)
    assert sum(item.energy_kwh for item in changed.total_intervals) == pytest.approx(9)
    assert apply_calibration(raw, 1.0, 9, TIMEZONE) is raw
    assert apply_calibration(changed, 1.2, 9, TIMEZONE) == changed


def test_forecast_basis_requires_actual_complete_roof_data() -> None:
    """Bestehende Tageswerte ohne Rohintervalle werden nicht rückwärts erfunden."""
    raw = calculate_forecast(
        (roof(),), {"roof_1": (weather(),)}, None, date(2026, 8, 23), TIMEZONE
    )
    point = weather()
    assert forecast_basis(replace(raw, roofs={}), point.start, point.end, None) is None
    assert (
        forecast_basis(raw, point.start - timedelta(hours=1), point.end, None) is None
    )
    reduced = forecast_basis(raw, point.start + timedelta(minutes=30), point.end, None)
    assert calibrated_energy(reduced, 0.8) == pytest.approx(4)


@pytest.mark.parametrize(
    ("day", "zone", "hours"),
    [
        (date(2026, 3, 29), "Europe/Berlin", 23),
        (date(2026, 10, 25), "Europe/Berlin", 25),
        (date(2026, 9, 10), "Asia/Kolkata", 24),
    ],
)
def test_calibration_keeps_actual_local_day_boundaries(day, zone, hours) -> None:
    """DST und halbe UTC-Randstunden bewahren ihre tatsächliche Tagesenergie."""

    timezone = ZoneInfo(zone)
    start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, timezone).astimezone(UTC)
    first = start.replace(minute=0)
    points = tuple(
        WeatherInterval(
            first + timedelta(hours=offset),
            first + timedelta(hours=offset + 1),
            1000,
            25,
        )
        for offset in range(hours + (1 if first < start else 0))
    )
    raw = calculate_forecast((roof(),), {"roof_1": points}, None, day, timezone)
    changed = apply_calibration(raw, 0.8, None, timezone)
    assert changed.total.today == pytest.approx(hours * 8)
    frozen = forecast_basis(raw, start, end, None)
    assert calibrated_energy(frozen, 0.8) == pytest.approx(changed.total.today)
    assert frozen.intervals[0].start == start
    assert frozen.intervals[-1].end == end


def test_calculate_forecast_accepts_same_optional_factor() -> None:
    """Ein Abruf und eine rein lokale Neuberechnung verwenden denselben Faktorpfad."""

    raw = calculate_forecast(
        (roof(),), {"roof_1": (weather(),)}, None, date(2026, 8, 23), TIMEZONE
    )
    actual = calculate_forecast(
        (roof(),),
        {"roof_1": (weather(),)},
        None,
        date(2026, 8, 23),
        TIMEZONE,
        calibration_factor=0.8,
    )
    assert actual == apply_calibration(raw, 0.8, None, TIMEZONE)


def test_training_waits_for_thirty_days_and_does_not_apply_candidate() -> None:
    """Anlauf ohne Historie und 29 Tage lassen das unveränderte Grundmodell gelten."""
    state = CalibrationState("plant", SEGMENT)
    state.update([], LEARNED, "plant", SEGMENT)
    assert state.approved_factor == 1
    assert state.snapshot()["training_days"] == 0
    state.update(training_days()[:29], LEARNED, "plant", SEGMENT)
    assert state.status == "learning"
    state.update(training_days(), LEARNED, "plant", SEGMENT)
    assert state.snapshot()["training_days"] == 30
    assert state.candidate_for_capture.factor == 0.8
    assert state.approved_factor == 1


def test_large_training_error_is_marked_but_not_removed() -> None:
    """Ein Extremtag bleibt Lerntag; absoluter Fehler begrenzt seinen Einfluss."""
    state = CalibrationState("plant", SEGMENT)
    days = training_days()
    days[-1] = replace(days[-1], actual_energy_kwh=200)
    state.update(days, LEARNED, "plant", SEGMENT)
    assert state.training_days == 30
    assert state.candidate.factor == 0.8
    assert "extreme_residual" in state.reasons


@pytest.mark.parametrize(
    "failure",
    [
        "missing_basis",
        "measurements_incomplete",
        "weather_quality",
        "strong_clipping",
        "insufficient_energy",
    ],
)
def test_unusable_training_day_is_excluded_with_reason(failure: str) -> None:
    """Messausfälle und unbrauchbare Modellleistung geben keinen Faktor frei."""
    days = training_days()
    changes = {
        "missing_basis": {"basis": None},
        "measurements_incomplete": {"valid": False, "actual_energy_kwh": None},
        "weather_quality": {"quality_flags": ("missing_gti",)},
        "strong_clipping": {"basis": basis(days[-1].target_date, 240, 5)},
        "insufficient_energy": {"basis": basis(days[-1].target_date, 0.01)},
    }
    days[-1] = replace(days[-1], **changes[failure])
    state = CalibrationState("plant", SEGMENT)
    state.update(days, LEARNED, "plant", SEGMENT)
    assert state.training_days == 29
    assert state.exclusion_reasons[failure] == 1


def test_old_or_reset_days_do_not_count_toward_training() -> None:
    """Alte Daten und ein bewusster Neubeginn verhindern stillen Datenübertrag."""
    state = CalibrationState("plant", SEGMENT)
    state.update(training_days(), LEARNED + timedelta(days=61), "plant", SEGMENT)
    assert state.training_days == 0
    state.update(training_days(), LEARNED + timedelta(days=62), "plant", LEARNED)
    assert state.training_days == 0
    assert state.candidate is None


def test_other_configuration_cannot_complete_or_duplicate_training_days() -> None:
    """Erhaltene Altkontexte vervollständigen keine aktive Lernstichprobe."""
    days = training_days()[:29]
    previous = replace(
        days[-1], record_id="previous-location", configuration_id="old-location"
    )
    state = CalibrationState("plant", SEGMENT)
    state.update([*days, previous], LEARNED, "plant", SEGMENT)
    assert state.training_days == 29
    assert state.candidate is None


def test_duplicate_day_in_active_configuration_remains_invalid() -> None:
    """Die Kontexttrennung darf tatsächlich doppelte aktive Tage nicht verstecken."""
    days = training_days()
    duplicate = replace(days[-1], record_id="duplicate-active-day")
    state = CalibrationState("plant", SEGMENT)
    with pytest.raises(ValueError, match="genau einen Vortagesstand"):
        state.update([*days, duplicate], LEARNED, "plant", SEGMENT)


def test_old_context_keeps_approval_but_cannot_replace_its_evidence() -> None:
    """Alte Tagesduplikate sind harmlos; fehlende aktive Belege entziehen Freigaben."""
    state, days, now = approved()
    previous = replace(
        days[0], record_id="previous-location", configuration_id="old-location"
    )
    state.update([*days, previous], now, "plant", SEGMENT)
    assert state.status == "approved"
    assert state.approved_factor == 0.8
    days[0] = replace(days[0], configuration_id="old-location")
    state.update([*days, previous], now + timedelta(minutes=1), "plant", SEGMENT)
    assert state.status == "invalidated"
    assert state.reasons == ["evidence_changed"]
    assert state.approved_factor == 1


def test_only_prospectively_frozen_candidate_days_can_validate() -> None:
    """Nachträglich berechnete Kandidatenwerte liefern keine Prüftage."""
    state, days = trained()
    days.extend(
        day_record(date(2026, 2, 2) + timedelta(days=offset)) for offset in range(14)
    )
    now = datetime(2026, 2, 16, tzinfo=UTC)
    state.update(days, now, "plant", SEGMENT)
    assert state.validation_days == 0
    assert state.approved_factor == 1
    candidate = state.candidate
    days[-1] = replace(
        days[-1],
        candidate_id=candidate.candidate_id,
        candidate_energy_kwh=8,
        observed_at=candidate.created_at - timedelta(minutes=1),
    )
    state.update(days, now, "plant", SEGMENT)
    assert state.validation_days == 0


def test_fourteen_new_days_validate_without_changing_trained_factor() -> None:
    """Nach echter späterer Beobachtung wird derselbe feste Kandidat freigegeben."""
    state, days, now = approved()
    assert state.approved_factor == 0.8
    assert state.candidate.created_at == LEARNED
    assert state.validation_days == 14
    assert state.snapshot()["improvement_percent"] == pytest.approx(100)
    restored = CalibrationState.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()
    restored.update(days, now, "plant", SEGMENT)
    assert restored.approved_factor == 0.8


def test_perfect_raw_model_and_worse_large_errors_prevent_approval() -> None:
    """Ein niedrigerer Durchschnitt darf die großen Fehler nicht verdecken."""
    assert not evaluate_calibration([10] * 14, [10] * 14, [10] * 14)["approved"]
    result = evaluate_calibration([10] * 14, [8] * 14, [8] * 12 + [14] * 2)
    assert result["calibrated_mae_kwh"] < result["raw_mae_kwh"]
    assert not result["approved"]
    assert result["reasons"] == ["large_errors_worse"]


@pytest.mark.parametrize(
    "change", ["correction", "delete", "curtailment", "sensor", "geometry"]
)
def test_relevant_evidence_change_retracts_approval(change: str) -> None:
    """Korrektur, Löschung, Wartung und eine neue Messgrenze entziehen den Faktor."""
    state, days, now = approved()
    excluded = ()
    config = "plant"
    if change == "correction":
        days[0] = replace(
            days[0], actual_energy_kwh=1, evidence_fingerprint="corrected"
        )
    elif change == "delete":
        days.pop(0)
    elif change == "curtailment":
        excluded = (days[0].target_date,)
    else:
        config = change
    state.update(days, now + timedelta(minutes=1), config, SEGMENT, excluded)
    assert state.approved_factor == 1
    assert state.candidate_for_capture is None


def test_validation_revision_is_also_a_dependency() -> None:
    """Auch eine geänderte Messung aus der Freigabeprüfung zieht den Faktor zurück."""
    state, days, now = approved()
    days[-1] = replace(days[-1], evidence_fingerprint="corrected-test")
    state.update(days, now + timedelta(minutes=1), "plant", SEGMENT)
    assert state.status == "invalidated"
    assert state.approved_factor == 1


def test_expired_test_waits_fourteen_days_before_new_candidate() -> None:
    """Weniger als 14 rechtzeitige Prüftage geben auch nach 28 Tagen nichts frei."""
    state, days = trained()
    expired = datetime(2026, 3, 2, tzinfo=UTC)
    state.update(days, expired, "plant", SEGMENT)
    assert state.status == "rejected"
    old_id = state.candidate.candidate_id
    state.update(days, expired + timedelta(days=13), "plant", SEGMENT)
    assert state.candidate.candidate_id == old_id
    assert state.candidate_for_capture is None
    state.update(days, expired + timedelta(days=14), "plant", SEGMENT)
    assert state.status == "learning"
    assert state.candidate is None


def test_rolling_review_retracts_factor_when_benefit_disappears() -> None:
    """Die letzten 14 Prüftage können eine bisher hilfreiche Korrektur entziehen."""
    state, days, now = approved()
    days.extend(
        day_record(
            date(2026, 2, 16) + timedelta(days=offset),
            actual=10,
            candidate=state.candidate,
        )
        for offset in range(14)
    )
    state.update(days, now + timedelta(days=14), "plant", SEGMENT)
    assert state.status == "rejected"
    assert state.approved_factor == 1
    assert "insufficient_improvement" in state.reasons


def test_rolling_review_requires_recent_measurements() -> None:
    """Ein alter Gütenachweis ersetzt keine anhaltende Messabdeckung."""
    state, days, now = approved()
    state.update(days, now + timedelta(days=28), "plant", SEGMENT)
    assert state.status == "invalidated"
    assert state.approved_factor == 1


def test_delayed_first_review_cannot_approve_expired_validation_days() -> None:
    """Nach einer Lernpause rechtfertigen alte Prüftage keine kurze Freigabe."""

    state, days = trained()
    days.extend(
        day_record(date(2026, 2, 2) + timedelta(days=offset), candidate=state.candidate)
        for offset in range(14)
    )
    resumed = datetime(2026, 4, 1, tzinfo=UTC)
    state.update(days, resumed, "plant", SEGMENT)
    assert state.status == "rejected"
    assert state.reasons == ["validation_expired"]
    assert state.validation_days == 0
    assert state.approved_factor == 1
    state.update(days, resumed + timedelta(seconds=1), "plant", SEGMENT)
    assert state.status == "rejected"
    assert state.approved_factor == 1


def test_unknown_learning_rule_is_not_restored() -> None:
    """Ein unbekannter Lernvertrag wird beim Laden kontrolliert abgewiesen."""
    state, _ = trained()
    stored = state.to_dict()
    stored["rule_version"] = 2
    with pytest.raises(ValueError, match="Unbekannte"):
        CalibrationState.from_dict(stored)
