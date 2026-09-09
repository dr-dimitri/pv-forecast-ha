"""Typisierte Modelle für Wetterdaten und PV-Prognosen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from math import fsum, isclose, isfinite
from typing import Any, Literal

type ForecastDay = Literal["today", "tomorrow"]


@dataclass(frozen=True, slots=True)
class GeocodedLocation:
    """Aufgelöste Anschrift für die einmalige Standortkonfiguration."""

    latitude: float
    longitude: float
    display_name: str


@dataclass(frozen=True, slots=True)
class PvRoof:
    """Unveränderliche Konfiguration einer PV-Dachfläche."""

    id: str
    name: str
    installed_power_kwp: float
    compass_azimuth_deg: float
    tilt_deg: float
    loss_fraction: float


@dataclass(frozen=True, slots=True)
class AcInverterGroup:
    """Ein reales AC-Gerät beziehungsweise eine gemeinsam begrenzte Dachgruppe."""

    id: str
    name: str
    max_power_kw: float
    roof_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WeatherInterval:
    """Wetterwerte für ein explizites Zeitintervall.

    Open-Meteo weist stündliche GTI-Werte als Mittelwert der vorhergehenden
    Stunde aus. Daher wird neben dem API-Zeitstempel (Intervallende) auch der
    Beginn gespeichert.
    """

    start: datetime
    end: datetime
    gti_w_m2: float
    ambient_temperature_c: float | None
    quality_flags: tuple[str, ...] = ()

    @property
    def duration_hours(self) -> float:
        """Länge des Intervalls in Stunden."""

        return (
            self.end.astimezone(UTC) - self.start.astimezone(UTC)
        ).total_seconds() / 3600


@dataclass(frozen=True, slots=True)
class RoofForecastInterval:
    """Berechnete Prognose einer Dachfläche für ein Intervall."""

    start: datetime
    end: datetime
    dc_power_kw: float
    ac_power_kw: float
    energy_kwh: float


@dataclass(frozen=True, slots=True)
class TotalForecastInterval:
    """Halb offenes UTC-Gesamtintervall nach Clipping mit Eingabequalität."""

    start: datetime
    end: datetime
    energy_kwh: float
    ac_power_kw: float
    quality_flags: tuple[str, ...] = ()
    is_complete: bool = True


@dataclass(frozen=True, slots=True)
class DailyYield:
    """Ertragsprognose für heute und morgen."""

    today: float
    tomorrow: float


@dataclass(frozen=True, slots=True)
class RoofForecast:
    """Zeitreihe und Tageswerte einer Dachfläche."""

    roof: PvRoof
    intervals: tuple[RoofForecastInterval, ...]
    daily: DailyYield


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """Vollständiges Ergebnis eines Coordinator-Updates."""

    local_date: date
    roofs: dict[str, RoofForecast]
    total: DailyYield
    total_intervals: tuple[TotalForecastInterval, ...] = ()
    inverter_groups: tuple[AcInverterGroup, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningValues:
    """Zeitabhängige Gesamtwerte; fehlende Abdeckung bleibt unbekannt."""

    remaining_today_kwh: float | None
    next_60_minutes_kwh: float | None
    power_now_kw: float | None
    peak_today: datetime | None
    peak_today_complete: bool


@dataclass(frozen=True, slots=True)
class OpenMeteoForecast:
    """Validierte Antwort für genau eine Dachgeometrie."""

    intervals: tuple[WeatherInterval, ...]


@dataclass(frozen=True, slots=True)
class ForecastBasisInterval:
    """Unveränderte Gesamtleistung vor Clipping für ein absolutes UTC-Intervall."""

    start: datetime
    end: datetime
    dc_power_kw: float
    group_dc_power_kw: tuple[float, ...] = ()
    ungrouped_dc_power_kw: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "dc_power_kw": self.dc_power_kw,
        }
        if self.group_dc_power_kw:
            result.update(
                group_dc_power_kw=list(self.group_dc_power_kw),
                ungrouped_dc_power_kw=self.ungrouped_dc_power_kw,
            )
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastBasisInterval:
        start, end = datetime.fromisoformat(data["start"]), datetime.fromisoformat(
            data["end"]
        )
        power = data["dc_power_kw"]
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or end.astimezone(UTC) <= start.astimezone(UTC)
            or not _basis_energy(power)
        ):
            raise ValueError("Die gespeicherte Kalibrierungsbasis ist ungültig")
        grouped = data.get("group_dc_power_kw", [])
        ungrouped = data.get("ungrouped_dc_power_kw")
        if (
            not isinstance(grouped, list)
            or (
                ("group_dc_power_kw" in data or "ungrouped_dc_power_kw" in data)
                and (not grouped or "ungrouped_dc_power_kw" not in data)
            )
            or any(not _basis_energy(value) for value in grouped)
            or (grouped and not _basis_energy(ungrouped))
            or (not grouped and ungrouped is not None)
        ):
            raise ValueError("Die gespeicherte Gruppenleistung ist ungültig")
        return cls(
            start.astimezone(UTC),
            end.astimezone(UTC),
            float(power),
            tuple(float(value) for value in grouped),
            float(ungrouped) if ungrouped is not None else None,
        )


def _basis_energy(value: object) -> bool:
    """Gespeicherte Leistungen ohne Bool-Zahlen und Überläufe prüfen."""

    try:
        return (
            not isinstance(value, bool)
            and isinstance(value, int | float)
            and isfinite(value)
            and value >= 0
        )
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class ForecastCalibrationBasis:
    """Lückenlose eingefrorene Rohleistung mit dem damaligen AC-Limit."""

    intervals: tuple[ForecastBasisInterval, ...]
    inverter_max_power_kw: float | None
    group_limits: tuple[tuple[str, float], ...] = ()
    has_ungrouped_roofs: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = {
            "intervals": [item.to_dict() for item in self.intervals],
            "inverter_max_power_kw": self.inverter_max_power_kw,
        }
        if self.group_limits:
            result.update(
                schema_version=2,
                inverter_groups=[
                    {"id": group_id, "max_power_kw": limit}
                    for group_id, limit in self.group_limits
                ],
                has_ungrouped_roofs=self.has_ungrouped_roofs,
            )
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastCalibrationBasis:
        intervals = tuple(
            ForecastBasisInterval.from_dict(item) for item in data["intervals"]
        )
        limit = data["inverter_max_power_kw"]
        if (
            not intervals
            or any(left.end != right.start for left, right in pairwise(intervals))
            or (limit is not None and (not _basis_energy(limit) or limit <= 0))
        ):
            raise ValueError("Die gespeicherte Kalibrierungsbasis ist ungültig")
        version = data.get("schema_version", 1)
        if type(version) is not int or version not in (1, 2):
            raise ValueError("Unbekannte Version der Kalibrierungsbasis")
        groups = data.get("inverter_groups", [])
        ungrouped = data.get("has_ungrouped_roofs", False)
        if version == 1:
            if (
                "inverter_groups" in data
                or "has_ungrouped_roofs" in data
                or any(item.group_dc_power_kw for item in intervals)
            ):
                raise ValueError("Gruppen benötigen einen eindeutigen Basisvertrag")
            return cls(intervals, float(limit) if limit is not None else None)
        if (
            not isinstance(groups, list)
            or not groups
            or "has_ungrouped_roofs" not in data
            or type(ungrouped) is not bool
        ):
            raise ValueError("Die gespeicherten AC-Gruppen sind ungültig")
        limits = []
        for group in groups:
            if (
                not isinstance(group, dict)
                or not isinstance(group.get("id"), str)
                or not group["id"].strip()
                or not _basis_energy(group.get("max_power_kw"))
                or group["max_power_kw"] <= 0
            ):
                raise ValueError("Die gespeicherte AC-Gruppe ist ungültig")
            limits.append((group["id"], float(group["max_power_kw"])))
        if len({group_id for group_id, _ in limits}) != len(limits):
            raise ValueError("Die gespeicherten AC-Gruppen sind doppelt vorhanden")
        for item in intervals:
            if (
                len(item.group_dc_power_kw) != len(limits)
                or item.ungrouped_dc_power_kw is None
                or (not ungrouped and item.ungrouped_dc_power_kw != 0)
            ):
                raise ValueError(
                    "Die gespeicherte Leistungsaufteilung ist unvollständig"
                )
            try:
                total = fsum((*item.group_dc_power_kw, item.ungrouped_dc_power_kw))
            except OverflowError as err:
                raise ValueError("Die gespeicherte Gruppensumme ist ungültig") from err
            if not isclose(total, item.dc_power_kw, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Die gespeicherte Gruppensumme stimmt nicht überein")
        return cls(
            intervals,
            float(limit) if limit is not None else None,
            tuple(limits),
            ungrouped,
        )
