"""Begrenzte Morgenform und kausale Nutzenprüfung aus Issue #172, ohne HA."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import ceil, fsum, isfinite
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sunrise

from .calculations import aggregate_energy_for_day, apply_inverter_limits
from .forecast_intervals import window_energy
from .models import (
    DailyYield,
    ForecastCalibrationBasis,
    ForecastResult,
    RoofForecastInterval,
    TotalForecastInterval,
)

METHOD = "morning_redistribution_v1"
COEFFICIENTS = tuple(i / 10 for i in range(-5, 6))
WINDOW = timedelta(hours=4)
STABILITY = timedelta(hours=1)
MAX_STEP = timedelta(minutes=15)
MIN_STEP = timedelta(minutes=5)


def instant(value: Any) -> datetime:
    """Gespeicherte Zeitpunkte strikt als absolute UTC-Zeit validieren."""
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("Ein absoluter Zeitpunkt ist erforderlich")
    return result.astimezone(UTC)


def number(value: Any) -> float:
    """Bool, NaN, Unendlich und negative Energie sind keine Messung."""
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise ValueError("Ungültige Energie")
    return float(value)


def morning_start(
    day: date, timezone: str, latitude: float, longitude: float
) -> datetime | None:
    """Der Anlagenstandort bestimmt den Sonnenaufgang; Polartage bleiben unbekannt."""
    try:
        return sunrise(
            Observer(latitude, longitude), day, ZoneInfo(timezone)
        ).astimezone(UTC)
    except ValueError:
        return None


def _parts(
    start: datetime, end: datetime, dawn: datetime
) -> list[tuple[datetime, datetime]]:
    points = sorted(
        {
            start,
            end,
            *(p for p in (dawn, dawn + WINDOW / 2, dawn + WINDOW) if start < p < end),
        }
    )
    return list(pairwise(points))


def _factors(
    basis: ForecastCalibrationBasis, dawn: datetime, coefficient: float
) -> tuple[float, float]:
    if coefficient not in COEFFICIENTS or type(coefficient) is bool:
        raise ValueError("Unbekannter Morgenkandidat")
    energies = [0.0, 0.0]
    for item in basis.intervals:
        for left, right in _parts(item.start, item.end, dawn):
            if dawn <= left < dawn + WINDOW:
                half = int(left >= dawn + WINDOW / 2)
                energies[half] += (
                    item.dc_power_kw * (right - left).total_seconds() / 3600
                )
    transfer = min(energies) * coefficient
    return tuple(
        1 + sign * transfer / value if value > 0 else 1.0
        for sign, value in zip((-1, 1), energies, strict=True)
    )


def _factor(at: datetime, dawn: datetime, factors: tuple[float, float]) -> float:
    return factors[int(at >= dawn + WINDOW / 2)] if dawn <= at < dawn + WINDOW else 1.0


def projected_basis(
    basis: ForecastCalibrationBasis,
    dawn: datetime,
    coefficient: float,
    global_factor: float,
) -> tuple[TotalForecastInterval, ...]:
    """Energie innerhalb des Morgens verschieben, danach bekannte AC-Limits einmal."""
    if not isfinite(global_factor) or not 0.5 <= global_factor <= 1.5:
        raise ValueError("Ungültiger Anlagenfaktor")
    factors = _factors(basis, dawn, coefficient)
    result = []
    for item in basis.intervals:
        for left, right in _parts(item.start, item.end, dawn):
            scale = global_factor * _factor(left, dawn, factors)
            if basis.group_limits:
                power = (
                    fsum(
                        min(value * scale, limit)
                        for value, (_, limit) in zip(
                            item.group_dc_power_kw, basis.group_limits, strict=True
                        )
                    )
                    + (item.ungrouped_dc_power_kw or 0) * scale
                )
            else:
                power = item.dc_power_kw * scale
            if basis.inverter_max_power_kw is not None:
                power = min(power, basis.inverter_max_power_kw)
            energy = number(power * (right - left).total_seconds() / 3600)
            result.append(TotalForecastInterval(left, right, energy, power))
    return tuple(result)


def apply_morning(
    raw: ForecastResult,
    baseline: ForecastResult,
    *,
    coefficient: float,
    global_factor: float,
    limit: float | None,
    timezone: str,
    latitude: float,
    longitude: float,
) -> ForecastResult:
    """Alle Dach-/Gesamtwerte aus derselben DC-Basis bilden; keine AC-Rückrechnung."""
    from .calculations import forecast_basis

    if coefficient == 0:
        return baseline
    zone = ZoneInfo(timezone)
    profiles = []
    for offset in range(min(raw.forecast_days, 2)):
        day = raw.local_date + timedelta(days=offset)
        dawn = morning_start(day, timezone, latitude, longitude)
        if dawn is None:
            continue
        basis = forecast_basis(raw, dawn, dawn + WINDOW, limit)
        if basis is None or any(
            i.quality_flags or not i.is_complete
            for i in raw.total_intervals
            if i.start < dawn + WINDOW and i.end > dawn
        ):
            continue
        profiles.append((dawn, _factors(basis, dawn, coefficient)))
    if not profiles:
        return baseline
    roofs: dict[str, list[RoofForecastInterval]] = {key: [] for key in raw.roofs}
    totals = []
    for item in raw.total_intervals:
        bounds = {item.start, item.end}
        for dawn, _ in profiles:
            bounds.update(
                p
                for p in (dawn, dawn + WINDOW / 2, dawn + WINDOW)
                if item.start < p < item.end
            )
        ordered = sorted(bounds)
        for left, right in pairwise(ordered):
            scale = global_factor
            for dawn, factors in profiles:
                scale *= _factor(left, dawn, factors)
            dc = {}
            for key, roof in raw.roofs.items():
                matches = [
                    i.dc_power_kw
                    for i in roof.intervals
                    if i.start <= left and i.end >= right
                ]
                if len(matches) != 1:
                    return baseline
                dc[key] = matches[0]
            ac = apply_inverter_limits(
                {key: power * scale for key, power in dc.items()},
                limit,
                raw.inverter_groups,
            )
            hours = (right - left).total_seconds() / 3600
            for key in roofs:
                roofs[key].append(
                    RoofForecastInterval(
                        left, right, dc[key], ac[key], number(ac[key] * hours)
                    )
                )
            total = number(fsum(ac.values()))
            totals.append(
                replace(
                    item,
                    start=left,
                    end=right,
                    ac_power_kw=total,
                    energy_kwh=number(total * hours),
                )
            )

    def daily(intervals: Any) -> DailyYield:
        return DailyYield(
            aggregate_energy_for_day(intervals, raw.local_date, zone),
            aggregate_energy_for_day(
                intervals, raw.local_date + timedelta(days=1), zone
            ),
        )

    return replace(
        raw,
        roofs={
            key: replace(raw.roofs[key], intervals=tuple(values), daily=daily(values))
            for key, values in roofs.items()
        },
        total_intervals=tuple(totals),
        total=daily(totals),
    )


def measured_trace(
    evidence: dict[str, Any], expected_sources: set[str], dawn: datetime
) -> tuple[TotalForecastInterval, ...]:
    """Nur gemeinsame echte Zählergrenzen zusammenfassen, positive Deltas nie teilen."""
    sources = [s for s in evidence["sources"] if s["kind"] != "power"]
    if (
        len(sources) != len(expected_sources)
        or {s["source_id"] for s in sources} != expected_sources
    ):
        raise ValueError("measurement_source_missing")
    rows = []
    for source in sources:
        if source.get("identity_unresolved"):
            raise ValueError("measurement_identity_unresolved")
        deltas = []
        segments = set()
        for item in source["deltas"]:
            left, right = instant(item["start"]), instant(item["end"])
            if right <= dawn or left >= dawn + WINDOW:
                continue
            if (
                set(item["quality_flags"]) - {"derived_energy"}
                or not timedelta(0) < right - left <= MAX_STEP
            ):
                raise ValueError("measurement_resolution_or_gap")
            if left < dawn or right > dawn + WINDOW:
                continue
            segments.add(item["segment_id"])
            deltas.append((left, right, number(item["energy_kwh"])))
        if (
            not deltas
            or len(segments) != 1
            or any(a[1] != b[0] for a, b in pairwise(deltas))
        ):
            raise ValueError("measurement_gap")
        rows.append(deltas)
    boundaries = set.intersection(
        *({p for left, right, _ in row for p in (left, right)} for row in rows)
    )
    boundaries = sorted(p for p in boundaries if dawn <= p <= dawn + WINDOW)
    if (
        not boundaries
        or boundaries[0] - dawn > MAX_STEP
        or dawn + WINDOW - boundaries[-1] > MAX_STEP
    ):
        raise ValueError("measurement_boundary_gap")
    compact = [boundaries[0]]
    for point in boundaries[1:]:
        if point - compact[-1] >= MIN_STEP:
            compact.append(point)
    if compact[-1] != boundaries[-1]:
        compact.append(boundaries[-1])
    result = []
    for left, right in pairwise(compact):
        if right - left > MAX_STEP:
            raise ValueError("measurement_resolution_or_gap")
        energies = []
        for row in rows:
            selected = [(a, b, e) for a, b, e in row if left <= a and b <= right]
            if not selected or selected[0][0] != left or selected[-1][1] != right:
                raise ValueError("measurement_boundary_gap")
            energies.extend(e for _, _, e in selected)
        energy = number(fsum(energies))
        result.append(
            TotalForecastInterval(
                left, right, energy, energy * 3600 / (right - left).total_seconds()
            )
        )
    if len(result) > 96 or not result:
        raise ValueError("measurement_resolution_or_gap")
    return tuple(result)


def rise(
    intervals: tuple[TotalForecastInterval, ...], threshold: float
) -> datetime | None:
    """60 Minuten Intervallmittel sind kein kontinuierliches Leistungsversprechen."""
    begin = None
    previous = None
    for item in intervals:
        if item.ac_power_kw < threshold or item.quality_flags or not item.is_complete:
            begin = None
        else:
            if begin is None or previous != item.start:
                begin = item.start
            if item.end - begin >= STABILITY:
                return begin
        previous = item.end
    return None


@dataclass(frozen=True)
class MorningCase:
    """Ein ganzer unabhängiger Morgen mit festem Vorabendstand und echtem Ist."""

    record_id: str
    target_date: date
    cutoff: datetime
    fingerprint: str
    dawn: datetime
    basis: ForecastCalibrationBasis
    global_factor: float
    measured: tuple[TotalForecastInterval, ...]
    day_actual_kwh: float
    trial_id: str | None = None


def _error(
    case: MorningCase, coefficient: float, threshold: float
) -> dict[str, float | int]:
    forecast = projected_basis(case.basis, case.dawn, coefficient, case.global_factor)
    projected = []
    for item in case.measured:
        energy = window_energy(forecast, item.start, item.end)
        if energy is None:
            raise ValueError("forecast_coverage")
        projected.append(
            replace(
                item,
                energy_kwh=energy,
                ac_power_kw=energy * 3600 / (item.end - item.start).total_seconds(),
            )
        )
    actual, predicted = rise(case.measured, threshold), rise(
        tuple(projected), threshold
    )
    missing = actual is None or predicted is None
    time_error = (
        (240.0 if actual != predicted else 0.0)
        if missing
        else abs((actual - predicted).total_seconds()) / 60
    )
    early = (
        (240.0 if predicted is not None else 0.0)
        if actual is None
        else (
            max(0.0, (actual - predicted).total_seconds() / 60)
            if predicted is not None
            else 0.0
        )
    )
    return {
        "time": time_error,
        "early": early,
        "energy": abs(
            fsum(i.energy_kwh for i in projected)
            - fsum(i.energy_kwh for i in case.measured)
        ),
        "day": abs(fsum(i.energy_kwh for i in forecast) - case.day_actual_kwh),
        "actual_rise": int(actual is not None),
        "false_rise": int(actual is None and predicted is not None),
        "missed_rise": int(actual is not None and predicted is None),
        "both_absent": int(actual is None and predicted is None),
    }


def metrics(
    cases: list[MorningCase], coefficient: float, threshold: float
) -> dict[str, Any]:
    if not cases:
        return {}
    errors = [_error(case, coefficient, threshold) for case in cases]
    early = sorted(float(item["early"]) for item in errors)
    return {
        "days": len(cases),
        "time_mae_minutes": fsum(float(e["time"]) for e in errors) / len(errors),
        "morning_mae_kwh": fsum(float(e["energy"]) for e in errors) / len(errors),
        "day_mae_kwh": fsum(float(e["day"]) for e in errors) / len(errors),
        "early_p90_minutes": early[ceil(0.9 * len(early)) - 1],
        **{
            key: sum(int(e[key]) for e in errors)
            for key in ("actual_rise", "false_rise", "missed_rise", "both_absent")
        },
    }


def approved(baseline: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if (
        not baseline
        or baseline["days"] < 14
        or baseline["actual_rise"] == 0
        or baseline["time_mae_minutes"] <= 0
    ):
        return False
    return (
        candidate["time_mae_minutes"] <= baseline["time_mae_minutes"] * 0.95
        and candidate["false_rise"] <= baseline["false_rise"]
        and candidate["early_p90_minutes"] <= baseline["early_p90_minutes"]
        and all(
            candidate[key] <= baseline[key] * 1.05 + 1e-10
            for key in ("morning_mae_kwh", "day_mae_kwh")
        )
    )


class MorningLearning:
    """Ein fester Kandidat, getrennte 30/14-Tage-Belege und laufender Entzug."""

    def __init__(self, began: datetime, threshold: float = 0.5) -> None:
        self.began = instant(began)
        self.threshold = number(threshold)
        if not 0.05 <= self.threshold <= 100:
            raise ValueError("Ungültige AC-Schwelle")
        self.candidate: dict[str, Any] | None = None
        self.retry_after: datetime | None = None
        self.report: dict[str, Any] = {
            "status": "insufficient_training",
            "training_days": 0,
            "validation_days": 0,
        }

    def update(self, cases: list[MorningCase], now: datetime, today: date) -> None:
        cases = sorted(
            (c for c in cases if c.cutoff >= self.began and c.target_date < today),
            key=lambda c: c.target_date,
        )
        if len({c.target_date for c in cases}) != len(cases):
            raise ValueError("Doppelte Morgenfälle")
        by_id = {case.record_id: case for case in cases}
        if self.candidate:
            refs = {
                **self.candidate["training"],
                **self.candidate.get("validation", {}),
            }
            if any(
                key not in by_id or by_id[key].fingerprint != value
                for key, value in refs.items()
            ):
                self.candidate = None
                self.retry_after = now + timedelta(days=14)
                self.report = {
                    "status": "evidence_changed",
                    "training_days": 0,
                    "validation_days": 0,
                }
                return
        if self.candidate is None:
            training = [
                case for case in cases if case.target_date >= today - timedelta(days=60)
            ][-30:]
            self.report = {
                "status": "insufficient_training",
                "training_days": len(training),
                "validation_days": 0,
            }
            if len(training) < 30 or (self.retry_after and now < self.retry_after):
                return
            coefficient = min(
                COEFFICIENTS,
                key=lambda value: (
                    metrics(training, value, self.threshold)["time_mae_minutes"],
                    abs(value),
                    value,
                ),
            )
            if coefficient == 0:
                self.report["status"] = "no_training_benefit"
                self.retry_after = now + timedelta(days=14)
                return
            self.candidate = {
                "id": uuid4().hex,
                "coefficient": coefficient,
                "learned_at": now.isoformat(),
                "training": {case.record_id: case.fingerprint for case in training},
                "validation": {},
            }
        candidate = self.candidate
        validation = [
            c
            for c in cases
            if c.trial_id == candidate["id"]
            and c.cutoff > instant(candidate["learned_at"])
            and c.target_date >= today - timedelta(days=28)
        ][-14:]
        if len(validation) < 14 and now > instant(candidate["learned_at"]) + timedelta(
            days=28
        ):
            self.report = {
                "status": "expired",
                "training_days": 30,
                "validation_days": len(validation),
            }
            self.candidate = None
            self.retry_after = now + timedelta(days=14)
            return
        base = metrics(validation, 0, self.threshold)
        trial = metrics(validation, candidate["coefficient"], self.threshold)
        candidate["validation"] = {c.record_id: c.fingerprint for c in validation}
        status = (
            "approved"
            if approved(base, trial)
            else ("rejected" if len(validation) >= 14 else "validating")
        )
        self.report = {
            "status": status,
            "training_days": len(candidate["training"]),
            "validation_days": len(validation),
            "raw": metrics(
                [replace(case, global_factor=1.0) for case in validation],
                0,
                self.threshold,
            ),
            "baseline": base,
            "candidate": trial,
            "candidate_id": candidate["id"],
            "coefficient": candidate["coefficient"],
            "learned_at": candidate["learned_at"],
        }
        if status == "rejected":
            self.candidate = None
            self.retry_after = now + timedelta(days=14)

    @property
    def effective_coefficient(self) -> float:
        return (
            float(self.candidate["coefficient"])
            if self.candidate and self.report["status"] == "approved"
            else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "began": self.began.isoformat(),
            "threshold": self.threshold,
            "candidate": self.candidate,
            "retry_after": self.retry_after.isoformat() if self.retry_after else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MorningLearning:
        result = cls(instant(data["began"]), data["threshold"])
        result.retry_after = (
            instant(data["retry_after"]) if data["retry_after"] else None
        )
        candidate = data["candidate"]
        if candidate is not None:
            if (
                not isinstance(candidate, dict)
                or candidate["coefficient"] not in COEFFICIENTS
                or type(candidate["coefficient"]) is bool
                or not isinstance(candidate["id"], str)
                or not candidate["id"]
                or instant(candidate["learned_at"]) < result.began
                or len(candidate["training"]) != 30
                or len(candidate["validation"]) > 14
                or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for refs in (candidate["training"], candidate["validation"])
                    for k, v in refs.items()
                )
            ):
                raise ValueError("Ungültiger Morgen-Lernzustand")
            result.candidate = candidate
        return result
