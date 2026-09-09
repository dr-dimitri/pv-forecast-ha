"""Typisierte Modelle für Wetterdaten und PV-Prognosen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from math import isfinite
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "dc_power_kw": self.dc_power_kw,
        }

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
            or isinstance(power, bool)
            or not isinstance(power, int | float)
            or not isfinite(power)
            or power < 0
        ):
            raise ValueError("Die gespeicherte Kalibrierungsbasis ist ungültig")
        return cls(start.astimezone(UTC), end.astimezone(UTC), float(power))


@dataclass(frozen=True, slots=True)
class ForecastCalibrationBasis:
    """Lückenlose eingefrorene Rohleistung mit dem damaligen AC-Limit."""

    intervals: tuple[ForecastBasisInterval, ...]
    inverter_max_power_kw: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervals": [item.to_dict() for item in self.intervals],
            "inverter_max_power_kw": self.inverter_max_power_kw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastCalibrationBasis:
        intervals = tuple(
            ForecastBasisInterval.from_dict(item) for item in data["intervals"]
        )
        limit = data["inverter_max_power_kw"]
        if (
            not intervals
            or any(left.end != right.start for left, right in pairwise(intervals))
            or (
                limit is not None
                and (
                    isinstance(limit, bool)
                    or not isinstance(limit, int | float)
                    or not isfinite(limit)
                    or limit <= 0
                )
            )
        ):
            raise ValueError("Die gespeicherte Kalibrierungsbasis ist ungültig")
        return cls(intervals, float(limit) if limit is not None else None)
