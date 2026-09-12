"""Prospektiver Morgenvergleich auf ursprünglichen AC-Zählerintervallen.

Regel 1: geometrischer Sonnenaufgang bis vier absolute Stunden danach, 0,5 kW
über mindestens 60 Minuten nach derselben Intervallmittelregel. Der belegte
Morgenkern verwendet gemeinsame Originalgrenzen aller Quellen, höchstens 15
Minuten von den Fenstergrenzen entfernt; keine positive Differenz wird geteilt.
Primär ist sein Energie-MAE, anschließend gelten unveränderliche Nebenregeln.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from itertools import pairwise
from math import ceil, fsum, isfinite
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .calculations import (
    aggregate_energy_for_day,
    apply_calibration,
    apply_inverter_limits,
    calibrated_energy,
    forecast_basis,
    validate_coordinates,
)
from .measurements import SourceConfig, SourceHistory, aggregate_energy
from .models import (
    DailyYield,
    ForecastCalibrationBasis,
    ForecastResult,
    TotalForecastInterval,
)
from .shading import solar_position

if TYPE_CHECKING:
    from .history import ArchiveRecord

RULE_VERSION = 1
MAX_CASES = 96
MAX_BYTES = 2 * 1024 * 1024
MAX_INTERVALS = 50
THRESHOLD_KW = 0.5
RESOLUTION = timedelta(minutes=15)
HOUR = timedelta(hours=1)
TRAINING_DAYS = 30
VALIDATION_DAYS = 14


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Morgenvergleiche benötigen absolute Zeitpunkte")
    return value.astimezone(UTC)


def _stamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _hash(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _storage_size(data: Mapping[str, Any]) -> int:
    """Auch die Einrückung im nativen Archivrahmen auf das Teilbudget anrechnen."""
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    return len(encoded.encode()) + 8 * (encoded.count("\n") + 1)


def _case_evidence(case: Mapping[str, Any]) -> str:
    """Messrevision und den dazu vorab festgehaltenen gesamten Modellstand binden."""
    return _hash(
        [
            case["daily_fingerprint"],
            case["measurement"],
            {
                key: case[key]
                for key in (
                    "date",
                    "cutoff",
                    "fetched_at",
                    "observed_at",
                    "basis",
                    "sources",
                    "global_factor",
                    "candidate_id",
                    "candidate_factor",
                    "window_start",
                    "window_end",
                    "model_version",
                )
            },
        ]
    )


def _number(value: object, minimum: float = 0, maximum: float = 1e12) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError("Der Morgenvergleich benötigt eine endliche gültige Zahl")
    return float(value)


def morning_window(
    day: date, timezone: ZoneInfo, latitude: float, longitude: float
) -> tuple[datetime, datetime] | None:
    """Erste aufsteigende geometrische Nullquerung, auf eine Sekunde eingegrenzt."""
    validate_coordinates(latitude, longitude)
    start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, timezone).astimezone(UTC)
    left = start
    previous = solar_position(left, latitude, longitude)[0]
    while left < end:
        right = min(end, left + timedelta(minutes=5))
        elevation = solar_position(right, latitude, longitude)[0]
        if previous < 0 <= elevation:
            while right - left > timedelta(seconds=1):
                middle = left + (right - left) / 2
                if solar_position(middle, latitude, longitude)[0] >= 0:
                    right = middle
                else:
                    left = middle
            sunrise = right.replace(microsecond=0) + timedelta(seconds=1)
            return sunrise, sunrise + 4 * HOUR
        left, previous = right, elevation
    return None


def _basis_series(
    basis: ForecastCalibrationBasis,
    window: tuple[datetime, datetime],
    global_factor: float,
    morning_factor: float,
) -> tuple[TotalForecastInterval, ...]:
    """Morgenfaktor vor dem globalen Faktor und beiden bestehenden AC-Limits."""
    result = []
    for item in basis.intervals:
        bounds = sorted(
            {item.start, item.end}
            | {bound for bound in window if item.start < bound < item.end}
        )
        for left, right in pairwise(bounds):
            factor = morning_factor if window[0] <= left < window[1] else 1.0
            # Ein einzelnes Originalintervall verwendet exakt den gemeinsamen
            # Gruppen-/Gesamtlimitvertrag; es wird nicht aus AC rückgerechnet.
            part = replace(
                basis,
                intervals=(
                    replace(
                        item,
                        start=left,
                        end=right,
                        dc_power_kw=item.dc_power_kw * factor,
                        group_dc_power_kw=tuple(
                            power * factor for power in item.group_dc_power_kw
                        ),
                        ungrouped_dc_power_kw=(
                            item.ungrouped_dc_power_kw * factor
                            if item.ungrouped_dc_power_kw is not None
                            else None
                        ),
                    ),
                ),
            )
            energy = calibrated_energy(part, global_factor)
            result.append(
                TotalForecastInterval(
                    left,
                    right,
                    energy,
                    energy / ((right - left).total_seconds() / 3600),
                )
            )
    return tuple(result)


def apply_morning(
    forecast: ForecastResult,
    factor: float,
    global_factor: float,
    inverter_max_power_kw: float | None,
    timezone: ZoneInfo,
    latitude: float,
    longitude: float,
) -> ForecastResult:
    """Wirksame Dach- und Gesamtwerte; ursprüngliche DC bleibt unverändert lesbar."""
    _number(factor, 0.8, 1.2)
    _number(global_factor, 0.5, 1.5)
    if factor == 1:
        return apply_calibration(
            forecast, global_factor, inverter_max_power_kw, timezone
        )
    windows = tuple(
        window
        for offset in range(min(forecast.forecast_days, 2))
        if (
            window := morning_window(
                forecast.local_date + timedelta(days=offset),
                timezone,
                latitude,
                longitude,
            )
        )
    )
    roof_intervals: dict[str, list] = {key: [] for key in forecast.roofs}
    total = []
    for original in forecast.total_intervals:
        bounds = sorted(
            {original.start, original.end}
            | {
                bound
                for window in windows
                for bound in window
                if original.start < bound < original.end
            }
        )
        for start, end in pairwise(bounds):
            multiplier = global_factor * (
                factor if any(left <= start < right for left, right in windows) else 1
            )
            matching = {
                key: next(
                    (
                        item
                        for item in roof.intervals
                        if item.start <= start and item.end >= end
                    ),
                    None,
                )
                for key, roof in forecast.roofs.items()
            }
            powers = apply_inverter_limits(
                {
                    key: item.dc_power_kw * multiplier if item else 0.0
                    for key, item in matching.items()
                },
                inverter_max_power_kw,
                forecast.inverter_groups,
            )
            hours = (end - start).total_seconds() / 3600
            for key, item in matching.items():
                if item is not None:
                    roof_intervals[key].append(
                        replace(
                            item,
                            start=start,
                            end=end,
                            ac_power_kw=powers[key],
                            energy_kwh=powers[key] * hours,
                        )
                    )
            power = fsum(powers.values())
            total.append(
                replace(
                    original,
                    start=start,
                    end=end,
                    ac_power_kw=power,
                    energy_kwh=power * hours,
                )
            )

    def daily(items):
        return DailyYield(
            aggregate_energy_for_day(items, forecast.local_date, timezone),
            aggregate_energy_for_day(
                items, forecast.local_date + timedelta(days=1), timezone
            ),
        )

    return replace(
        forecast,
        roofs={
            key: replace(
                roof,
                intervals=tuple(roof_intervals[key]),
                daily=daily(roof_intervals[key]),
            )
            for key, roof in forecast.roofs.items()
        },
        total_intervals=tuple(total),
        total=daily(total),
    )


def _energy(series, start: datetime, end: datetime) -> float:
    return fsum(
        item.ac_power_kw
        * (min(end, item.end) - max(start, item.start)).total_seconds()
        / 3600
        for item in series
        if item.start < end and item.end > start
    )


def _rise(intervals: Sequence[tuple[datetime, datetime, float]]) -> datetime | None:
    """60 Minuten aufeinanderfolgende Intervalle mit Mittelwert mindestens 0,5 kW."""
    run_start = previous = None
    for start, end, power in intervals:
        if power < THRESHOLD_KW:
            run_start = previous = None
            continue
        if previous != start:
            run_start = start
        previous = end
        if end - run_start >= HOUR:
            return run_start
    return None


def measured_morning(
    histories: Sequence[SourceHistory],
    sources: Sequence[SourceConfig],
    window: tuple[datetime, datetime],
    now: datetime,
) -> dict[str, Any]:
    """Gemeinsame Originalgrenzen ohne Verteilung einer positiven Zählerdifferenz."""
    start, end = window
    by_id = {history.source.source_id: history for history in histories}
    selected = []
    boundaries = []
    for source in sources:
        history = by_id.get(source.source_id)
        if (
            history is None
            or history.source.measurement_identity != source.measurement_identity
        ):
            return {"valid": False, "reason": "source_identity_changed"}
        deltas = sorted(
            (
                item
                for item in history.deltas
                if item.start >= start and item.end <= end
            ),
            key=lambda item: item.start,
        )
        if not deltas:
            return {"valid": False, "reason": "measurements_missing"}
        selected.append((history, deltas))
        boundaries.append(
            {point for item in deltas for point in (item.start, item.end)}
        )
    common = sorted(set.intersection(*boundaries)) if boundaries else []
    if (
        len(common) < 2
        or common[0] - start > RESOLUTION
        or end - common[-1] > RESOLUTION
    ):
        return {"valid": False, "reason": "morning_coverage_missing"}
    core_start, core_end = common[0], common[-1]
    if any(right - left > RESOLUTION for left, right in pairwise(common)):
        return {"valid": False, "reason": "measurement_resolution_too_coarse"}
    if len(common) > 501:
        # Nur vollständige benachbarte Originaldifferenzen zusammenfassen.
        # Häufige HA-Meldungen dürfen die begrenzte Messkopie nicht aufblasen.
        reduced = [common[0]]
        previous = common[0]
        for bound in common[1:]:
            if bound - reduced[-1] > RESOLUTION:
                reduced.append(previous)
            previous = bound
        if reduced[-1] != common[-1]:
            reduced.append(common[-1])
        common = reduced
    evidence = []
    powers = []
    for history, deltas in selected:
        deltas = [
            item for item in deltas if item.start >= core_start and item.end <= core_end
        ]
        cursor = core_start
        for item in deltas:
            if (
                item.start != cursor
                or item.end - item.start > RESOLUTION
                or (set(item.quality_flags) - {"derived_energy"})
            ):
                return {"valid": False, "reason": "morning_coverage_missing"}
            cursor = item.end
        if cursor != core_end or len({item.segment_id for item in deltas}) != 1:
            return {"valid": False, "reason": "measurement_segment_changed"}
        snapshot = aggregate_energy([history], core_start, core_end, now)
        if not snapshot["energy_complete"]:
            return {"valid": False, "reason": "morning_coverage_missing"}
        evidence.append(
            {
                "identity": list(history.source.measurement_identity),
                "deltas": [item.to_dict() for item in deltas],
            }
        )
        powers.append(
            [
                fsum(
                    item.energy_kwh
                    for item in deltas
                    if item.start >= left and item.end <= right
                )
                / ((right - left).total_seconds() / 3600)
                for left, right in pairwise(common)
            ]
        )
    intervals = [
        (left, right, fsum(values[index] for values in powers))
        for index, (left, right) in enumerate(pairwise(common))
    ]
    rise = _rise(intervals)
    return {
        "valid": True,
        "reason": None,
        "start": core_start.isoformat(),
        "end": core_end.isoformat(),
        "edge_unassessed_minutes": [
            (core_start - start).total_seconds() / 60,
            (end - core_end).total_seconds() / 60,
        ],
        "intervals": [
            [left.isoformat(), right.isoformat(), power]
            for left, right, power in intervals
        ],
        "morning_energy_kwh": fsum(
            power * (right - left).total_seconds() / 3600
            for left, right, power in intervals
        ),
        "rise": rise.isoformat() if rise else None,
        "resolution_minutes": max(
            (right - left).total_seconds() / 60 for left, right in pairwise(common)
        ),
        "reported_at": core_end.isoformat(),
        "observed_at": _utc(now).isoformat(),
        "device_timestamp_verified": False,
        "fingerprint": _hash(evidence),
    }


def _features(
    case: Mapping[str, Any], factor: float, global_factor: float | None = None
) -> dict:
    basis = ForecastCalibrationBasis.from_dict(case["basis"])
    window = (_stamp(case["window_start"]), _stamp(case["window_end"]))
    series = _basis_series(
        basis,
        window,
        case["global_factor"] if global_factor is None else global_factor,
        factor,
    )
    measured = case["measurement"]
    intervals = [
        (
            _stamp(left),
            _stamp(right),
            _energy(series, _stamp(left), _stamp(right))
            / ((_stamp(right) - _stamp(left)).total_seconds() / 3600),
        )
        for left, right, _ in measured["intervals"]
    ]
    rise = _rise(intervals)
    return {
        "morning_energy_kwh": _energy(
            series, _stamp(measured["start"]), _stamp(measured["end"])
        ),
        "day_energy_kwh": fsum(item.energy_kwh for item in series),
        "rise": rise.isoformat() if rise else None,
    }


def evaluate_morning(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Alle Varianten auf denselben Tagen; fehlender Anstieg bleibt in der Stichprobe.

    Wenn nur eine Seite ansteigt, erhält der Zeitfehler die vorab festgelegte
    Fensterlänge von 240 Minuten. Beidseitiges Ausbleiben zählt mit 0; ohne einen
    auf allen Varianten und der Messung belegten Anstieg ist keine Freigabe möglich.
    """
    if not cases:
        raise ValueError("Die Morgenprüfung benötigt mindestens einen gleichen Fall")
    result: dict[str, Any] = {"sample_days": len(cases)}
    paired_rises = 0
    for case in cases:
        if all(
            case[key].get("rise") is not None
            for key in ("raw", "baseline", "candidate", "measurement")
        ):
            paired_rises += 1
    for variant in ("raw", "baseline", "candidate"):
        energy_errors, day_errors, time_errors, early_errors = [], [], [], []
        false_rises = neither = missed = 0
        for case in cases:
            prediction, actual = case[variant], case["measurement"]
            energy_errors.append(
                abs(prediction["morning_energy_kwh"] - actual["morning_energy_kwh"])
            )
            day_errors.append(
                abs(prediction["day_energy_kwh"] - actual["day_energy_kwh"])
            )
            predicted_rise, actual_rise = prediction["rise"], actual["rise"]
            if predicted_rise is not None and actual_rise is not None:
                difference = (
                    _stamp(predicted_rise) - _stamp(actual_rise)
                ).total_seconds() / 60
                time_errors.append(abs(difference))
                early_errors.append(max(0.0, -difference))
            elif predicted_rise is not None:
                false_rises += 1
                time_errors.append(240.0)
                early_errors.append(240.0)
            elif actual_rise is not None:
                missed += 1
                time_errors.append(240.0)
                early_errors.append(0.0)
            else:
                neither += 1
                time_errors.append(0.0)
                early_errors.append(0.0)
        count = len(cases)
        result.update(
            {
                f"{variant}_morning_mae_kwh": fsum(energy_errors) / count,
                f"{variant}_day_mae_kwh": fsum(day_errors) / count,
                f"{variant}_rise_mae_minutes": fsum(time_errors) / count,
                f"{variant}_early_p90_minutes": sorted(early_errors)[
                    ceil(0.9 * count) - 1
                ],
                f"{variant}_false_rise_rate": false_rises / count,
                f"{variant}_false_rises": false_rises,
                f"{variant}_missed_rises": missed,
                f"{variant}_neither_rises": neither,
            }
        )
    reasons = []
    baseline = result["baseline_morning_mae_kwh"]
    if baseline <= 0 or result["candidate_morning_mae_kwh"] > baseline * 0.95:
        reasons.append("insufficient_morning_improvement")
    for metric in ("rise_mae_minutes", "day_mae_kwh"):
        if result[f"candidate_{metric}"] > 1.05 * result[f"baseline_{metric}"] + 1e-10:
            reasons.append(f"{metric}_worse")
    for metric in ("false_rise_rate", "early_p90_minutes"):
        if result[f"candidate_{metric}"] > result[f"baseline_{metric}"] + 1e-10:
            reasons.append(f"{metric}_worse")
    if paired_rises == 0:
        reasons.append("rise_metric_unavailable")
    result.update(
        approved=not reasons,
        reasons=reasons,
        paired_rise_days=paired_rises,
        primary_metric="morning_energy_mae_kwh",
    )
    return result


class MorningState:
    """Begrenzte, ausschließlich im bestehenden Archiv gespeicherte Morgenbelege."""

    def __init__(
        self, configuration_id: str, segment_start: datetime, timezone: str = "UTC"
    ) -> None:
        if not isinstance(configuration_id, str) or not configuration_id:
            raise ValueError("Der Morgenvergleich benötigt die Anlagenkonfiguration")
        self.configuration_id = configuration_id
        self.segment_start = _utc(segment_start)
        self.timezone = ZoneInfo(timezone)
        self.cases: dict[str, dict[str, Any]] = {}
        self.candidate: dict[str, Any] | None = None
        self.status = "learning"
        self.reasons = ["insufficient_training_days"]
        self.exclusion_reasons: dict[str, int] = {}
        self.metrics: dict[str, Any] = {}
        self.training_days = self.validation_days = 0
        self.last_updated_at: datetime | None = None
        self.next_attempt_at: datetime | None = None
        self.retention_truncated = False

    @property
    def approved_factor(self) -> float:
        return (
            self.candidate["factor"]
            if self.status == "approved" and self.candidate
            else 1.0
        )

    @property
    def sources(self) -> tuple[SourceConfig, ...]:
        sources = {}
        for case in self.cases.values():
            for item in case["sources"]:
                source = SourceConfig.from_dict(item)
                sources[(source.source_id, source.measurement_identity)] = source
        return tuple(sources.values())

    def capture(
        self,
        forecast: ForecastResult,
        fetched_at: datetime,
        observed_at: datetime,
        measurement_sources: Sequence[SourceConfig],
        *,
        latitude: float,
        longitude: float,
        inverter_max_power_kw: float | None = None,
        global_factor: float = 1.0,
    ) -> bool:
        from .history import MODEL_VERSION

        fetched_at, observed_at = _utc(fetched_at), _utc(observed_at)
        _number(global_factor, 0.5, 1.5)
        if fetched_at > observed_at:
            raise ValueError("Eine Prognose kann nicht vor ihrem Abruf beobachtet sein")
        if self.last_updated_at is not None and observed_at < self.last_updated_at:
            return False
        sources = tuple(
            source for source in measurement_sources if source.kind != "power"
        )
        if not sources or len(
            {source.measurement_identity for source in sources}
        ) != len(sources):
            return False
        changed = False
        for offset in (0, 1):
            day = forecast.local_date + timedelta(days=offset)
            cutoff = datetime.combine(
                day - timedelta(days=1), time(18), self.timezone
            ).astimezone(UTC)
            if (
                not self.segment_start <= observed_at <= cutoff
                or cutoff - fetched_at > 2 * HOUR
            ):
                continue
            start = datetime.combine(day, time.min, self.timezone).astimezone(UTC)
            end = datetime.combine(
                day + timedelta(days=1), time.min, self.timezone
            ).astimezone(UTC)
            window = morning_window(day, self.timezone, latitude, longitude)
            basis = forecast_basis(forecast, start, end, inverter_max_power_kw)
            if (
                window is None
                or window[1] > end
                or basis is None
                or len(basis.intervals) > MAX_INTERVALS
            ):
                continue
            if any(
                item.quality_flags
                for item in forecast.total_intervals
                if item.start < end and item.end > start
            ):
                continue
            previous = self.cases.get(day.isoformat())
            if previous and _stamp(previous["observed_at"]) >= observed_at:
                continue
            candidate = (
                self.candidate if self.status in ("testing", "approved") else None
            )
            # Ein vor Kandidatenbildung beobachteter Wetterstand darf nicht
            # nachträglich zu einem angeblich prospektiven Prüfprofil werden.
            if candidate and _stamp(candidate["created_at"]) > observed_at:
                candidate = None
            candidate_id = candidate["id"] if candidate else None
            if previous and (
                fetched_at < _stamp(previous["fetched_at"])
                or (
                    fetched_at == _stamp(previous["fetched_at"])
                    and previous["global_factor"] == global_factor
                    and previous["candidate_id"] == candidate_id
                )
            ):
                continue
            self.cases[day.isoformat()] = {
                "date": day.isoformat(),
                "cutoff": cutoff.isoformat(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "window_start": window[0].isoformat(),
                "window_end": window[1].isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "observed_at": observed_at.isoformat(),
                "model_issued_at": None,
                "model_version": MODEL_VERSION,
                "basis": basis.to_dict(),
                "sources": [source.to_dict() for source in sources],
                "global_factor": global_factor,
                "candidate_id": candidate["id"] if candidate else None,
                "candidate_factor": candidate["factor"] if candidate else None,
                "measurement": None,
                "daily_fingerprint": None,
                "evidence_fingerprint": None,
            }
            changed = True
        return changed | self.prune(observed_at)

    def _invalidate(self, reason: str, now: datetime) -> None:
        self.status = "invalidated"
        self.reasons = [reason]
        self.next_attempt_at = now + timedelta(days=14)

    def _assessment(
        self, case: dict, record: ArchiveRecord | None, histories, now
    ) -> str | None:
        from .history import MODEL_VERSION

        assessment = record.assessment if record else None
        if (
            record is None
            or record.configuration_id != self.configuration_id
            or record.model_version != MODEL_VERSION
            or case["model_version"] != MODEL_VERSION
            or record.deleted_sources
            or record.quality_flags
            or assessment is None
            or not assessment.valid
            or assessment.manual
            or not _stamp(case["end"]) <= assessment.assessed_at <= now
            or assessment.actual_energy_kwh is None
        ):
            if case["measurement"]:
                case["measurement"] = None
                case["evidence_fingerprint"] = None
            return "daily_measurement_unavailable"
        if {source.measurement_identity for source in record.measurement_sources} != {
            SourceConfig.from_dict(source).measurement_identity
            for source in case["sources"]
        }:
            case["measurement"] = None
            case["evidence_fingerprint"] = None
            return "source_identity_changed"
        fingerprint = _hash(assessment.to_dict())
        if (
            case["measurement"] is not None
            and case["measurement"]["day_energy_kwh"] != assessment.actual_energy_kwh
        ):
            case["measurement"] = None
            case["evidence_fingerprint"] = None
            case["correction_invalidated"] = True
        if case["daily_fingerprint"] and fingerprint != case["daily_fingerprint"]:
            # Eine Tageskorrektur kann keine verschwundene Morgenform berichtigen.
            case["measurement"] = None
            case["evidence_fingerprint"] = None
            case["correction_invalidated"] = True
        if case.get("correction_invalidated"):
            return "measurement_corrected"
        if _stamp(case["end"]) >= now - timedelta(days=6):
            measured = measured_morning(
                histories,
                [SourceConfig.from_dict(item) for item in case["sources"]],
                (_stamp(case["window_start"]), _stamp(case["window_end"])),
                now,
            )
            if measured["valid"]:
                if (
                    case["measurement"]
                    and measured["fingerprint"] != case["measurement"]["fingerprint"]
                ):
                    case["measurement"] = None
                    case["evidence_fingerprint"] = None
                    case["correction_invalidated"] = True
                    return "measurement_corrected"
                # Eine unveränderte Messkopie behält ihre echte erste Beobachtungszeit.
                if case["measurement"] is None:
                    measured["day_energy_kwh"] = assessment.actual_energy_kwh
                    case["measurement"] = measured
            else:
                if case["measurement"] is not None:
                    case["measurement"] = None
                    case["evidence_fingerprint"] = None
                    case["correction_invalidated"] = True
                return measured["reason"]
        if case["measurement"] is None:
            return "measurements_missing"
        case["daily_fingerprint"] = fingerprint
        case["evidence_fingerprint"] = _case_evidence(case)
        case["raw"] = _features(case, 1.0, 1.0)
        case["baseline"] = _features(case, 1.0)
        if case["candidate_factor"] is not None:
            case["candidate"] = _features(case, case["candidate_factor"])
        return None

    def reconcile(
        self,
        histories: Sequence[SourceHistory],
        records: Iterable[ArchiveRecord],
        now: datetime,
        excluded_dates: Iterable[date | str] = (),
    ) -> bool:
        now = _utc(now)
        before = self.to_dict()
        if self.last_updated_at and now < self.last_updated_at:
            return False
        self.last_updated_at = now
        excluded = {
            item.isoformat() if isinstance(item, date) else item
            for item in excluded_dates
        }
        daily = {
            record.target_date.isoformat(): record
            for record in records
            if record.horizon == "daily_previous_18"
            and record.configuration_id == self.configuration_id
        }
        eligible = []
        exclusions: Counter[str] = Counter()
        for key, case in sorted(self.cases.items()):
            if _stamp(case["end"]) > now:
                continue
            reason = (
                "known_curtailment"
                if key in excluded
                else self._assessment(case, daily.get(key), histories, now)
            )
            if reason:
                exclusions[reason] += 1
            else:
                eligible.append(case)
        self.exclusion_reasons = dict(sorted(exclusions.items()))
        by_date = {case["date"]: case for case in eligible}
        if self.candidate and self.status in ("testing", "approved"):
            refs = {**self.candidate["training"], **self.candidate["validation"]}
            if any(
                key not in by_date or by_date[key]["evidence_fingerprint"] != value
                for key, value in refs.items()
            ):
                self._invalidate("evidence_changed", now)
                return before != self.to_dict()
        if self.next_attempt_at and now < self.next_attempt_at:
            return before != self.to_dict()
        if self.status in ("invalidated", "rejected"):
            self.candidate = None
            self.status = "learning"
            self.metrics = {}
            self.validation_days = 0
        today = now.astimezone(self.timezone).date()
        if self.status == "learning":
            training = [
                case
                for case in eligible
                if today - timedelta(days=60)
                <= date.fromisoformat(case["date"])
                < today
            ][-TRAINING_DAYS:]
            self.training_days = len(training)
            self.reasons = ["insufficient_training_days"]
            if len(training) < TRAINING_DAYS:
                return before != self.to_dict()

            scoring = [
                (
                    ForecastCalibrationBasis.from_dict(case["basis"]),
                    (_stamp(case["window_start"]), _stamp(case["window_end"])),
                    case["global_factor"],
                    _stamp(case["measurement"]["start"]),
                    _stamp(case["measurement"]["end"]),
                    case["measurement"]["morning_energy_kwh"],
                )
                for case in training
            ]

            def score(factor):
                return (
                    fsum(
                        abs(
                            _energy(
                                _basis_series(basis, window, global_factor, factor),
                                start,
                                end,
                            )
                            - actual
                        )
                        for basis, window, global_factor, start, end, actual in scoring
                    ),
                    abs(factor - 1),
                    factor,
                )

            factor = min((value / 100 for value in range(80, 121)), key=score)
            refs = {case["date"]: case["evidence_fingerprint"] for case in training}
            self.candidate = {
                "id": _hash([self.configuration_id, now.isoformat(), factor, refs]),
                "factor": factor,
                "created_at": now.isoformat(),
                "training": refs,
                "validation": {},
                "first_date": (
                    today
                    + timedelta(
                        days=1 if now.astimezone(self.timezone).time() < time(18) else 2
                    )
                ).isoformat(),
            }
            self.status = "testing"
            self.next_attempt_at = None
        candidate = self.candidate
        first = date.fromisoformat(candidate["first_date"])
        selected = [
            case
            for case in eligible
            if case["candidate_id"] == candidate["id"]
            and _stamp(case["observed_at"]) >= _stamp(candidate["created_at"])
            and _stamp(case["cutoff"]) > _stamp(candidate["created_at"])
            and max(first, today - timedelta(days=28))
            <= date.fromisoformat(case["date"])
            < today
            and (
                self.status == "approved"
                or date.fromisoformat(case["date"]) < first + timedelta(days=28)
            )
        ]
        self.validation_days = min(len(selected), VALIDATION_DAYS)
        candidate["validation"] = {
            case["date"]: case["evidence_fingerprint"] for case in selected[-28:]
        }
        if len(selected) < VALIDATION_DAYS:
            self.reasons = ["insufficient_validation_days"]
            if self.status == "approved":
                self._invalidate("validation_expired", now)
            elif today >= first + timedelta(days=28):
                self.status = "rejected"
                self.reasons = ["validation_expired"]
                self.next_attempt_at = now + timedelta(days=14)
        else:
            self.metrics = evaluate_morning(selected[-VALIDATION_DAYS:])
            self.reasons = list(self.metrics["reasons"])
            self.status = "approved" if self.metrics["approved"] else "rejected"
            if self.status == "rejected":
                self.next_attempt_at = now + timedelta(days=14)
        self.prune(now)
        return before != self.to_dict()

    def snapshot(self, mode: str = "observe") -> dict[str, Any]:
        """Additiver Lesevertrag ohne private Messintervalle oder Quellennamen."""
        applied = mode == "auto" and self.status == "approved"
        return {
            "schema_version": 1,
            "rule_version": RULE_VERSION,
            "mode": mode,
            "status": "off" if mode == "off" else self.status,
            "applied": applied,
            "effective_factor": self.approved_factor if applied else 1.0,
            "candidate_factor": self.candidate["factor"] if self.candidate else None,
            "candidate_id": self.candidate["id"] if self.candidate else None,
            "training_days": self.training_days,
            "validation_days": self.validation_days,
            "reasons": list(self.reasons),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "metrics": dict(self.metrics),
            "primary_metric": "morning_energy_mae_kwh",
            "method": "morning_factor_v1",
            "threshold_kw": THRESHOLD_KW,
            "sustained_minutes": 60,
            "measurement_max_resolution_minutes": 15,
            "measurement_rule": "common_original_interval_means",
            "morning_energy_scope": "common_measured_core_inside_sunrise_plus_4h",
            "maximum_unassessed_edge_minutes": 15,
            "forecast_resolution": "original_weather_intervals",
            "cutoff": "daily_previous_18",
            "maximum_forecast_age_minutes": 120,
            "model_issued_at": None,
            "device_timestamp_verified": False,
            "window_uncertainty": {
                "status": "unavailable",
                "reason": "no_matching_prospective_window_evidence",
            },
            "real_world_validation": "not_demonstrated",
            "retention_truncated": self.retention_truncated,
        }

    def prune(self, now: datetime) -> bool:
        before = len(self.cases)
        first = _utc(now).astimezone(self.timezone).date() - timedelta(days=96)
        self.cases = {
            key: value
            for key, value in sorted(self.cases.items())
            if date.fromisoformat(key) >= first
        }
        while len(self.cases) > MAX_CASES or _storage_size(self.to_dict()) > MAX_BYTES:
            if not self.cases:
                raise ValueError(
                    "Der Morgen-Lernzustand überschreitet seine Speichergrenze"
                )
            self.cases.pop(next(iter(self.cases)))
        changed = before != len(self.cases)
        if changed:
            self.retention_truncated = True
            if (
                self.candidate
                and self.status in ("testing", "approved")
                and any(
                    key not in self.cases
                    for key in (
                        *self.candidate["training"],
                        *self.candidate["validation"],
                    )
                )
            ):
                self._invalidate("evidence_expired", _utc(now))
        return changed

    def delete_measurement_source(self, source_id: str) -> bool:
        selected = [
            key
            for key, case in self.cases.items()
            if any(source["source_id"] == source_id for source in case["sources"])
        ]
        for key in selected:
            del self.cases[key]
        if selected:
            self.candidate = None
            self.status = "learning"
            self.training_days = self.validation_days = 0
            self.metrics = {}
            self.reasons = ["measurement_source_deleted"]
        return bool(selected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_version": RULE_VERSION,
            "configuration_id": self.configuration_id,
            "segment_start": self.segment_start.isoformat(),
            "timezone": self.timezone.key,
            "cases": deepcopy(list(self.cases.values())),
            "candidate": deepcopy(self.candidate),
            "status": self.status,
            "reasons": list(self.reasons),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "metrics": dict(self.metrics),
            "training_days": self.training_days,
            "validation_days": self.validation_days,
            "last_updated_at": (
                self.last_updated_at.isoformat() if self.last_updated_at else None
            ),
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "retention_truncated": self.retention_truncated,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MorningState:
        """Unbekannte, zu große oder inkonsistente Belege niemals freigeben."""
        from .history import MODEL_VERSION

        try:
            # JSON-Kopie entkoppelt ausstehende Speicherstände von weiteren Änderungen.
            text = json.dumps(data, allow_nan=False)
            if (
                _storage_size(data) > MAX_BYTES
                or type(data["rule_version"]) is not int
                or data["rule_version"] != RULE_VERSION
            ):
                raise ValueError("Unbekannter oder zu großer Morgenvertrag")
            data = json.loads(text)
            state = cls(
                data["configuration_id"],
                _stamp(data["segment_start"]),
                data["timezone"],
            )
            if not isinstance(data["cases"], list) or len(data["cases"]) > MAX_CASES:
                raise ValueError("Zu viele gespeicherte Morgenfälle")
            for case in data["cases"]:
                day = date.fromisoformat(case["date"])
                basis = ForecastCalibrationBasis.from_dict(case["basis"])
                start, end, cutoff = (
                    _stamp(case[key]) for key in ("start", "end", "cutoff")
                )
                if (
                    len(basis.intervals) > MAX_INTERVALS
                    or basis.intervals[0].start != start
                    or basis.intervals[-1].end != end
                    or start
                    != datetime.combine(day, time.min, state.timezone).astimezone(UTC)
                    or end
                    != datetime.combine(
                        day + timedelta(days=1), time.min, state.timezone
                    ).astimezone(UTC)
                    or cutoff
                    != datetime.combine(
                        day - timedelta(days=1), time(18), state.timezone
                    ).astimezone(UTC)
                    or not state.segment_start <= _stamp(case["observed_at"]) <= cutoff
                    or not cutoff - 2 * HOUR
                    <= _stamp(case["fetched_at"])
                    <= _stamp(case["observed_at"])
                    or not start
                    <= _stamp(case["window_start"])
                    < _stamp(case["window_end"])
                    <= end
                    or _stamp(case["window_end"]) - _stamp(case["window_start"])
                    != 4 * HOUR
                    or case["date"] in state.cases
                    or case.get("model_issued_at") is not None
                    or case["model_version"] != MODEL_VERSION
                ):
                    raise ValueError("Ungültiger eingefrorener Morgenfall")
                _number(case["global_factor"], 0.5, 1.5)
                sources = [SourceConfig.from_dict(item) for item in case["sources"]]
                if (
                    not sources
                    or any(source.kind == "power" for source in sources)
                    or len({source.measurement_identity for source in sources})
                    != len(sources)
                ):
                    raise ValueError("Ungültige Quellen des Morgenvergleichs")
                if case["candidate_factor"] is not None:
                    _number(case["candidate_factor"], 0.8, 1.2)
                    if (
                        not isinstance(case["candidate_id"], str)
                        or not case["candidate_id"]
                    ):
                        raise ValueError("Der Morgenkandidat hat keine Identität")
                measurement = case["measurement"]
                if measurement is not None:
                    _validate_measurement(measurement, case)
                state.cases[case["date"]] = case
            state.candidate = data["candidate"]
            state.status = data["status"]
            if state.status not in (
                "learning",
                "testing",
                "approved",
                "rejected",
                "invalidated",
            ):
                raise ValueError("Unbekannter Morgenstatus")
            if state.candidate:
                candidate = state.candidate
                factor = _number(candidate["factor"], 0.8, 1.2)
                if (
                    abs(factor * 100 - round(factor * 100)) > 1e-8
                    or len(candidate["training"]) != 30
                    or len(candidate["validation"]) > 28
                ):
                    raise ValueError("Ungültiger Morgenkandidat")
                if _stamp(candidate["created_at"]) < state.segment_start:
                    raise ValueError("Der Morgenkandidat liegt vor seinem Segment")
                date.fromisoformat(candidate["first_date"])
                for refs in (candidate["training"], candidate["validation"]):
                    if not isinstance(refs, dict) or any(
                        not isinstance(value, str) or not value
                        for value in refs.values()
                    ):
                        raise ValueError("Ungültige Morgen-Prüfreferenzen")
            elif state.status != "learning":
                raise ValueError("Ein Morgen-Prüfstatus benötigt seinen Kandidaten")
            state.reasons = list(data["reasons"])
            state.exclusion_reasons = dict(data["exclusion_reasons"])
            state.metrics = dict(data["metrics"])
            state.training_days = int(_number(data["training_days"], 0, 30))
            state.validation_days = int(_number(data["validation_days"], 0, 14))
            state.last_updated_at = (
                _stamp(data["last_updated_at"]) if data["last_updated_at"] else None
            )
            state.next_attempt_at = (
                _stamp(data["next_attempt_at"]) if data["next_attempt_at"] else None
            )
            state.retention_truncated = data["retention_truncated"] is True
            if state.status == "approved" and (
                state.validation_days != 14 or state.metrics.get("approved") is not True
            ):
                raise ValueError(
                    "Eine gespeicherte Morgenfreigabe benötigt ihre Prüfung"
                )
            if state.candidate:
                state._validate_candidate()
            return state
        except (KeyError, TypeError, AttributeError, OverflowError) as err:
            raise ValueError("Beschädigter Morgenvergleich") from err

    def _validate_candidate(self) -> None:
        """Gespeicherte Freigaben aus ihren unveränderten Referenzen nachvollziehen."""
        candidate = self.candidate
        created = _stamp(candidate["created_at"])
        if candidate["id"] != _hash(
            [
                self.configuration_id,
                created.isoformat(),
                candidate["factor"],
                candidate["training"],
            ]
        ):
            raise ValueError(
                "Die Identität des Morgenkandidaten passt nicht zu seiner Basis"
            )
        if self.status not in ("testing", "approved"):
            return
        for key, fingerprint in {
            **candidate["training"],
            **candidate["validation"],
        }.items():
            case = self.cases.get(key)
            if (
                case is None
                or case["measurement"] is None
                or case["evidence_fingerprint"] != fingerprint
                or fingerprint != _case_evidence(case)
            ):
                raise ValueError("Die gespeicherten Morgen-Prüfbelege fehlen")
            if key in candidate["training"]:
                if (
                    _stamp(case["measurement"]["observed_at"]) > created
                    or _stamp(case["end"]) > created
                ):
                    raise ValueError(
                        "Eine Morgen-Trainingsrevision ist erst später bekannt"
                    )
            elif (
                case["candidate_id"] != candidate["id"]
                or case["candidate_factor"] != candidate["factor"]
                or _stamp(case["observed_at"]) < created
                or _stamp(case["cutoff"]) <= created
                or key < candidate["first_date"]
            ):
                raise ValueError("Die gespeicherte Morgenprüfung ist nicht prospektiv")
            case["raw"] = _features(case, 1.0, 1.0)
            case["baseline"] = _features(case, 1.0)
            if case["candidate_factor"] is not None:
                case["candidate"] = _features(case, case["candidate_factor"])
        if self.status == "approved":
            selected = [
                self.cases[key] for key in sorted(candidate["validation"])[-14:]
            ]
            if len(selected) != 14 or not evaluate_morning(selected)["approved"]:
                raise ValueError(
                    "Der gespeicherte Morgenfaktor besteht seine Prüfung nicht"
                )


def _validate_measurement(measurement: dict, case: dict) -> None:
    intervals = measurement["intervals"]
    if not isinstance(intervals, list) or not 1 <= len(intervals) <= 500:
        raise ValueError("Ungültige Anzahl ursprünglicher Messintervalle")
    cursor = _stamp(measurement["start"])
    start = cursor
    powers = []
    for left, right, power in intervals:
        left, right = _stamp(left), _stamp(right)
        if left != cursor or not timedelta(0) < right - left <= RESOLUTION:
            raise ValueError("Die Morgenmessung hat keine vollständige feine Abdeckung")
        powers.append((left, right, _number(power)))
        cursor = right
    if (
        cursor != _stamp(measurement["end"])
        or not timedelta(0) <= start - _stamp(case["window_start"]) <= RESOLUTION
        or not timedelta(0) <= _stamp(case["window_end"]) - cursor <= RESOLUTION
        or measurement["valid"] is not True
        or not isinstance(measurement["fingerprint"], str)
    ):
        raise ValueError("Die ursprünglichen Morgenmessgrenzen sind inkonsistent")
    _number(measurement["morning_energy_kwh"])
    _number(measurement["day_energy_kwh"])
    energy = fsum(
        power * (right - left).total_seconds() / 3600 for left, right, power in powers
    )
    if abs(energy - measurement["morning_energy_kwh"]) > 1e-8:
        raise ValueError(
            "Die gespeicherte Morgenmesssumme passt nicht zu den Intervallen"
        )
    rise = _rise(powers)
    if measurement["rise"] != (rise.isoformat() if rise else None):
        raise ValueError("Der gespeicherte PV-Anstieg passt nicht zur Messung")
