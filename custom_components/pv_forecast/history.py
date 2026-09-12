"""Rechtzeitig beobachtete Prognosen und nachvollziehbare lokale Bewertungen."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from itertools import pairwise
from math import fsum, isclose, isfinite
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from .calculations import calibrated_energy, forecast_basis
from .measurements import SourceConfig
from .models import ForecastCalibrationBasis, ForecastResult, TotalForecastInterval
from .short_term import build_trial, validate_trial
from .temperature_comparison import (
    MODEL as TEMPERATURE_MODEL,
)
from .temperature_comparison import (
    parameter_id,
    validate_comparison,
)
from .underperformance import discard_invalid_references, empty_state, validate_state

if TYPE_CHECKING:
    from .morning import MorningState

type Horizon = Literal[
    "daily_previous_18", "daily_same_06", "hourly_1h", "hourly_3h", "daily_remaining_12"
]
HORIZONS: tuple[Horizon, ...] = (
    "daily_previous_18",
    "daily_same_06",
    "hourly_1h",
    "hourly_3h",
    "daily_remaining_12",
)
MODEL_VERSION = "1"
MAX_RECORDS = 6000
MAX_BYTES = 32 * 1024 * 1024
MAX_REVISIONS = 3
HOUR = timedelta(hours=1)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Archivzeitpunkte benötigen eine eindeutige Zeitzone")
    return value.astimezone(UTC)


def _timestamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _energy(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Archivenergie muss eine endliche nichtnegative Zahl sein")
    try:
        result = float(value)
    except OverflowError as err:
        raise ValueError("Archivenergie ist nicht darstellbar") from err
    if not isfinite(result) or result < 0:
        raise ValueError("Archivenergie muss endlich und nichtnegativ sein")
    return result


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError("Archivmarkierungen müssen eine Zeichenkettenliste sein")
    return tuple(value)


def _factor(value: object) -> float:
    result = _energy(value)
    if not 0.5 <= result <= 1.5:
        raise ValueError(
            "Der archivierte Anlagenfaktor muss zwischen 0,5 und 1,5 liegen"
        )
    return result


def _candidate_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Der archivierte Kandidat benötigt eine Identität")
    return value


def _morning_factor(value: object) -> float:
    result = _energy(value)
    if not 0.8 <= result <= 1.2:
        raise ValueError(
            "Der archivierte Morgenfaktor muss zwischen 0,8 und 1,2 liegen"
        )
    return result


def _morning_from_dict(value: object) -> dict[str, Any] | None:
    """Nur die damals beobachtete Morgenwirkung ohne neue Berechnung übernehmen."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "factor",
        "candidate_id",
        "energy_kwh",
    }:
        raise ValueError("Die archivierte Morgenwirkung ist unvollständig")
    return {
        "factor": _morning_factor(value["factor"]),
        "candidate_id": _candidate_id(value["candidate_id"]),
        "energy_kwh": _energy(value["energy_kwh"]),
    }


def _day_bounds(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, timezone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, timezone).astimezone(UTC),
    )


def _record_id(horizon: Horizon, start: datetime, end: datetime) -> str:
    return sha256(
        f"{horizon}|{start.isoformat()}|{end.isoformat()}".encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ComparisonForecast:
    """Am gemeinsamen Stichtag lokal beobachtete fremde Tagesprognose."""

    energy_kwh: float
    entity_id: str
    registry_id: str | None
    scope: str
    reported_at: datetime
    observed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "energy_kwh": self.energy_kwh,
            "entity_id": self.entity_id,
            "registry_id": self.registry_id,
            "scope": self.scope,
            "reported_at": self.reported_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ComparisonForecast:
        entity_id, registry_id, scope = (
            data["entity_id"],
            data.get("registry_id"),
            data["scope"],
        )
        if (
            not isinstance(entity_id, str)
            or not entity_id.startswith("sensor.")
            or (
                registry_id is not None
                and (not isinstance(registry_id, str) or not registry_id)
            )
            or not isinstance(scope, str)
            or not scope.strip()
        ):
            raise ValueError("Die fremde Prognose benötigt eine erklärte Messgrenze")
        reported, observed = _timestamp(data["reported_at"]), _timestamp(
            data["observed_at"]
        )
        if not timedelta(0) <= observed - reported <= 2 * HOUR:
            raise ValueError("Die fremde Prognose ist nicht rechtzeitig beobachtet")
        return cls(
            _energy(data["energy_kwh"]),
            entity_id,
            registry_id,
            scope,
            reported,
            observed,
        )


@dataclass(frozen=True, slots=True)
class SourceAssessment:
    """Begrenzte Messkopie einer Quelle mit den tatsächlich verwendeten Segmenten."""

    source_id: str
    energy_kwh: float | None
    segment_ids: tuple[str, ...]
    quality_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "energy_kwh": self.energy_kwh,
            "segment_ids": list(self.segment_ids),
            "quality_flags": list(self.quality_flags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceAssessment:
        if not isinstance(data["source_id"], str) or not data["source_id"]:
            raise ValueError("Ungültige archivierte Messquelle")
        return cls(
            data["source_id"],
            _energy(data["energy_kwh"]) if data["energy_kwh"] is not None else None,
            _strings(data["segment_ids"]),
            _strings(data["quality_flags"]),
        )


@dataclass(frozen=True, slots=True)
class Assessment:
    """Eine datierte Bewertung; Änderungen werden als neue Revision bewahrt."""

    assessed_at: datetime
    actual_energy_kwh: float | None
    valid: bool
    reasons: tuple[str, ...]
    sources: tuple[SourceAssessment, ...]
    manual: bool = False

    @property
    def has_derived_gap(self) -> bool:
        """Auch alte Messkopien können eine unbelegte Integralmenge dokumentieren."""
        return not self.manual and any(
            {"derived_energy", "gap"} <= set(source.quality_flags)
            for source in self.sources
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessed_at": self.assessed_at.isoformat(),
            "actual_energy_kwh": self.actual_energy_kwh,
            "valid": self.valid,
            "reasons": list(self.reasons),
            "sources": [item.to_dict() for item in self.sources],
            **({"manual": True} if self.manual else {}),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Assessment:
        actual = (
            _energy(data["actual_energy_kwh"])
            if data["actual_energy_kwh"] is not None
            else None
        )
        reasons = _strings(data["reasons"])
        manual = data.get("manual", False)
        if (
            not isinstance(manual, bool)
            or not isinstance(data["valid"], bool)
            or (data["valid"] and (actual is None or reasons))
            or (not data["valid"] and actual is not None)
            or (manual and (not data["valid"] or data["sources"]))
        ):
            raise ValueError("Die archivierte Bewertung ist inkonsistent")
        sources = tuple(SourceAssessment.from_dict(item) for item in data["sources"])
        if len({item.source_id for item in sources}) != len(sources):
            raise ValueError("Archivierte Messquellen sind doppelt vorhanden")
        return cls(
            _timestamp(data["assessed_at"]),
            actual,
            data["valid"],
            reasons,
            sources,
            manual,
        )


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    """Unveränderlicher Prognosestand; nur die Bewertung wird explizit revisioniert."""

    record_id: str
    start: datetime
    end: datetime
    target_date: date
    horizon: Horizon
    cutoff: datetime
    fetched_at: datetime
    observed_at: datetime
    timezone: str
    configuration_id: str
    measurement_sources: tuple[SourceConfig, ...]
    raw_energy_kwh: float
    quality_flags: tuple[str, ...]
    comparison: ComparisonForecast | None = None
    assessment: Assessment | None = None
    assessment_revisions: tuple[Assessment, ...] = ()
    deleted_sources: tuple[str, ...] = ()
    model_version: str = MODEL_VERSION
    calibrated_energy_kwh: float | None = None
    basis: ForecastCalibrationBasis | None = None
    applied_factor: float | None = None
    applied_candidate_id: str | None = None
    candidate_factor: float | None = None
    candidate_id: str | None = None
    candidate_energy_kwh: float | None = None
    short_term: dict[str, Any] | None = None
    temperature_comparison: dict[str, Any] | None = None
    measured_assessment: Assessment | None = None
    morning: dict[str, Any] | None = None

    @property
    def config_fingerprint(self) -> str:
        return self.configuration_id

    @property
    def effective_energy_kwh(self) -> float:
        """Den tatsächlich wirksamen, am Stichtag beobachteten Stand verwenden."""
        if self.morning is not None:
            return self.morning["energy_kwh"]
        if self.calibrated_energy_kwh is not None:
            return self.calibrated_energy_kwh
        return self.raw_energy_kwh

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "target_date": self.target_date.isoformat(),
            "horizon": self.horizon,
            "cutoff": self.cutoff.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "timezone": self.timezone,
            "config_fingerprint": self.configuration_id,
            "model_version": self.model_version,
            "model_issued_at": None,
            "measurement_sources": [
                source.to_dict() for source in self.measurement_sources
            ],
            "raw_energy_kwh": self.raw_energy_kwh,
            "calibrated_energy_kwh": self.calibrated_energy_kwh,
            "basis": self.basis.to_dict() if self.basis else None,
            "applied_factor": self.applied_factor,
            "applied_candidate_id": self.applied_candidate_id,
            "candidate_factor": self.candidate_factor,
            "candidate_id": self.candidate_id,
            "candidate_energy_kwh": self.candidate_energy_kwh,
            "short_term": self.short_term,
            "temperature_comparison": self.temperature_comparison,
            "morning": deepcopy(self.morning),
            "quality_flags": list(self.quality_flags),
            "comparison": self.comparison.to_dict() if self.comparison else None,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "assessment_revisions": [
                item.to_dict() for item in self.assessment_revisions
            ],
            **(
                {
                    "measured_assessment": (
                        self.measured_assessment.to_dict()
                        if self.measured_assessment
                        else None
                    )
                }
                if self.assessment and self.assessment.manual
                else {}
            ),
            "deleted_sources": list(self.deleted_sources),
        }


class HistoryArchive:
    """Opt-in-Archiv ohne nachträgliche Prognosebeschaffung oder Trainingsfunktion."""

    def __init__(self, timezone: str, *, morning: MorningState | None = None) -> None:
        self.timezone = ZoneInfo(timezone)
        self.records: dict[str, ArchiveRecord] = {}
        self.retention_truncated = False
        self.underperformance = empty_state()
        self.morning = morning
        self._configuration_changes: list[tuple[datetime, str]] = []
        self._latest_observed_at: datetime | None = None
        # Ausschließlich entkoppelte, danach unveränderte Speicherbausteine.
        # Laufende Bewertungen ersetzen Records; ausstehende Schreibungen
        # behalten damit ihre eigene alte Generation bis zum Dateischluss.
        self._storage_records: dict[str, tuple[ArchiveRecord, dict[str, Any], int]] = {}

    def note_configuration(self, configuration_id: str, observed_at: datetime) -> bool:
        """Reale Konfigurationswechsel begrenzen die vergleichbare Messperiode."""
        observed_at = _utc(observed_at)
        if not isinstance(configuration_id, str) or not configuration_id:
            raise ValueError("Eine Konfiguration benötigt einen Fingerprint")
        if (
            self._latest_observed_at is not None
            and observed_at < self._latest_observed_at
        ):
            return False
        self._latest_observed_at = observed_at
        if self._configuration_changes:
            last_time, last_id = self._configuration_changes[-1]
            if last_id == configuration_id:
                return False
            if observed_at < last_time:
                raise ValueError(
                    "Konfigurationswechsel dürfen nicht rückdatiert werden"
                )
        self._configuration_changes.append((observed_at, configuration_id))
        return True

    def _configuration_valid(self, record: ArchiveRecord) -> bool:
        previous = [
            item for item in self._configuration_changes if item[0] <= record.start
        ]
        if previous and previous[-1][1] != record.configuration_id:
            return False
        # Der erste beobachtete Stand belegt noch keinen vorherigen Wechsel.
        return not any(
            record.start < instant < record.end
            for instant, _ in self._configuration_changes[1:]
        )

    def capture(
        self,
        forecast: ForecastResult,
        fetched_at: datetime,
        observed_at: datetime,
        configuration_id: str,
        measurement_sources: Sequence[SourceConfig | Mapping[str, Any]],
        comparison: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        inverter_max_power_kw: float | None = None,
        applied_factor: float = 1.0,
        applied_candidate_id: str | None = None,
        trial_factor: float | None = None,
        trial_candidate_id: str | None = None,
        short_term_enabled: bool = False,
        excluded_dates: set[date] | None = None,
        temperature_forecast: ForecastResult | None = None,
        temperature_mountings: dict[str, str] | None = None,
        morning_forecast: ForecastResult | None = None,
        morning_factor: float = 1.0,
        morning_candidate_id: str | None = None,
    ) -> bool:
        """Nur vorab definierte und rechtzeitig beobachtete Stände auswählen."""
        fetched_at, observed_at = _utc(fetched_at), _utc(observed_at)
        if fetched_at > observed_at:
            raise ValueError("Eine Prognose kann nicht vor ihrem Abruf beobachtet sein")
        applied_factor = _factor(applied_factor)
        if applied_candidate_id is not None:
            applied_candidate_id = _candidate_id(applied_candidate_id)
        elif applied_factor != 1:
            raise ValueError("Ein wirksamer Anlagenfaktor benötigt seinen Kandidaten")
        if (trial_factor is None) != (trial_candidate_id is None):
            raise ValueError("Ein Prüfstand benötigt Faktor und Kandidatenidentität")
        if trial_factor is not None:
            trial_factor = _factor(trial_factor)
            trial_candidate_id = _candidate_id(trial_candidate_id)
        if morning_forecast is None:
            if morning_factor != 1 or morning_candidate_id is not None:
                raise ValueError("Eine Morgenwirkung benötigt ihre wirksame Zeitreihe")
        else:
            morning_factor = _morning_factor(morning_factor)
            morning_candidate_id = _candidate_id(morning_candidate_id)
            if (
                morning_forecast.local_date != forecast.local_date
                or morning_forecast.forecast_days != forecast.forecast_days
            ):
                raise ValueError("Die Morgenwirkung gehört zu einem anderen Horizont")
        if (
            self._latest_observed_at is not None
            and observed_at < self._latest_observed_at
        ):
            return False
        sources = tuple(
            (
                source
                if isinstance(source, SourceConfig)
                else SourceConfig.from_dict(source)
            )
            for source in measurement_sources
        )
        sources = tuple(source for source in sources if source.kind != "power")
        if (
            len({item.source_id for item in sources}) != len(sources)
            or len({item.entity_id for item in sources}) != len(sources)
            or len({item.registry_id or item.entity_id for item in sources})
            != len(sources)
        ):
            raise ValueError("Eine Prognose darf Messquellen nicht doppelt zuordnen")
        changed = self.note_configuration(configuration_id, observed_at)
        intervals = tuple(
            sorted(forecast.total_intervals, key=lambda item: _utc(item.start))
        )
        excluded_dates = excluded_dates or set()
        for offset in (0, 1):
            day = forecast.local_date + timedelta(days=offset)
            start, end = _day_bounds(day, self.timezone)
            checkpoints = [
                (
                    "daily_previous_18",
                    datetime.combine(day - timedelta(days=1), time(18), self.timezone),
                ),
                ("daily_same_06", datetime.combine(day, time(6), self.timezone)),
            ]
            if short_term_enabled:
                checkpoints.append(
                    (
                        "daily_remaining_12",
                        datetime.combine(day, time(12), self.timezone),
                    )
                )
            for horizon, cutoff in checkpoints:
                window_start = (
                    _utc(cutoff) if horizon == "daily_remaining_12" else start
                )
                window_energy, window_flags = _forecast_window(
                    intervals, window_start, end
                )
                if window_energy is None:
                    continue
                existing = None
                if (
                    horizon != "daily_remaining_12"
                    and comparison
                    and day.isoformat() in comparison
                ):
                    try:
                        candidate = ComparisonForecast.from_dict(
                            comparison[day.isoformat()]
                        )
                        if candidate.observed_at == observed_at:
                            existing = candidate
                    except (ValueError, KeyError, TypeError):
                        pass
                changed |= self._capture_record(
                    horizon,
                    window_start,
                    end,
                    day,
                    _utc(cutoff),
                    HOUR if horizon == "daily_remaining_12" else 2 * HOUR,
                    fetched_at,
                    observed_at,
                    configuration_id,
                    sources,
                    window_energy,
                    window_flags,
                    existing,
                    forecast,
                    inverter_max_power_kw,
                    applied_factor,
                    applied_candidate_id,
                    trial_factor,
                    trial_candidate_id,
                    short_term_enabled,
                    excluded_dates,
                    temperature_forecast,
                    temperature_mountings,
                    morning_forecast,
                    morning_factor,
                    morning_candidate_id,
                )
        for interval in intervals:
            start, end = _utc(interval.start), _utc(interval.end)
            # Äußere, lokal gekürzte Ränder dürfen keine zweiten Stundenziele bilden.
            if (
                start.minute
                or start.second
                or start.microsecond
                or end - start != HOUR
                or not interval.is_complete
            ):
                continue
            energy = _energy(interval.energy_kwh)
            for horizon, lead in (("hourly_1h", 1), ("hourly_3h", 3)):
                changed |= self._capture_record(
                    horizon,
                    start,
                    end,
                    start.astimezone(self.timezone).date(),
                    start - lead * HOUR,
                    HOUR,
                    fetched_at,
                    observed_at,
                    configuration_id,
                    sources,
                    energy,
                    tuple(interval.quality_flags),
                    None,
                    forecast,
                    inverter_max_power_kw,
                    applied_factor,
                    applied_candidate_id,
                    trial_factor,
                    trial_candidate_id,
                    short_term_enabled,
                    excluded_dates,
                    temperature_forecast,
                    temperature_mountings,
                    morning_forecast,
                    morning_factor,
                    morning_candidate_id,
                )
        return changed

    def _capture_record(
        self,
        horizon: Horizon,
        start: datetime,
        end: datetime,
        target_date: date,
        cutoff: datetime,
        max_age: timedelta,
        fetched_at: datetime,
        observed_at: datetime,
        configuration_id: str,
        sources: tuple[SourceConfig, ...],
        energy: float,
        flags: tuple[str, ...],
        comparison: ComparisonForecast | None,
        forecast: ForecastResult,
        inverter_max_power_kw: float | None,
        applied_factor: float,
        applied_candidate_id: str | None,
        trial_factor: float | None,
        trial_candidate_id: str | None,
        short_term_enabled: bool,
        excluded_dates: set[date],
        temperature_forecast: ForecastResult | None,
        temperature_mountings: dict[str, str] | None,
        morning_forecast: ForecastResult | None,
        morning_factor: float,
        morning_candidate_id: str | None,
    ) -> bool:
        if not cutoff - max_age <= fetched_at <= observed_at <= cutoff:
            return False
        record_id = _record_id(horizon, start, end)
        previous = self.records.get(record_id)
        if previous is not None and fetched_at < previous.fetched_at:
            return False
        basis = forecast_basis(forecast, start, end, inverter_max_power_kw)
        calibrated = (
            calibrated_energy(basis, applied_factor)
            if basis is not None and applied_candidate_id is not None
            else None
        )
        candidate = (
            calibrated_energy(basis, trial_factor)
            if basis is not None and trial_factor is not None
            else None
        )
        morning = None
        if morning_forecast is not None:
            morning_energy, morning_flags = _forecast_window(
                morning_forecast.total_intervals, start, end
            )
            if morning_energy is None:
                return False
            morning = {
                "factor": morning_factor,
                "candidate_id": morning_candidate_id,
                "energy_kwh": morning_energy,
            }
            flags = tuple(sorted({*flags, *morning_flags}))
        record = ArchiveRecord(
            record_id,
            start,
            end,
            target_date,
            horizon,
            cutoff,
            fetched_at,
            observed_at,
            self.timezone.key,
            configuration_id,
            sources,
            energy,
            flags,
            comparison,
            calibrated_energy_kwh=calibrated,
            basis=basis,
            applied_factor=applied_factor if calibrated is not None else None,
            applied_candidate_id=(
                applied_candidate_id if calibrated is not None else None
            ),
            candidate_factor=trial_factor if candidate is not None else None,
            candidate_id=trial_candidate_id if candidate is not None else None,
            candidate_energy_kwh=candidate,
            morning=morning,
        )
        if short_term_enabled and horizon in (
            "hourly_1h",
            "hourly_3h",
            "daily_remaining_12",
        ):
            record = replace(
                record,
                short_term=build_trial(
                    tuple(self.records.values()), record, excluded_dates
                ),
            )
        if temperature_forecast is not None and temperature_mountings:
            alternative_energy, alternative_flags = _forecast_window(
                temperature_forecast.total_intervals, start, end
            )
            if alternative_energy is not None and not alternative_flags and not flags:
                record = replace(
                    record,
                    temperature_comparison={
                        "schema_version": 1,
                        "model": TEMPERATURE_MODEL,
                        "parameter_id": parameter_id(temperature_mountings),
                        "mountings": dict(temperature_mountings),
                        "energy_kwh": alternative_energy,
                    },
                )
        if previous is not None and fetched_at == previous.fetched_at:
            # Ein lokaler Faktorwechsel benötigt keinen neuen Wetterabruf. Derselbe
            # Stand darf nur vor seinem Stichtag neue Kalibrierfelder erhalten.
            if (
                observed_at < previous.observed_at
                or configuration_id != previous.configuration_id
                or energy != previous.raw_energy_kwh
            ) or all(
                getattr(previous, field) == getattr(record, field)
                for field in (
                    "basis",
                    "calibrated_energy_kwh",
                    "applied_factor",
                    "applied_candidate_id",
                    "candidate_factor",
                    "candidate_id",
                    "candidate_energy_kwh",
                    "short_term",
                    "temperature_comparison",
                    "morning",
                )
            ):
                return False
            record = replace(
                previous,
                observed_at=observed_at,
                comparison=record.comparison,
                basis=record.basis,
                calibrated_energy_kwh=record.calibrated_energy_kwh,
                applied_factor=record.applied_factor,
                applied_candidate_id=record.applied_candidate_id,
                candidate_factor=record.candidate_factor,
                candidate_id=record.candidate_id,
                candidate_energy_kwh=record.candidate_energy_kwh,
                short_term=record.short_term,
                temperature_comparison=record.temperature_comparison,
                morning=record.morning,
            )
        self.records[record_id] = record
        return True

    def assess(
        self, record_id: str, evidence: Mapping[str, Any] | None, assessed_at: datetime
    ) -> bool:
        """Vollständige Quellendeltas bewerten, ohne Lücken auf Stunden zu verteilen."""
        assessed_at = _utc(assessed_at)
        record = self.records[record_id]
        if record.assessment and record.assessment.manual:
            # Der ausdrücklich bestätigte Tageswert hat Vorrang vor Rohmessungen.
            return False
        if assessed_at < record.end or record.deleted_sources:
            return False
        if record.assessment and assessed_at < record.assessment.assessed_at:
            raise ValueError("Eine Bewertung darf nicht rückdatiert werden")
        reasons: set[str] = set()
        contributions: list[SourceAssessment] = []
        if record.deleted_sources:
            reasons.add("measurement_source_deleted")
        elif not record.measurement_sources:
            reasons.add("no_measurement_sources")
        elif evidence is None:
            reasons.add("measurements_missing")
        else:
            try:
                if (
                    _timestamp(evidence["start"]) != record.start
                    or _timestamp(evidence["end"]) != record.end
                ):
                    raise ValueError("Die Messung gehört zu einem anderen UTC-Fenster")
                actual_sources = evidence["sources"]
                by_id = {item["source_id"]: item for item in actual_sources}
                if len(by_id) != len(actual_sources):
                    reasons.add("duplicate_measurement_sources")
                for expected in record.measurement_sources:
                    actual = by_id.get(expected.source_id)
                    if actual is None:
                        reasons.add("measurement_source_missing")
                        continue
                    selected_segments = {
                        delta["segment_id"] for delta in actual["deltas"]
                    }
                    flags = set(actual["quality_flags"]) - {"stale", "no_valid_reading"}
                    if "derived_measurement_gap" in flags or any(
                        {"derived_energy", "gap"} <= set(delta["quality_flags"])
                        for delta in actual["deltas"]
                    ):
                        reasons.add("derived_measurement_gap")
                    if actual["energy_complete"] is not True:
                        reasons.add("measurement_incomplete")
                    if not selected_segments:
                        reasons.add("measurement_incomplete")
                    identity_matches = True
                    for segment_id in selected_segments:
                        config = SourceConfig.from_dict(actual["segments"][segment_id])
                        if (
                            config.source_id != expected.source_id
                            or config.measurement_identity
                            != expected.measurement_identity
                        ):
                            reasons.add("measurement_boundary_changed")
                            identity_matches = False
                    value = (
                        _energy(actual["energy_kwh"])
                        if identity_matches and actual["energy_kwh"] is not None
                        else None
                    )
                    if value is None:
                        reasons.add("measurement_incomplete")
                    contributions.append(
                        SourceAssessment(
                            expected.source_id,
                            value,
                            tuple(sorted(selected_segments)),
                            tuple(sorted(flags)),
                        )
                    )
            except (KeyError, TypeError, ValueError, AttributeError):
                reasons.add("measurement_evidence_invalid")
        if not self._configuration_valid(record):
            reasons.add("configuration_changed")
        previous = record.assessment
        affirmative_flags = {
            "daily_correction",
            "counter_decrease",
            "multiple_segments",
            "source_changed",
            "counter_reset",
            "invalid_metadata",
        }
        if (
            previous is not None
            and previous.valid
            and reasons
            and reasons
            <= {
                "measurements_missing",
                "measurement_source_missing",
                "measurement_incomplete",
            }
            and not any(
                affirmative_flags.intersection(item.quality_flags)
                for item in contributions
            )
        ):
            # Eine Rohdatenbegrenzung widerlegt keine früher vollständig belegte Menge.
            return False
        actual_energy = None
        if not reasons:
            try:
                actual_energy = _energy(
                    fsum(
                        item.energy_kwh
                        for item in contributions
                        if item.energy_kwh is not None
                    )
                )
            except (ValueError, OverflowError):
                reasons.add("arithmetic_overflow")
        assessment = Assessment(
            assessed_at,
            actual_energy,
            not reasons,
            tuple(sorted(reasons)),
            tuple(contributions),
        )
        if (
            previous is not None
            and replace(previous, assessed_at=assessed_at) == assessment
        ):
            return False
        revisions = record.assessment_revisions
        if previous is not None:
            revisions = (*revisions, previous)[-MAX_REVISIONS:]
        self.records[record_id] = replace(
            record, assessment=assessment, assessment_revisions=revisions
        )
        return True

    def invalidate_derived_gaps(self, now: datetime) -> bool:
        """Früher irrtümlich bestätigte Integral-Lücken vor dem Lernen korrigieren."""
        now = _utc(now)
        changed = False
        for record_id, record in tuple(self.records.items()):
            previous = record.assessment
            if previous is None or not previous.valid or not previous.has_derived_gap:
                continue
            self.records[record_id] = replace(
                record,
                assessment=replace(
                    previous,
                    assessed_at=max(now, previous.assessed_at),
                    actual_energy_kwh=None,
                    valid=False,
                    reasons=tuple(
                        sorted(set(previous.reasons) | {"derived_measurement_gap"})
                    ),
                ),
                assessment_revisions=(*record.assessment_revisions, previous)[
                    -MAX_REVISIONS:
                ],
            )
            changed = True
        return changed

    def delete_measurement_source(self, source_id: str) -> bool:
        """Messkopien samt Revisionen löschen und ihre Wiedererfassung blockieren."""
        changed = (
            self.morning.delete_measurement_source(source_id)
            if self.morning is not None
            else False
        )
        for record_id, record in tuple(self.records.items()):
            if any(
                source.source_id == source_id for source in record.measurement_sources
            ):
                self.records[record_id] = replace(
                    record,
                    measurement_sources=tuple(
                        source
                        for source in record.measurement_sources
                        if source.source_id != source_id
                    ),
                    assessment=None,
                    assessment_revisions=(),
                    measured_assessment=None,
                    short_term=None,
                    deleted_sources=tuple(sorted({*record.deleted_sources, source_id})),
                )
                changed = True
        if changed:
            discard_invalid_references(self.underperformance, self.records)
        return changed

    def external_sources(self) -> tuple[dict[str, str | None], ...]:
        """Auch historische externe Quellen bleiben für die Leserechte sichtbar."""
        sources = {
            (source.entity_id, source.registry_id)
            for record in self.records.values()
            for source in record.measurement_sources
        }
        sources.update(
            (source.upstream_entity_id, source.upstream_registry_id)
            for record in self.records.values()
            for source in record.measurement_sources
            if source.upstream_registry_id is not None
        )
        sources.update(
            (record.comparison.entity_id, record.comparison.registry_id)
            for record in self.records.values()
            if record.comparison is not None
        )
        if self.morning is not None:
            sources.update(
                (source.entity_id, source.registry_id)
                for source in self.morning.sources
            )
            sources.update(
                (source.upstream_entity_id, source.upstream_registry_id)
                for source in self.morning.sources
                if source.upstream_registry_id is not None
            )
        return tuple(
            {"entity_id": entity, "registry_id": registry}
            for entity, registry in sorted(sources, key=str)
        )

    def day_view(
        self, now: datetime, active_configuration_id: str, **selection
    ) -> dict[str, Any]:
        """Historische Darstellung ausschließlich aus bestehenden Belegen bilden."""
        from .history_view import build_archive_day_view

        return build_archive_day_view(
            tuple(self.records.values()),
            now,
            active_configuration_id,
            self.timezone.key,
            retention_truncated=self.retention_truncated,
            **selection,
        )

    def current_targets(
        self, now: datetime, configuration_id: str | None = None
    ) -> dict[str, Any]:
        """Bereits feste Stundenprognosen für die zwei aktuellen lokalen Tage lesen."""

        now = _utc(now)
        configuration_id = configuration_id or self._active_configuration_id()
        today = now.astimezone(self.timezone).date()
        start, _ = _day_bounds(today, self.timezone)
        _, end = _day_bounds(today + timedelta(days=1), self.timezone)
        days = (
            _day_bounds(today, self.timezone),
            _day_bounds(today + timedelta(days=1), self.timezone),
        )
        records = sorted(
            (
                record
                for record in self.records.values()
                if record.horizon == "hourly_1h"
                and record.configuration_id == configuration_id
                and record.timezone == self.timezone.key
                and record.cutoff <= now
                and record.start < end
                and record.end > start
            ),
            key=lambda record: record.start,
        )
        return {
            "view_version": 1,
            "as_of": now.isoformat(),
            "timezone": self.timezone.key,
            "horizon": "hourly_1h",
            "label": "Jeweils 1 Stunde vorher",
            "intervals": [
                {
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    "energy_kwh": record.effective_energy_kwh
                    * ((right - left) / (record.end - record.start)),
                    "ac_power_kw": record.effective_energy_kwh
                    / ((record.end - record.start).total_seconds() / 3600),
                    "source_start": record.start.isoformat(),
                    "source_end": record.end.isoformat(),
                    "raw_energy_kwh": record.raw_energy_kwh,
                    "calibrated_energy_kwh": record.calibrated_energy_kwh,
                    "applied_factor": record.applied_factor,
                    "applied_candidate_id": record.applied_candidate_id,
                    "morning": deepcopy(record.morning),
                    "quality_flags": list(record.quality_flags),
                    "fetched_at": record.fetched_at.isoformat(),
                    "cutoff": record.cutoff.isoformat(),
                }
                for record in records
                for day_start, day_end in days
                if (right := min(record.end, day_end))
                > (left := max(record.start, day_start))
            ],
        }

    def _summarize_horizons(
        self,
        records: Sequence[ArchiveRecord],
        now: datetime,
        days: int,
        timezone: ZoneInfo,
    ) -> dict[str, Any]:
        """Genau eine Konfiguration in ihrer eigenen Tageszeitzone bewerten."""

        end_day = now.astimezone(timezone).date()
        start_day = end_day - timedelta(days=days)
        horizons = {}
        for horizon in HORIZONS:
            selected = [record for record in records if record.horizon == horizon]
            expected = (
                days
                if horizon.startswith("daily")
                else _expected_hours(start_day, end_day, timezone)
            )
            valid = []
            exclusions: Counter[str] = Counter()
            exclusions["forecast_missing"] = max(0, expected - len(selected))
            for record in selected:
                if record.end > now:
                    exclusions["target_not_finished"] += 1
                elif record.deleted_sources:
                    exclusions["measurement_source_deleted"] += 1
                elif record.assessment is None:
                    exclusions["not_assessed"] += 1
                elif not self._configuration_valid(record):
                    exclusions["configuration_changed"] += 1
                elif not record.assessment.valid:
                    exclusions.update(record.assessment.reasons)
                else:
                    valid.append(record)
            errors = [
                record.raw_energy_kwh - record.assessment.actual_energy_kwh
                for record in valid
            ]
            paired = [record for record in valid if record.comparison is not None]
            calibrated = [
                record for record in valid if record.calibrated_energy_kwh is not None
            ]
            morning = [record for record in valid if record.morning is not None]
            morning_baseline_errors = [
                (
                    record.calibrated_energy_kwh
                    if record.calibrated_energy_kwh is not None
                    else record.raw_energy_kwh
                )
                - record.assessment.actual_energy_kwh
                for record in morning
            ]
            morning_errors = [
                record.effective_energy_kwh - record.assessment.actual_energy_kwh
                for record in morning
            ]
            horizons[horizon] = {
                "count_expected": expected,
                "count_forecasts": len(selected),
                "count_valid": len(valid),
                "coverage": len(valid) / expected,
                "mae_kwh": _mean([abs(error) for error in errors]),
                "bias_kwh": _mean(errors),
                "exclusion_reasons": {
                    key: value for key, value in sorted(exclusions.items()) if value
                },
                "calibrated_comparison": {
                    "count": len(calibrated),
                    "raw_mae_kwh": _mean(
                        [
                            abs(
                                record.raw_energy_kwh
                                - record.assessment.actual_energy_kwh
                            )
                            for record in calibrated
                        ]
                    ),
                    "calibrated_mae_kwh": _mean(
                        [
                            abs(
                                record.calibrated_energy_kwh
                                - record.assessment.actual_energy_kwh
                            )
                            for record in calibrated
                        ]
                    ),
                    "raw_bias_kwh": _mean(
                        [
                            record.raw_energy_kwh - record.assessment.actual_energy_kwh
                            for record in calibrated
                        ]
                    ),
                    "calibrated_bias_kwh": _mean(
                        [
                            record.calibrated_energy_kwh
                            - record.assessment.actual_energy_kwh
                            for record in calibrated
                        ]
                    ),
                },
                "morning_comparison": {
                    "method": "morning_rule_1",
                    "count": len(morning),
                    "baseline_mae_kwh": _mean(
                        [abs(error) for error in morning_baseline_errors]
                    ),
                    "applied_mae_kwh": _mean([abs(error) for error in morning_errors]),
                    "baseline_bias_kwh": _mean(morning_baseline_errors),
                    "applied_bias_kwh": _mean(morning_errors),
                },
                "existing_comparison": {
                    "count": len(paired),
                    "raw_mae_kwh": _mean(
                        [
                            abs(
                                record.raw_energy_kwh
                                - record.assessment.actual_energy_kwh
                            )
                            for record in paired
                        ]
                    ),
                    "existing_mae_kwh": _mean(
                        [
                            abs(
                                record.comparison.energy_kwh
                                - record.assessment.actual_energy_kwh
                            )
                            for record in paired
                        ]
                    ),
                    "raw_bias_kwh": _mean(
                        [
                            record.raw_energy_kwh - record.assessment.actual_energy_kwh
                            for record in paired
                        ]
                    ),
                    "existing_bias_kwh": _mean(
                        [
                            record.comparison.energy_kwh
                            - record.assessment.actual_energy_kwh
                            for record in paired
                        ]
                    ),
                    "mean_own_age_seconds": _mean(
                        [
                            (record.cutoff - record.fetched_at).total_seconds()
                            for record in paired
                        ]
                    ),
                    "mean_existing_age_seconds": _mean(
                        [
                            (
                                record.cutoff - record.comparison.reported_at
                            ).total_seconds()
                            for record in paired
                        ]
                    ),
                },
            }
        return horizons

    def _active_configuration_id(self) -> str | None:
        """Die zuletzt tatsächlich beobachtete Konfiguration als Vorgabe nehmen."""

        return (
            self._configuration_changes[-1][1] if self._configuration_changes else None
        )

    def snapshot(
        self,
        now: datetime,
        days: int,
        include_records: bool = False,
        *,
        configuration_id: str | None = None,
    ) -> dict[str, Any]:
        """Aktuelle Kennzahlen und ältere Anlagenkontexte getrennt ausweisen."""

        now = _utc(now)
        if isinstance(days, bool) or days not in (7, 30, 90):
            raise ValueError("Das Bewertungsfenster umfasst 7, 30 oder 90 Tage")
        configuration_id = configuration_id or self._active_configuration_id()
        end_day = now.astimezone(self.timezone).date()
        start_day = end_day - timedelta(days=days)
        groups: dict[tuple[str | None, str], list[ArchiveRecord]] = {
            (configuration_id, self.timezone.key): []
        }
        for record in self.records.values():
            groups.setdefault((record.configuration_id, record.timezone), []).append(
                record
            )
        contexts = []
        included_records = []
        for (group_configuration, zone_name), all_records in sorted(
            groups.items(), key=lambda item: (item[0][0] or "", item[0][1])
        ):
            zone = ZoneInfo(zone_name)
            group_end = now.astimezone(zone).date()
            group_start = group_end - timedelta(days=days)
            selected = [
                record
                for record in all_records
                if group_start <= record.target_date < group_end
            ]
            included_records.extend(selected)
            contexts.append(
                {
                    "configuration_id": group_configuration,
                    "timezone": zone_name,
                    "active": (group_configuration, zone_name)
                    == (configuration_id, self.timezone.key),
                    "window_start": group_start.isoformat(),
                    "window_end_exclusive": group_end.isoformat(),
                    "record_count": len(all_records),
                    "horizons": self._summarize_horizons(selected, now, days, zone),
                }
            )
        active = next(group for group in contexts if group["active"])
        result = {
            "schema_version": 1,
            "timezone": self.timezone.key,
            "window_days": days,
            "window_start": start_day.isoformat(),
            "window_end_exclusive": end_day.isoformat(),
            "retention_truncated": self.retention_truncated,
            "underperformance": self.underperformance,
            "latest_observed_at": (
                self._latest_observed_at.isoformat()
                if self._latest_observed_at
                else None
            ),
            "record_count": len(self.records),
            "configuration_id": configuration_id,
            "horizons": active["horizons"],
            "configuration_groups": contexts,
        }
        if include_records:
            result["records"] = [
                record.to_dict()
                for record in sorted(
                    included_records, key=lambda item: (item.start, item.horizon)
                )
            ]
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._storage_metadata(),
            "records": [
                record.to_dict()
                for record in sorted(
                    self.records.values(), key=lambda item: (item.start, item.horizon)
                )
            ],
        }

    def _storage_metadata(self) -> dict[str, Any]:
        """Veränderliche Metadaten ohne Verweise auf den laufenden Stand kopieren."""
        return {
            "timezone": self.timezone.key,
            "retention_truncated": self.retention_truncated,
            "underperformance": deepcopy(self.underperformance),
            **(
                {"morning": deepcopy(self.morning.to_dict())}
                if self.morning is not None
                else {}
            ),
            "latest_observed_at": (
                self._latest_observed_at.isoformat()
                if self._latest_observed_at
                else None
            ),
            "configuration_changes": [
                {"observed_at": instant.isoformat(), "configuration_id": value}
                for instant, value in self._configuration_changes
            ],
        }

    def _storage_record(self, record: ArchiveRecord) -> tuple[dict[str, Any], int]:
        """Nur geänderte Records aufbereiten und ihre genaue UTF-8-Größe messen."""
        previous = self._storage_records.get(record.record_id)
        if (
            previous is not None
            and previous[0] is record
            # Die optionalen Versuchs- und Wirkblöcke sind trotz frozen Record
            # verschachtelte Dicts. Auch eine Änderung darin darf weder einen
            # alten Cache verwenden noch einen laufenden Snapshot verändern.
            and previous[1]["short_term"] == record.short_term
            and previous[1]["temperature_comparison"] == record.temperature_comparison
            and previous[1].get("morning") == record.morning
        ):
            return previous[1], previous[2]
        data = record.to_dict()
        data["short_term"] = deepcopy(data["short_term"])
        data["temperature_comparison"] = deepcopy(data["temperature_comparison"])
        size = len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
        self._storage_records[record.record_id] = (record, data, size)
        return data, size

    def _storage_size(self) -> int:
        """Exakte JSON-Größe aus Metadaten, Recordgrößen und Listentrennern bilden."""
        metadata = self._storage_metadata()
        metadata["records"] = []
        return (
            len(json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode())
            + sum(self._storage_record(record)[1] for record in self.records.values())
            + 2 * max(0, len(self.records) - 1)
        )

    def storage_snapshot(self) -> dict[str, Any]:
        """Entkoppelte Speicherbausteine liefern; der Leser darf sie nicht ändern.

        Neue Generationen ersetzen Bausteine vollständig. Der native Store darf
        diesen Snapshot deshalb außerhalb des Event Loops nach JSON serialisieren.
        Öffentliche Leseaktionen verwenden weiterhin eigene Darstellungsdaten.
        """
        return {
            **self._storage_metadata(),
            "records": [
                self._storage_record(record)[0]
                for record in sorted(
                    self.records.values(), key=lambda item: (item.start, item.horizon)
                )
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], timezone: str) -> HistoryArchive:
        """Persistierte Stichtage streng prüfen, bevor neue Werte aufgenommen werden."""
        try:
            ZoneInfo(data["timezone"])
            if not isinstance(data["retention_truncated"], bool):
                raise ValueError(
                    "Gespeicherte Archivzeitzone oder Begrenzung ist ungültig"
                )
            archive = cls(timezone)
            archive.retention_truncated = data["retention_truncated"]
            archive.underperformance = validate_state(data.get("underperformance"))
            if data.get("morning") is not None:
                from .morning import MorningState

                archive.morning = MorningState.from_dict(data["morning"])
            for item in data["configuration_changes"]:
                instant = _timestamp(item["observed_at"])
                if archive._configuration_changes and (
                    instant < archive._configuration_changes[-1][0]
                    or item["configuration_id"] == archive._configuration_changes[-1][1]
                ):
                    raise ValueError(
                        "Gespeicherte Konfigurationswechsel sind inkonsistent"
                    )
                archive.note_configuration(item["configuration_id"], instant)
            for item in data["records"]:
                record = _record_from_dict(item, ZoneInfo(item["timezone"]))
                if record.record_id in archive.records:
                    raise ValueError("Ein Archivziel ist doppelt gespeichert")
                archive.records[record.record_id] = record
            latest = (
                _timestamp(data["latest_observed_at"])
                if data["latest_observed_at"]
                else None
            )
            all_observations = [
                record.observed_at for record in archive.records.values()
            ]
            all_observations.extend(
                instant for instant, _ in archive._configuration_changes
            )
            if all_observations and (latest is None or latest < max(all_observations)):
                raise ValueError("Die gespeicherte Beobachtungsgrenze ist inkonsistent")
            archive._latest_observed_at = latest
            return archive
        except (KeyError, TypeError, AttributeError, OverflowError) as err:
            raise ValueError("Das gespeicherte Prognosearchiv ist beschädigt") from err

    def prune(
        self, now: datetime, max_records: int = MAX_RECORDS, max_bytes: int = MAX_BYTES
    ) -> bool:
        """Älteste Ziele zuerst begrenzen und jeden Verlust ausdrücklich markieren."""
        now = _utc(now)
        if max_records < 0 or max_bytes < 1:
            raise ValueError("Archivgrenzen müssen nichtnegative Datensätze erlauben")
        previous_count = len(self.records)
        local_days = {
            record.timezone: now.astimezone(ZoneInfo(record.timezone)).date()
            for record in self.records.values()
        }
        self.records = {
            key: record
            for key, record in self.records.items()
            if record.target_date
            >= local_days[record.timezone]
            - timedelta(days=365 if record.horizon.startswith("daily") else 90)
        }
        ordered = sorted(
            self.records.values(), key=lambda item: (item.end, item.horizon)
        )
        for record in ordered[: max(0, len(ordered) - max_records)]:
            self.records.pop(record.record_id)
        self._prune_configuration_changes()
        if len(self.records) != previous_count:
            self.retention_truncated = True
        changed = len(self.records) != previous_count
        if self.morning is not None:
            changed |= self.morning.prune(now)
        ordered = sorted(
            self.records.values(), key=lambda item: (item.end, item.horizon)
        )
        current_bytes = self._storage_size()
        reached_byte_limit = current_bytes > max_bytes
        for record in ordered:
            if current_bytes <= max_bytes:
                break
            record_bytes = self._storage_record(record)[1]
            current_bytes -= record_bytes + (2 if len(self.records) > 1 else 0)
            self.records.pop(record.record_id)
            self.retention_truncated = True
            changed = True
        self._prune_configuration_changes()
        self._storage_records = {
            key: value
            for key, value in self._storage_records.items()
            if key in self.records
        }
        # Nach tatsächlichem Beschnitt die veränderten Metadaten mitprüfen.
        # Unterhalb der Grenze genügt die bereits exakte additive Messung.
        if (changed or reached_byte_limit) and self._storage_size() > max_bytes:
            raise ValueError("Die Archivgrenze ist kleiner als die nötigen Metadaten")
        return changed

    def _prune_configuration_changes(self) -> None:
        if not self._configuration_changes:
            return
        if not self.records:
            self._configuration_changes = self._configuration_changes[-1:]
            return
        earliest = min(record.start for record in self.records.values())
        before = [item for item in self._configuration_changes if item[0] <= earliest]
        after = [item for item in self._configuration_changes if item[0] > earliest]
        self._configuration_changes = before[-1:] + after


def _forecast_window(
    intervals: tuple[TotalForecastInterval, ...], start: datetime, end: datetime
) -> tuple[float | None, tuple[str, ...]]:
    cursor = start
    values = []
    flags: set[str] = set()
    for interval in intervals:
        left, right = max(_utc(interval.start), start), min(_utc(interval.end), end)
        if right <= left:
            continue
        if left != cursor or not interval.is_complete:
            return None, ()
        fraction = (right - left).total_seconds() / (
            _utc(interval.end) - _utc(interval.start)
        ).total_seconds()
        values.append(_energy(interval.energy_kwh) * fraction)
        flags.update(interval.quality_flags)
        cursor = right
    if cursor != end:
        return None, ()
    try:
        return _energy(fsum(values)), tuple(sorted(flags))
    except (ValueError, OverflowError):
        return None, ()


def _mean(values: Sequence[float]) -> float | None:
    """Große gültige Fehler ohne Überlauf oder verstecktes Herausfiltern mitteln."""
    return fsum(value / len(values) for value in values) if values else None


def _expected_hours(start_day: date, end_day: date, timezone: ZoneInfo) -> int:
    start = _day_bounds(start_day, timezone)[0]
    end = _day_bounds(end_day, timezone)[0]
    first = start.replace(minute=0, second=0, microsecond=0)
    if first < start:
        first += HOUR
    return max(0, int((end - first + HOUR - timedelta(microseconds=1)) // HOUR))


def _calibration_from_dict(
    data: Mapping[str, Any], start: datetime, end: datetime, raw_energy_kwh: float
) -> dict[str, Any]:
    """Alte Rohstände ohne Lernbasis erhalten; neue eingefrorene Werte prüfen."""
    basis = (
        ForecastCalibrationBasis.from_dict(data["basis"])
        if data.get("basis") is not None
        else None
    )
    if basis is not None and (
        not basis.intervals
        or basis.intervals[0].start != start
        or basis.intervals[-1].end != end
        or not isclose(
            calibrated_energy(basis, 1), raw_energy_kwh, rel_tol=1e-12, abs_tol=1e-12
        )
    ):
        raise ValueError("Die Lernbasis gehört nicht zum archivierten Rohstand")
    result: dict[str, Any] = {"basis": basis}
    for energy_field, factor_field, id_field in (
        ("calibrated_energy_kwh", "applied_factor", "applied_candidate_id"),
        ("candidate_energy_kwh", "candidate_factor", "candidate_id"),
    ):
        values = (data.get(energy_field), data.get(factor_field), data.get(id_field))
        if all(value is None for value in values):
            result.update({energy_field: None, factor_field: None, id_field: None})
            continue
        if basis is None or any(value is None for value in values):
            raise ValueError("Ein korrigierter Archivstand benötigt Basis und Kandidat")
        energy, factor, identity = (
            _energy(values[0]),
            _factor(values[1]),
            _candidate_id(values[2]),
        )
        if not isclose(
            energy, calibrated_energy(basis, factor), rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError(
                "Die korrigierte Archivenergie passt nicht zu ihrem Faktor"
            )
        result.update({energy_field: energy, factor_field: factor, id_field: identity})
    return result


def _record_from_dict(data: Mapping[str, Any], timezone: ZoneInfo) -> ArchiveRecord:
    horizon = data["horizon"]
    if (
        horizon not in HORIZONS
        or data["timezone"] != timezone.key
        or data["model_version"] != MODEL_VERSION
    ):
        raise ValueError("Unbekannter Archivhorizont oder Modellvertrag")
    start, end = _timestamp(data["start"]), _timestamp(data["end"])
    target_date = date.fromisoformat(data["target_date"])
    cutoff = _timestamp(data["cutoff"])
    if horizon == "daily_remaining_12":
        expected_cutoff = datetime.combine(target_date, time(12), timezone)
        if (
            start != _utc(expected_cutoff)
            or end != _day_bounds(target_date, timezone)[1]
        ):
            raise ValueError("Ein Resttagsziel hat ungültige lokale Grenzen")
        max_age = HOUR
    elif horizon.startswith("daily"):
        if (start, end) != _day_bounds(target_date, timezone):
            raise ValueError("Ein Tagesziel hat ungültige lokale Grenzen")
        expected_cutoff = (
            datetime.combine(target_date - timedelta(days=1), time(18), timezone)
            if horizon == "daily_previous_18"
            else datetime.combine(target_date, time(6), timezone)
        )
        max_age = 2 * HOUR
    else:
        if (
            start.minute
            or start.second
            or start.microsecond
            or end - start != HOUR
            or start.astimezone(timezone).date() != target_date
        ):
            raise ValueError("Ein Stundenziel muss eine volle UTC-Stunde sein")
        expected_cutoff = start - (1 if horizon == "hourly_1h" else 3) * HOUR
        max_age = HOUR
    fetched, observed = _timestamp(data["fetched_at"]), _timestamp(data["observed_at"])
    if (
        cutoff != _utc(expected_cutoff)
        or not cutoff - max_age <= fetched <= observed <= cutoff
    ):
        raise ValueError("Der gespeicherte Forecast verfehlt seinen Stichtag")
    record_id = _record_id(horizon, start, end)
    if (
        data["record_id"] != record_id
        or not isinstance(data["config_fingerprint"], str)
        or not data["config_fingerprint"]
    ):
        raise ValueError("Der gespeicherte Forecast hat keine konsistente Identität")
    sources = tuple(
        SourceConfig.from_dict(item) for item in data["measurement_sources"]
    )
    if (
        len({source.source_id for source in sources}) != len(sources)
        or len({source.entity_id for source in sources}) != len(sources)
        or len({source.registry_id or source.entity_id for source in sources})
        != len(sources)
        or any(source.kind == "power" for source in sources)
    ):
        raise ValueError("Archivierte Messquellen sind doppelt vorhanden")
    comparison = (
        ComparisonForecast.from_dict(data["comparison"])
        if data.get("comparison")
        else None
    )
    if comparison and (
        horizon not in ("daily_previous_18", "daily_same_06")
        or comparison.observed_at != observed
    ):
        raise ValueError("Fremdvergleich gehört nicht zum archivierten Stichtag")
    assessment = (
        Assessment.from_dict(data["assessment"]) if data.get("assessment") else None
    )
    revisions = tuple(
        Assessment.from_dict(item) for item in data["assessment_revisions"]
    )
    measured = (
        Assessment.from_dict(data["measured_assessment"])
        if data.get("measured_assessment") is not None
        else None
    )
    if assessment and assessment.manual and "measured_assessment" not in data:
        raise ValueError("Die Korrektur nennt keinen ursprünglichen Messstand")
    if measured is not None and (
        measured.manual
        or not assessment
        or not assessment.manual
        or measured.assessed_at > assessment.assessed_at
    ):
        raise ValueError("Die ursprüngliche Tagesmessung ist inkonsistent")
    all_assessments = (*revisions, *((assessment,) if assessment else ()))
    expected_source_ids = {source.source_id for source in sources}
    for item in (*all_assessments, *((measured,) if measured else ())):
        if item.assessed_at < end:
            raise ValueError("Eine Bewertung liegt vor dem Tagesabschluss")
        if item.manual:
            if horizon not in ("daily_previous_18", "daily_same_06") or not sources:
                raise ValueError(
                    "Eine Tageskorrektur benötigt eine bestätigte Messgrenze"
                )
            continue
        actual_ids = {source.source_id for source in item.sources}
        if not actual_ids <= expected_source_ids:
            raise ValueError("Eine Bewertung nennt nicht zugeordnete Messquellen")
        if item.valid and (
            not expected_source_ids
            or actual_ids != expected_source_ids
            or any(source.energy_kwh is None for source in item.sources)
            or _energy(fsum(source.energy_kwh for source in item.sources))
            != item.actual_energy_kwh
        ):
            raise ValueError("Eine Bewertung hat keine vollständige Quellensumme")
    if (
        len(revisions) > MAX_REVISIONS
        or any(item.assessed_at < end for item in all_assessments)
        or any(a.assessed_at > b.assessed_at for a, b in pairwise(all_assessments))
    ):
        raise ValueError("Die Bewertungsrevisionen sind ungültig")
    if data.get("model_issued_at") is not None:
        raise ValueError("Dieses Archiv kennt keine Modell-Ausgabezeit")
    deleted_sources = _strings(data["deleted_sources"])
    if deleted_sources and (
        all_assessments or expected_source_ids.intersection(deleted_sources)
    ):
        raise ValueError("Gelöschte Messkopien sind noch im Archiv enthalten")
    raw_energy_kwh = _energy(data["raw_energy_kwh"])
    calibration = _calibration_from_dict(data, start, end, raw_energy_kwh)
    return ArchiveRecord(
        record_id,
        start,
        end,
        target_date,
        horizon,
        cutoff,
        fetched,
        observed,
        timezone.key,
        data["config_fingerprint"],
        sources,
        raw_energy_kwh,
        _strings(data["quality_flags"]),
        comparison,
        assessment,
        revisions,
        deleted_sources,
        **calibration,
        short_term=validate_trial(data.get("short_term")),
        temperature_comparison=validate_comparison(data.get("temperature_comparison")),
        measured_assessment=measured,
        morning=_morning_from_dict(data.get("morning")),
    )
