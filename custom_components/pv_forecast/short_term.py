"""Vorab festgelegter, ausschließlich beobachtender Zukunftsvergleich."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import pairwise
from math import fsum, isfinite
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .calculations import calibrated_energy

if TYPE_CHECKING:
    from .history import ArchiveRecord

HORIZONS = ("hourly_1h", "hourly_3h", "daily_remaining_12")


def _sources(record: ArchiveRecord) -> tuple:
    return tuple(
        sorted(source.measurement_identity for source in record.measurement_sources)
    )


def evidence_fingerprint(record: ArchiveRecord) -> str:
    """Messkorrekturen und Änderungen der ursprünglichen Vergleichsbasis erkennen."""

    data = {
        "configuration": record.configuration_id,
        "energy": record.effective_energy_kwh,
        "assessment": record.assessment.to_dict() if record.assessment else None,
        "sources": _sources(record),
        "quality": record.quality_flags,
        "deleted": record.deleted_sources,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def build_trial(
    records: Sequence[ArchiveRecord], target: ArchiveRecord, excluded_dates: set[date]
) -> dict[str, Any]:
    """Nur zum Beobachtungszeitpunkt bekannte Vergangenheit als Eingabe zulassen."""

    now = target.observed_at
    result = {
        "rule_version": 1,
        "status": "unavailable",
        "reason": "insufficient_recent_measurements",
        "factor": None,
        "energy_kwh": None,
        "evidence": [],
    }
    today = now.astimezone(ZoneInfo(target.timezone)).date()
    if target.target_date != today or target.start < now:
        return {**result, "reason": "outside_current_day"}
    if (
        target.basis is None
        or target.morning is not None
        or target.quality_flags
        or target.target_date in excluded_dates
        or not target.measurement_sources
        or any(
            not source.confirmed_pv or not source.confirmed_disjoint
            for source in target.measurement_sources
        )
    ):
        return {**result, "reason": "unsuitable_forecast_basis"}
    eligible = sorted(
        (
            item
            for item in records
            if item.horizon == "hourly_1h"
            and now - timedelta(hours=5) <= item.end <= now
            and item.configuration_id == target.configuration_id
            and item.timezone == target.timezone
            and item.morning is None
            and _sources(item) == _sources(target)
            and item.target_date not in excluded_dates
            and not item.quality_flags
            and not item.deleted_sources
            and item.assessment is not None
            and item.assessment.valid
            and not any(source.quality_flags for source in item.assessment.sources)
            and item.end <= item.assessment.assessed_at <= now
            and item.end <= now
        ),
        key=lambda item: item.end,
    )
    recent = eligible[-3:]
    if (
        len(recent) != 3
        or now - recent[-1].end > timedelta(minutes=90)
        or any(left.end != right.start for left, right in pairwise(recent))
        or any(item.end - item.start != timedelta(hours=1) for item in recent)
    ):
        return result
    try:
        baseline = fsum(item.effective_energy_kwh for item in recent)
        actual = fsum(item.assessment.actual_energy_kwh for item in recent)
        if baseline < 0.3:
            return {**result, "reason": "insufficient_reference_energy"}
        ratio = actual / baseline
        if not isfinite(ratio) or not 0.5 <= ratio <= 1.5:
            return {**result, "reason": "unclear_extreme"}
        factor = min(1.2, max(0.8, ratio))
        energies = []
        for interval in target.basis.intervals:
            start_hours = max(0.0, (interval.start - now).total_seconds() / 3600)
            end_hours = max(0.0, (interval.end - now).total_seconds() / 3600)

            # Integral des linearen Abklingens, auch über die Sechsstundengrenze.
            def integral(hours: float) -> float:
                hours = min(6.0, hours)
                return hours - hours * hours / 12

            weight = (integral(end_hours) - integral(start_hours)) / (
                end_hours - start_hours
            )
            scale = 1 + (factor - 1) * weight
            scaled = replace(
                interval,
                dc_power_kw=interval.dc_power_kw * scale,
                group_dc_power_kw=tuple(
                    value * scale for value in interval.group_dc_power_kw
                ),
                ungrouped_dc_power_kw=(
                    interval.ungrouped_dc_power_kw * scale
                    if interval.ungrouped_dc_power_kw is not None
                    else None
                ),
            )
            energies.append(
                calibrated_energy(
                    replace(target.basis, intervals=(scaled,)),
                    target.applied_factor or 1.0,
                )
            )
        energy = fsum(energies)
        if not isfinite(energy):
            raise ValueError
    except (ValueError, OverflowError, ZeroDivisionError):
        return {**result, "reason": "arithmetic_overflow"}
    return {
        **result,
        "status": "recorded",
        "reason": None,
        "factor": factor,
        "energy_kwh": energy,
        "evidence": [[item.record_id, evidence_fingerprint(item)] for item in recent],
    }


def validate_trial(data: Any) -> dict[str, Any] | None:
    """Unbekannte oder beschädigte Versuchsdaten niemals als Kandidaten laden."""

    if data is None:
        return None
    if not isinstance(data, dict) or data.get("rule_version") != 1:
        raise ValueError("Unbekannter kurzfristiger Versuchsvertrag")
    if data.get("status") == "unavailable":
        if (
            not isinstance(data.get("reason"), str)
            or data.get("energy_kwh") is not None
            or data.get("factor") is not None
            or data.get("evidence") != []
        ):
            raise ValueError("Ungültiger leerer Versuchsstand")
        return data
    try:
        valid = (
            data.get("status") == "recorded"
            and data.get("reason") is None
            and type(data["factor"]) in (int, float)
            and 0.8 <= data["factor"] <= 1.2
            and type(data["energy_kwh"]) in (int, float)
            and isfinite(data["energy_kwh"])
            and data["energy_kwh"] >= 0
            and isinstance(data["evidence"], list)
            and len(data["evidence"]) == 3
            and all(
                isinstance(item, list)
                and len(item) == 2
                and all(
                    isinstance(value, str)
                    and len(value) == 64
                    and all(c in "0123456789abcdef" for c in value)
                    for value in item
                )
                for item in data["evidence"]
            )
            and len({item[0] for item in data["evidence"]}) == 3
        )
    except (KeyError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise ValueError("Ungültiger kurzfristiger Versuchsstand")
    return data


def trial_report(
    records: Sequence[ArchiveRecord],
    now: datetime,
    configuration_id: str,
    excluded_dates: set[date],
) -> dict[str, Any]:
    """Feste Kandidaten mit denselben späteren Messungen vergleichen."""

    by_id = {record.record_id: record for record in records}
    result = {
        "schema_version": 1,
        "rule_version": 1,
        "mode": "observe_only",
        "applied": False,
        "window_days": 60,
        "minimum_days": 30,
        "required_mae_improvement": 0.05,
        "horizons": {},
    }
    for horizon in HORIZONS:
        selected = [
            record
            for record in records
            if record.horizon == horizon
            and record.configuration_id == configuration_id
            and now.astimezone(ZoneInfo(record.timezone)).date() - timedelta(days=60)
            <= record.target_date
            < now.astimezone(ZoneInfo(record.timezone)).date()
        ]
        valid = []
        excluded = Counter()
        for record in selected:
            trial = record.short_term
            reason = None
            if (
                record.target_date in excluded_dates
                or record.quality_flags
                or record.deleted_sources
            ):
                reason = "unsuitable_basis"
            elif trial is None:
                reason = "no_timely_trial"
            elif trial["status"] != "recorded":
                reason = trial["reason"]
            elif (
                record.assessment is None
                or not record.assessment.valid
                or record.assessment.assessed_at > now
            ):
                reason = "incomplete_measurement"
            elif any(
                key not in by_id or evidence_fingerprint(by_id[key]) != fingerprint
                for key, fingerprint in trial["evidence"]
            ):
                reason = "changed_or_missing_evidence"
            elif any(
                by_id[key].target_date in excluded_dates for key, _ in trial["evidence"]
            ):
                reason = "excluded_input_day"
            if reason:
                excluded[reason] += 1
            else:
                valid.append(record)
        count = len(valid)
        days = len({record.target_date for record in valid})
        baseline_errors = [
            record.effective_energy_kwh - record.assessment.actual_energy_kwh
            for record in valid
        ]
        trial_errors = [
            record.short_term["energy_kwh"] - record.assessment.actual_energy_kwh
            for record in valid
        ]

        def mean(values, count=count):
            try:
                value = fsum(value / count for value in values) if count else None
                return value if value is None or isfinite(value) else None
            except OverflowError:
                return None

        baseline_mae = mean([abs(value) for value in baseline_errors])
        candidate_mae = mean([abs(value) for value in trial_errors])
        result["horizons"][horizon] = {
            "count": count,
            "days": days,
            "count_forecasts": len(selected),
            "coverage_fraction": count / len(selected) if selected else None,
            "baseline_mae_kwh": baseline_mae,
            "candidate_mae_kwh": candidate_mae,
            "baseline_bias_kwh": mean(baseline_errors),
            "candidate_bias_kwh": mean(trial_errors),
            "exclusions": dict(sorted(excluded.items())),
            "criterion_met": bool(
                days >= 30
                and baseline_mae
                and candidate_mae is not None
                and candidate_mae <= 0.95 * baseline_mae
            ),
        }
    return result
