"""Typisierte Modelle für Wetterdaten und PV-Prognosen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime


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


@dataclass(frozen=True, slots=True)
class OpenMeteoForecast:
    """Validierte Antwort für genau eine Dachgeometrie."""

    intervals: tuple[WeatherInterval, ...]
