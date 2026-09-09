"""Begrenztes Anlagenlernen mit tatsächlich nachfolgenden, getrennten Prüftagen."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from math import ceil, fsum, isfinite
from typing import Any
from zoneinfo import ZoneInfo

from .calculations import calibrated_energy, fit_calibration_factor
from .models import ForecastCalibrationBasis

RULE_VERSION = 1
TRAINING_DAYS = 30
VALIDATION_DAYS = 14
TRAINING_WINDOW_DAYS = 60
VALIDATION_WINDOW_DAYS = 28
RETRY_DAYS = 14


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Lernzeitpunkte benötigen eine eindeutige Zeitzone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CalibrationDay:
    """Ein eingefrorener Vortagesstand mit seiner aktuellen Messbewertung."""

    record_id: str
    target_date: date
    start: datetime
    end: datetime
    cutoff: datetime
    observed_at: datetime
    configuration_id: str
    basis: ForecastCalibrationBasis | None
    raw_energy_kwh: float
    actual_energy_kwh: float | None
    valid: bool
    quality_flags: tuple[str, ...]
    evidence_fingerprint: str
    candidate_id: str | None = None
    candidate_energy_kwh: float | None = None


@dataclass(frozen=True, slots=True)
class CalibrationReference:
    """Die genaue Messrevision eines verwendeten Tages ohne eigene Messkopie."""

    record_id: str
    target_date: date
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "target_date": self.target_date.isoformat(),
            "evidence_fingerprint": self.evidence_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CalibrationReference:
        if any(
            not isinstance(data[key], str) or not data[key]
            for key in ("record_id", "evidence_fingerprint")
        ):
            raise ValueError("Ein Lernbeleg benötigt seine ursprüngliche Messrevision")
        return cls(
            data["record_id"],
            date.fromisoformat(data["target_date"]),
            data["evidence_fingerprint"],
        )


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    """Ein fester Faktor ohne Nachtraining anhand seiner späteren Prüftage."""

    candidate_id: str
    factor: float
    created_at: datetime
    training_references: tuple[CalibrationReference, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "factor": self.factor,
            "created_at": self.created_at.isoformat(),
            "training_references": [
                item.to_dict() for item in self.training_references
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CalibrationCandidate:
        references = tuple(
            CalibrationReference.from_dict(item) for item in data["training_references"]
        )
        factor = data["factor"]
        if (
            not isinstance(data["candidate_id"], str)
            or not data["candidate_id"]
            or isinstance(factor, bool)
            or not isinstance(factor, int | float)
            or not isfinite(factor)
            or not 0.5 <= factor <= 1.5
            or abs(factor * 100 - round(factor * 100)) > 1e-8
            or len(references) != TRAINING_DAYS
            or len({item.record_id for item in references}) != len(references)
            or len({item.target_date for item in references}) != len(references)
        ):
            raise ValueError("Der gespeicherte Lernkandidat ist ungültig")
        return cls(
            data["candidate_id"],
            float(factor),
            _utc(datetime.fromisoformat(data["created_at"])),
            references,
        )


def evaluate_calibration(
    raw: Sequence[float], calibrated: Sequence[float], actual: Sequence[float]
) -> dict[str, Any]:
    """Beide Varianten auf genau denselben Tagen nach festen Nutzenregeln prüfen.

    Das 90%-Quantil verwendet den nächsten Rang (ceil(0,9 * Stichprobe)).
    Große Fehler bleiben vollständig in beiden Kennzahlen enthalten.
    """

    if not raw or len(raw) != len(calibrated) or len(raw) != len(actual):
        raise ValueError("Die Qualitätsprüfung benötigt dieselben nichtleeren Prüftage")
    if any(
        not isfinite(value) or value < 0
        for values in (raw, calibrated, actual)
        for value in values
    ):
        raise ValueError("Die Qualitätsprüfung benötigt endliche Tagesenergien")
    raw_errors = [
        abs(prediction - observed)
        for prediction, observed in zip(raw, actual, strict=True)
    ]
    corrected_errors = [
        abs(prediction - observed)
        for prediction, observed in zip(calibrated, actual, strict=True)
    ]
    raw_mae = fsum(value / len(raw) for value in raw_errors)
    corrected_mae = fsum(value / len(raw) for value in corrected_errors)
    rank = ceil(0.9 * len(raw)) - 1
    raw_p90, corrected_p90 = sorted(raw_errors)[rank], sorted(corrected_errors)[rank]
    gain = (1 - corrected_mae / raw_mae) * 100 if raw_mae > 0 else None
    reasons = []
    if raw_mae <= 0 or corrected_mae > 0.95 * raw_mae:
        reasons.append("insufficient_improvement")
    if corrected_p90 > 1.05 * raw_p90:
        reasons.append("large_errors_worse")
    return {
        "approved": not reasons,
        "raw_mae_kwh": raw_mae,
        "calibrated_mae_kwh": corrected_mae,
        "raw_p90_kwh": raw_p90,
        "calibrated_p90_kwh": corrected_p90,
        "improvement_percent": gain,
        "reasons": reasons,
    }


class CalibrationState:
    """Deterministischer Lernzustand; alle Daten kommen aus dem bestehenden Archiv."""

    def __init__(
        self, configuration_id: str, segment_start: datetime, timezone: str = "UTC"
    ) -> None:
        if not configuration_id:
            raise ValueError("Ein Lernsegment benötigt eine Konfigurationskennung")
        self.configuration_id = configuration_id
        self.segment_start = _utc(segment_start)
        self.timezone = ZoneInfo(timezone)
        self.status = "learning"
        self.candidate: CalibrationCandidate | None = None
        self.validation_references: tuple[CalibrationReference, ...] = ()
        self.first_validation_date: date | None = None
        self.next_attempt_at: datetime | None = None
        self.last_updated_at: datetime | None = None
        self.training_days = 0
        self.validation_days = 0
        self.reasons: list[str] = ["insufficient_training_days"]
        self.exclusion_reasons: dict[str, int] = {}
        self.metrics: dict[str, Any] = {}

    @property
    def approved_factor(self) -> float:
        return (
            self.candidate.factor
            if self.status == "approved" and self.candidate
            else 1.0
        )

    @property
    def candidate_for_capture(self) -> CalibrationCandidate | None:
        return self.candidate if self.status in ("testing", "approved") else None

    def _base_reason(
        self, day: CalibrationDay, excluded: set[date], now: datetime
    ) -> str | None:
        if day.configuration_id != self.configuration_id:
            return "configuration_changed"
        if day.target_date in excluded:
            return "known_curtailment"
        if (
            day.end > now
            or day.observed_at < self.segment_start
            or day.cutoff < self.segment_start
            or day.observed_at > day.cutoff
            or not day.valid
            or day.actual_energy_kwh is None
            or not day.evidence_fingerprint
            or not isfinite(day.actual_energy_kwh)
            or day.actual_energy_kwh < 0
            or not isfinite(day.raw_energy_kwh)
            or day.raw_energy_kwh < 0
        ):
            return "measurements_incomplete"
        if day.basis is None:
            return "missing_basis"
        if day.quality_flags:
            return "weather_quality"
        return None

    def _training_reason(
        self, day: CalibrationDay, excluded: set[date], now: datetime
    ) -> str | None:
        if reason := self._base_reason(day, excluded, now):
            return reason
        uncut = fsum(
            interval.dc_power_kw
            * (interval.end - interval.start).total_seconds()
            / 3600
            for interval in day.basis.intervals
        )
        if uncut < 0.1:
            return "insufficient_energy"
        if calibrated_energy(day.basis, 1.0) < 0.9 * uncut:
            return "strong_clipping"
        return None

    @staticmethod
    def _reference(day: CalibrationDay) -> CalibrationReference:
        return CalibrationReference(
            day.record_id, day.target_date, day.evidence_fingerprint
        )

    @staticmethod
    def _extreme(day: CalibrationDay) -> bool:
        return (
            day.actual_energy_kwh > 0
            if day.raw_energy_kwh == 0
            else not 0.2 <= day.actual_energy_kwh / day.raw_energy_kwh <= 2.0
        )

    def _abort(self, reason: str, now: datetime) -> None:
        self.status = "invalidated"
        self.reasons = [reason]
        self.next_attempt_at = now + timedelta(days=RETRY_DAYS)

    def update(
        self,
        days: Iterable[CalibrationDay],
        now: datetime,
        configuration_id: str,
        segment_start: datetime,
        excluded_dates: Iterable[date | str] = (),
    ) -> bool:
        """Lernen, zukünftige Prüfungen und späteren Nutzen gemeinsam fortführen."""

        now, segment_start = _utc(now), _utc(segment_start)
        before = self.to_dict()
        if self.last_updated_at is not None and now < self.last_updated_at:
            return False
        if (
            configuration_id != self.configuration_id
            or segment_start != self.segment_start
        ):
            self.__init__(configuration_id, segment_start, self.timezone.key)
        local_today = now.astimezone(self.timezone).date()
        excluded = {
            date.fromisoformat(value) if isinstance(value, str) else value
            for value in excluded_dates
        }
        # Alte Standort-/Zeitzonenkontexte bleiben im Archiv erhalten. Gleiche
        # lokale Zieltage daraus sind keine doppelten Tage der aktiven Anlage.
        ordered = sorted(
            (day for day in days if day.configuration_id == self.configuration_id),
            key=lambda day: (day.target_date, day.record_id),
        )
        by_id = {day.record_id: day for day in ordered}
        if len(by_id) != len(ordered) or len(
            {day.target_date for day in ordered}
        ) != len(ordered):
            raise ValueError(
                "Das Anlagenlernen benötigt genau einen Vortagesstand je Tag"
            )
        self.last_updated_at = now

        if self.candidate_for_capture:
            references = (
                *self.candidate.training_references,
                *self.validation_references,
            )
            for reference in references:
                day = by_id.get(reference.record_id)
                if (
                    day is None
                    or day.evidence_fingerprint != reference.evidence_fingerprint
                    or day.target_date != reference.target_date
                    or self._base_reason(day, excluded, now) is not None
                ):
                    self._abort("evidence_changed", now)
                    return before != self.to_dict()

        if (
            self.status in ("rejected", "invalidated")
            and self.next_attempt_at
            and now < self.next_attempt_at
        ):
            return before != self.to_dict()
        if self.status in ("rejected", "invalidated"):
            self.status = "learning"
            self.candidate = None
            self.validation_references = ()
            self.first_validation_date = None
            self.validation_days = 0
            self.metrics = {}

        if self.status == "learning":
            eligible = []
            exclusions: Counter[str] = Counter()
            for day in ordered:
                if (
                    not local_today - timedelta(days=TRAINING_WINDOW_DAYS)
                    <= day.target_date
                    < local_today
                ):
                    continue
                if reason := self._training_reason(day, excluded, now):
                    exclusions[reason] += 1
                else:
                    eligible.append(day)
            self.training_days = min(TRAINING_DAYS, len(eligible))
            self.exclusion_reasons = dict(sorted(exclusions.items()))
            self.reasons = ["insufficient_training_days"]
            if any(self._extreme(day) for day in eligible):
                self.reasons.append("extreme_residual")
            if len(eligible) < TRAINING_DAYS:
                return before != self.to_dict()
            selected = eligible[-TRAINING_DAYS:]
            factor = fit_calibration_factor(
                [(day.basis, day.actual_energy_kwh) for day in selected]
            )
            references = tuple(self._reference(day) for day in selected)
            identity = json.dumps(
                [
                    self.configuration_id,
                    now.isoformat(),
                    factor,
                    [item.to_dict() for item in references],
                ],
                sort_keys=True,
            )
            self.candidate = CalibrationCandidate(
                sha256(identity.encode()).hexdigest(), factor, now, references
            )
            first = local_today + timedelta(days=1)
            if (
                datetime.combine(
                    first - timedelta(days=1), time(18), self.timezone
                ).astimezone(UTC)
                <= now
            ):
                first += timedelta(days=1)
            self.first_validation_date = first
            self.next_attempt_at = None
            self.status = "testing"

        candidate = self.candidate
        training_extreme = any(
            self._extreme(by_id[reference.record_id])
            for reference in candidate.training_references
        )
        selected = []
        exclusions = Counter()
        # Auch die erste Prüfung nach einer Pause benötigt aktuelle Belege.
        first = max(
            self.first_validation_date,
            local_today - timedelta(days=VALIDATION_WINDOW_DAYS),
        )
        deadline_day = self.first_validation_date + timedelta(
            days=VALIDATION_WINDOW_DAYS
        )
        for day in ordered:
            if day.target_date < first or day.target_date >= local_today:
                continue
            if self.status == "testing" and day.target_date >= deadline_day:
                continue
            if reason := self._base_reason(day, excluded, now):
                exclusions[reason] += 1
                continue
            if (
                day.cutoff <= candidate.created_at
                or day.observed_at < candidate.created_at
                or day.candidate_id != candidate.candidate_id
                or day.candidate_energy_kwh is None
                or not isfinite(day.candidate_energy_kwh)
                or day.candidate_energy_kwh < 0
            ):
                exclusions["candidate_forecast_missing"] += 1
                continue
            selected.append(day)
        self.validation_references = tuple(
            self._reference(day) for day in selected[-VALIDATION_WINDOW_DAYS:]
        )
        self.validation_days = min(VALIDATION_DAYS, len(selected))
        self.exclusion_reasons = dict(sorted(exclusions.items()))
        self.reasons = ["insufficient_validation_days"]
        if training_extreme or any(self._extreme(day) for day in selected):
            self.reasons.append("extreme_residual")
        if len(selected) < VALIDATION_DAYS:
            deadline = datetime.combine(
                deadline_day, time.min, self.timezone
            ).astimezone(UTC)
            if self.status == "approved":
                self._abort("insufficient_validation_days", now)
            elif now >= deadline:
                self.status = "rejected"
                self.reasons = ["validation_expired"]
                self.next_attempt_at = now + timedelta(days=RETRY_DAYS)
            return before != self.to_dict()
        evaluation = selected[-VALIDATION_DAYS:]
        self.metrics = evaluate_calibration(
            [day.raw_energy_kwh for day in evaluation],
            [day.candidate_energy_kwh for day in evaluation],
            [day.actual_energy_kwh for day in evaluation],
        )
        self.reasons = list(self.metrics["reasons"])
        if training_extreme or any(self._extreme(day) for day in evaluation):
            self.reasons.append("extreme_residual")
        self.status = "approved" if self.metrics["approved"] else "rejected"
        if self.status == "rejected":
            self.next_attempt_at = now + timedelta(days=RETRY_DAYS)
        return before != self.to_dict()

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_factor": self.candidate.factor if self.candidate else None,
            "training_days": self.training_days,
            "validation_days": self.validation_days,
            "last_learned_at": (
                self.candidate.created_at.isoformat() if self.candidate else None
            ),
            "raw_mae_kwh": self.metrics.get("raw_mae_kwh"),
            "calibrated_mae_kwh": self.metrics.get("calibrated_mae_kwh"),
            "raw_p90_kwh": self.metrics.get("raw_p90_kwh"),
            "calibrated_p90_kwh": self.metrics.get("calibrated_p90_kwh"),
            "improvement_percent": self.metrics.get("improvement_percent"),
            "reasons": list(self.reasons),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_version": RULE_VERSION,
            "configuration_id": self.configuration_id,
            "segment_start": self.segment_start.isoformat(),
            "timezone": self.timezone.key,
            "status": self.status,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "validation_references": [
                item.to_dict() for item in self.validation_references
            ],
            "first_validation_date": (
                self.first_validation_date.isoformat()
                if self.first_validation_date
                else None
            ),
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "last_updated_at": (
                self.last_updated_at.isoformat() if self.last_updated_at else None
            ),
            "training_days": self.training_days,
            "validation_days": self.validation_days,
            "reasons": list(self.reasons),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "metrics": dict(self.metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CalibrationState:
        """Nur den bekannten begrenzten Lernvertrag aus dem getrennten Store laden."""

        try:
            if data["rule_version"] != RULE_VERSION:
                raise ValueError("Unbekannte Version der Lernregeln")
            state = cls(
                data["configuration_id"],
                _utc(datetime.fromisoformat(data["segment_start"])),
                data["timezone"],
            )
            state.status = data["status"]
            state.candidate = (
                CalibrationCandidate.from_dict(data["candidate"])
                if data["candidate"]
                else None
            )
            state.validation_references = tuple(
                CalibrationReference.from_dict(item)
                for item in data["validation_references"]
            )
            state.first_validation_date = (
                date.fromisoformat(data["first_validation_date"])
                if data["first_validation_date"]
                else None
            )
            state.next_attempt_at = (
                _utc(datetime.fromisoformat(data["next_attempt_at"]))
                if data["next_attempt_at"]
                else None
            )
            state.last_updated_at = (
                _utc(datetime.fromisoformat(data["last_updated_at"]))
                if data["last_updated_at"]
                else None
            )
            state.training_days = data["training_days"]
            state.validation_days = data["validation_days"]
            state.reasons = list(data["reasons"])
            state.exclusion_reasons = dict(data["exclusion_reasons"])
            state.metrics = dict(data["metrics"])
            if (
                state.status
                not in ("learning", "testing", "approved", "rejected", "invalidated")
                or not isinstance(state.training_days, int)
                or not 0 <= state.training_days <= TRAINING_DAYS
                or not isinstance(state.validation_days, int)
                or not 0 <= state.validation_days <= VALIDATION_DAYS
                or len(state.validation_references) > VALIDATION_WINDOW_DAYS
                or len({item.record_id for item in state.validation_references})
                != len(state.validation_references)
                or any(not isinstance(reason, str) for reason in state.reasons)
                or (
                    state.status != "learning"
                    and (state.candidate is None or state.first_validation_date is None)
                )
                or (
                    state.status == "approved"
                    and (
                        state.validation_days < VALIDATION_DAYS
                        or state.metrics.get("approved") is not True
                    )
                )
                or (
                    state.candidate is not None
                    and state.candidate.created_at < state.segment_start
                )
                or any(
                    value is not None
                    and not isinstance(value, bool | list)
                    and (not isinstance(value, int | float) or not isfinite(value))
                    for value in state.metrics.values()
                )
            ):
                raise ValueError("Der gespeicherte Lernzustand ist inkonsistent")
            return state
        except (KeyError, TypeError, AttributeError, OverflowError) as err:
            raise ValueError("Der gespeicherte Lernzustand ist beschädigt") from err
