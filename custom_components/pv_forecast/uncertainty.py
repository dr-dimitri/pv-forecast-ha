"""Tages-Erfahrungsbänder aus rechtzeitig bekannten, getrennten Archivfällen."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, fsum, isfinite, sqrt
from typing import Any

from .history import MODEL_VERSION, ArchiveRecord, Assessment

RULE_VERSION = 1
TRAINING_DAYS = 60
VALIDATION_DAYS = 30
WINDOW_DAYS = 180
TARGET_COVERAGE = 0.8
MINIMUM_COVERAGE = 0.7
DAILY_HORIZONS = ("daily_previous_18", "daily_same_06")


@dataclass(frozen=True, slots=True)
class _Case:
    """Ein tatsächlich bekannter Messstand und seine ausgegebene Tagesprognose."""

    record: ArchiveRecord
    assessment: Assessment
    central: float

    @property
    def actual(self) -> float:
        return self.assessment.actual_energy_kwh


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Erfahrungsbänder benötigen eindeutige UTC-Zeitpunkte")
    return value.astimezone(UTC)


def _finite_energy(value: object) -> bool:
    try:
        return (
            not isinstance(value, bool)
            and isinstance(value, int | float)
            and isfinite(value)
            and value >= 0
        )
    except OverflowError:
        return False


def _sources(record: ArchiveRecord) -> tuple:
    return tuple(
        sorted(
            (source.source_id, source.measurement_identity)
            for source in record.measurement_sources
            if source.kind != "power"
            and source.confirmed_pv
            and source.confirmed_disjoint
        )
    )


def _assessment_at(record: ArchiveRecord, before: datetime) -> Assessment | None:
    """Keine spätere Messkorrektur in einen früheren Forecast zurückdatieren."""

    known = [
        item
        for item in (
            *record.assessment_revisions,
            *((record.assessment,) if record.assessment is not None else ()),
        )
        if record.end <= item.assessed_at <= before
    ]
    if not known:
        return None
    latest = max(known, key=lambda item: item.assessed_at)
    if not latest.valid or not _finite_energy(latest.actual_energy_kwh):
        return None
    return latest


def _bounded(
    central: float, residuals: tuple[float, float], maximum: float | None
) -> tuple[float, float]:
    bounds = tuple(max(0.0, central + residual) for residual in residuals)
    if maximum is not None:
        bounds = tuple(min(maximum, value) for value in bounds)
    if not all(isfinite(value) for value in bounds):
        raise ValueError("Das Erfahrungsband ist numerisch nicht darstellbar")
    return bounds


def _maximum(record: ArchiveRecord, power_limit: float | None) -> float | None:
    if power_limit is None:
        return None
    maximum = power_limit * (
        (_utc(record.end) - _utc(record.start)).total_seconds() / 3600
    )
    if not isfinite(maximum) or maximum <= 0:
        raise ValueError("Die bekannte AC-Energiegrenze ist nicht darstellbar")
    return maximum


def _score(lower: float, upper: float, actual: float) -> float:
    """Breite plus Fehlbetragsstrafe des Winkler-Scores bei Zielabdeckung 0,8."""

    return upper - lower + 10 * max(lower - actual, actual - upper, 0)


def _wilson(covered: int, count: int) -> dict[str, Any]:
    """Indikativer Wilson-Bereich, ohne Unabhängigkeit der Tage zu behaupten."""

    z = 1.959963984540054
    fraction = covered / count
    divisor = 1 + z * z / count
    center = (fraction + z * z / (2 * count)) / divisor
    half = (
        z
        * sqrt(fraction * (1 - fraction) / count + z * z / (4 * count * count))
        / divisor
    )
    return {
        "lower": max(0.0, center - half),
        "upper": min(1.0, center + half),
        "confidence_level": 0.95,
        "assumption": "independent_days",
        "indicative_only": True,
    }


def unavailable_band(reason: str) -> dict[str, Any]:
    """Ein begründeter Leerzustand enthält niemals ersatzweise Bandgrenzen."""

    return {
        "status": "unavailable",
        "label": "Bandbreite noch nicht belastbar",
        "reasons": [reason],
        "lower_kwh": None,
        "central_kwh": None,
        "upper_kwh": None,
        "training_count": 0,
        "validation_count": 0,
    }


def evaluate_experience_band(
    records: Sequence[ArchiveRecord],
    target: ArchiveRecord,
    *,
    as_of: datetime,
) -> dict[str, Any]:
    """Ein festes Tagesband mit 60 früheren und 30 späteren Fällen prüfen.

    Training, breite Referenz und Band bleiben während der ganzen Prüfung fest.
    Tagesresiduen werden nicht aus Stundenquantilen zusammengesetzt. Rohmodell
    und angewendete Kalibrierung bleiben getrennt; Faktor und Kandidaten-ID
    derselben Regelversion verändern die Vergleichsgruppe ausdrücklich nicht.
    """

    now = _utc(as_of)
    result = unavailable_band("insufficient_training_days")
    calibrated = target.calibrated_energy_kwh is not None
    result.update(
        rule_version=RULE_VERSION,
        target_date=target.target_date.isoformat(),
        horizon=target.horizon,
        cutoff=target.cutoff.isoformat(),
        forecast_observed_at=target.observed_at.isoformat(),
        variant="applied_calibration_rule_1" if calibrated else "raw_model",
        target_coverage=TARGET_COVERAGE,
        minimum_validation_coverage=MINIMUM_COVERAGE,
        minimum_training_days=TRAINING_DAYS,
        minimum_validation_days=VALIDATION_DAYS,
        window_days=WINDOW_DAYS,
        quality_flags=[
            "empirical_not_guaranteed",
            "seasonal_changes_possible",
            "coverage_uncertainty_assumes_independent_days",
        ],
    )
    if target.horizon not in DAILY_HORIZONS:
        result["reasons"] = ["unsupported_horizon"]
        return result
    if target.cutoff > now:
        result["reasons"] = ["forecast_not_frozen"]
        return result
    if target.model_version != MODEL_VERSION:
        result["reasons"] = ["unsupported_model_version"]
        return result
    if target.quality_flags or target.deleted_sources or not _sources(target):
        result["reasons"] = ["target_basis_invalid"]
        return result
    central = target.effective_energy_kwh
    if not _finite_energy(central):
        result["reasons"] = ["target_basis_invalid"]
        return result
    result["central_kwh"] = central
    # Auch eine heutige spätere Anfrage darf keine Zukunft in den damaligen
    # Ausgabezeitpunkt des hier dargestellten festen Prognosestands einbauen.
    known_before = min(now, _utc(target.observed_at))
    earliest_day = target.target_date - timedelta(days=WINDOW_DAYS)
    excluded: Counter[str] = Counter()
    compatible: list[ArchiveRecord] = []
    seen: set = set()
    for record in sorted(records, key=lambda item: (item.target_date, item.record_id)):
        if record.horizon != target.horizon:
            continue
        reason = None
        if (
            record.configuration_id != target.configuration_id
            or record.timezone != target.timezone
            or record.model_version != target.model_version
            or _sources(record) != _sources(target)
        ):
            reason = "incompatible_configuration_or_sources"
        elif record.target_date < earliest_day:
            reason = "outside_training_window"
        elif record.end > known_before:
            reason = "not_known_at_forecast"
        elif record.quality_flags or record.deleted_sources:
            reason = "invalid_basis"
        elif calibrated and record.calibrated_energy_kwh is None:
            reason = "different_forecast_method"
        elif record.target_date in seen:
            reason = "duplicate_target_day"
        if reason is not None:
            excluded[reason] += 1
            continue
        seen.add(record.target_date)
        compatible.append(record)

    def cases(before: datetime) -> list[_Case]:
        values = []
        for record in compatible:
            assessment = _assessment_at(record, before)
            prediction = (
                record.effective_energy_kwh if calibrated else record.raw_energy_kwh
            )
            if assessment is not None and _finite_energy(prediction):
                values.append(_Case(record, assessment, prediction))
        return values

    usable = cases(known_before)
    excluded["missing_timely_measurement"] += len(compatible) - len(usable)
    result["excluded_counts"] = {
        key: value for key, value in sorted(excluded.items()) if value
    }
    result["training_count"] = min(TRAINING_DAYS, len(usable))
    result["validation_count"] = min(
        VALIDATION_DAYS, max(0, len(usable) - TRAINING_DAYS)
    )
    if len(usable) < TRAINING_DAYS + VALIDATION_DAYS:
        if len(usable) >= TRAINING_DAYS:
            result["reasons"] = ["insufficient_validation_days"]
        return result
    validation = usable[-VALIDATION_DAYS:]
    training_cutoff = _utc(validation[0].record.observed_at)
    training = [
        case
        for case in cases(training_cutoff)
        if case.record.target_date < validation[0].record.target_date
    ][-TRAINING_DAYS:]
    result["training_count"] = len(training)
    if len(training) < TRAINING_DAYS:
        result["reasons"] = ["insufficient_timely_training_days"]
        return result

    residuals = sorted(case.actual - case.central for case in training)
    band = (
        residuals[ceil(0.1 * TRAINING_DAYS) - 1],
        residuals[ceil(0.9 * TRAINING_DAYS) - 1],
    )
    reference = (residuals[0], residuals[-1])
    limit = target.basis.inverter_max_power_kw if target.basis is not None else None
    if (
        target.basis is not None
        and target.basis.group_limits
        and not target.basis.has_ungrouped_roofs
    ):
        try:
            group_limit = fsum(limit for _, limit in target.basis.group_limits)
        except OverflowError:
            result["reasons"] = ["arithmetic_overflow"]
            return result
        limit = min(limit, group_limit) if limit is not None else group_limit
    if limit is not None and (not _finite_energy(limit) or limit <= 0):
        result["reasons"] = ["target_basis_invalid"]
        return result
    try:
        evaluation = _evaluate(validation, band, reference, limit)
        lower, upper = _bounded(central, band, _maximum(target, limit))
    except (ValueError, OverflowError):
        result["reasons"] = ["arithmetic_overflow"]
        return result
    result.update(
        training_period={
            "start": training[0].record.target_date.isoformat(),
            "end": training[-1].record.target_date.isoformat(),
            "measurements_known_before": training_cutoff.isoformat(),
        },
        validation_period={
            "start": validation[0].record.target_date.isoformat(),
            "end": validation[-1].record.target_date.isoformat(),
        },
        evaluation=evaluation,
        physical_maximum_kwh=_maximum(target, limit),
    )
    reasons = []
    if evaluation["coverage_fraction"] < MINIMUM_COVERAGE:
        reasons.append("coverage_below_threshold")
    score = evaluation["winkler_score_kwh"]
    reference_score = evaluation["reference_winkler_score_kwh"]
    if score > reference_score + 1e-9 * max(1, reference_score):
        reasons.append("interval_score_worse_than_reference")
    result["reasons"] = reasons
    if not reasons:
        result.update(
            status="available",
            label="Erfahrungsband",
            lower_kwh=lower,
            upper_kwh=upper,
        )
    return result


def _evaluate(
    cases: Sequence[_Case],
    band: tuple[float, float],
    reference: tuple[float, float],
    limit: float | None,
) -> dict[str, Any]:
    bounds = [
        _bounded(case.central, band, _maximum(case.record, limit)) for case in cases
    ]
    broad = [
        _bounded(case.central, reference, _maximum(case.record, limit))
        for case in cases
    ]
    covered = sum(
        lower <= case.actual <= upper
        for case, (lower, upper) in zip(cases, bounds, strict=True)
    )
    count = len(cases)
    values = {
        "coverage_fraction": covered / count,
        "mean_width_kwh": fsum((upper - lower) / count for lower, upper in bounds),
        "reference_mean_width_kwh": fsum(
            (upper - lower) / count for lower, upper in broad
        ),
        "winkler_score_kwh": fsum(
            _score(lower, upper, case.actual) / count
            for case, (lower, upper) in zip(cases, bounds, strict=True)
        ),
        "reference_winkler_score_kwh": fsum(
            _score(lower, upper, case.actual) / count
            for case, (lower, upper) in zip(cases, broad, strict=True)
        ),
    }
    if not all(isfinite(value) for value in values.values()):
        raise ValueError("Die Bandprüfung ist numerisch nicht darstellbar")
    return {
        **values,
        "count": count,
        "covered_count": covered,
        "coverage_wilson95": _wilson(covered, count),
        "physical_bounds_applied": True,
        "reference_method": "training_residual_min_max",
    }
