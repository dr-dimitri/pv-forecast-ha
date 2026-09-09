"""Deterministische PV-Ertragsberechnung ohne Home-Assistant-Abhängigkeit."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from .const import DEFAULT_TEMPERATURE_COEFFICIENT, REFERENCE_TEMPERATURE_C
from .models import (
    DailyYield,
    ForecastResult,
    PvRoof,
    RoofForecast,
    RoofForecastInterval,
    WeatherInterval,
)


class InvalidConfigurationError(ValueError):
    """Eine PV-Konfiguration oder ihr Berechnungsergebnis ist fachlich ungültig."""


def _finite_result(value: float, quantity: str) -> float:
    """Nicht endliche Ergebnisse vor Begrenzung und Veröffentlichung stoppen."""

    if not math.isfinite(value):
        raise InvalidConfigurationError(
            f"PV-Berechnung liefert keinen endlichen Wert für {quantity}"
        )
    return value


def validate_coordinates(latitude: float, longitude: float) -> None:
    """Standortkoordinaten validieren."""

    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise InvalidConfigurationError("Breitengrad muss zwischen -90 und 90 liegen")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise InvalidConfigurationError("Längengrad muss zwischen -180 und 180 liegen")


def validate_roof(roof: PvRoof) -> None:
    """Eine Dachflächenkonfiguration validieren."""

    if not roof.id or not roof.name.strip():
        raise InvalidConfigurationError("Dach-ID und Dachname dürfen nicht leer sein")
    if not math.isfinite(roof.installed_power_kwp) or roof.installed_power_kwp <= 0:
        raise InvalidConfigurationError("Installierte Leistung muss größer als 0 sein")
    if (
        not math.isfinite(roof.compass_azimuth_deg)
        or not 0 <= roof.compass_azimuth_deg < 360
    ):
        raise InvalidConfigurationError(
            "Azimut muss zwischen 0 (inklusive) und 360 liegen"
        )
    if not math.isfinite(roof.tilt_deg) or not 0 <= roof.tilt_deg <= 90:
        raise InvalidConfigurationError("Neigung muss zwischen 0 und 90 liegen")
    if not math.isfinite(roof.loss_fraction) or not 0 <= roof.loss_fraction <= 1:
        raise InvalidConfigurationError("Verlustfaktor muss zwischen 0 und 1 liegen")


def to_open_meteo_azimuth(compass_azimuth_deg: float) -> float:
    """Kompass-Azimut (Nord=0°, Ost=90°) in Open-Meteo umrechnen.

    Open-Meteo verwendet Süd=0°, Ost=-90°, West=+90° und Nord=±180°.
    """

    if not math.isfinite(compass_azimuth_deg):
        raise InvalidConfigurationError("Azimut muss endlich sein")
    return compass_azimuth_deg % 360 - 180


def temperature_factor(
    ambient_temperature_c: float | None,
    coefficient_per_c: float = DEFAULT_TEMPERATURE_COEFFICIENT,
    reference_temperature_c: float = REFERENCE_TEMPERATURE_C,
) -> float:
    """Temperaturfaktor mit Außentemperatur als vereinfachtem Zelltemperatur-Proxy.

    Fehlt die Temperatur, wird keine Temperaturkorrektur angewendet. Es wird
    dabei keine vermeintliche Temperatur erfunden.
    """

    if ambient_temperature_c is None:
        return 1.0
    if not math.isfinite(ambient_temperature_c):
        return 1.0
    temperature_difference = _finite_result(
        ambient_temperature_c - reference_temperature_c, "Temperaturdifferenz"
    )
    factor = _finite_result(
        1 + coefficient_per_c * temperature_difference, "Temperaturfaktor"
    )
    return max(0.0, factor)


def calculate_dc_power_kw(roof: PvRoof, weather: WeatherInterval) -> float:
    """Verlust- und temperaturkorrigierte DC-Leistung berechnen."""

    validate_roof(roof)
    gti_w_m2 = max(0.0, weather.gti_w_m2) if math.isfinite(weather.gti_w_m2) else 0.0
    raw_power_kw = _finite_result(
        roof.installed_power_kwp * gti_w_m2 / 1000, "Rohleistung"
    )
    corrected_power_kw = _finite_result(
        raw_power_kw * temperature_factor(weather.ambient_temperature_c),
        "temperaturkorrigierte Leistung",
    )
    return max(0.0, corrected_power_kw * (1 - roof.loss_fraction))


def proportional_clipping(
    dc_power_by_roof: Mapping[str, float], inverter_max_power_kw: float | None
) -> dict[str, float]:
    """Ein globales Wechselrichterlimit proportional auf Dachflächen verteilen."""

    sanitized = {
        roof_id: max(0.0, _finite_result(power, "Dachleistung"))
        for roof_id, power in dc_power_by_roof.items()
    }
    total_dc = _finite_result(sum(sanitized.values()), "Gesamtleistung")
    if inverter_max_power_kw is None:
        return sanitized
    if not math.isfinite(inverter_max_power_kw) or inverter_max_power_kw <= 0:
        raise InvalidConfigurationError("Wechselrichterleistung muss größer als 0 sein")
    if total_dc <= inverter_max_power_kw or total_dc == 0:
        return sanitized
    factor = inverter_max_power_kw / total_dc
    return {roof_id: power * factor for roof_id, power in sanitized.items()}


def calculate_forecast(
    roofs: tuple[PvRoof, ...],
    weather_by_roof: Mapping[str, tuple[WeatherInterval, ...]],
    inverter_max_power_kw: float | None,
    local_date: date,
    timezone: tzinfo,
) -> ForecastResult:
    """Zeitreihen aller Dächer berechnen, clippen und für zwei Tage summieren."""

    if not roofs:
        raise InvalidConfigurationError("Mindestens eine Dachfläche ist erforderlich")
    for roof in roofs:
        validate_roof(roof)

    # Wiederholte Ortsstunden beim DST-Rücksprung sind nur in UTC eindeutig.
    weather_maps = {
        roof.id: {
            point.end.astimezone(UTC): point
            for point in weather_by_roof.get(roof.id, ())
        }
        for roof in roofs
    }
    timestamps = sorted(
        {timestamp for points in weather_maps.values() for timestamp in points}
    )
    intervals_by_roof: dict[str, list[RoofForecastInterval]] = {
        roof.id: [] for roof in roofs
    }

    for timestamp in timestamps:
        points = {roof.id: weather_maps[roof.id].get(timestamp) for roof in roofs}
        dc_by_roof: dict[str, float] = {}
        for roof in roofs:
            point = points[roof.id]
            dc_by_roof[roof.id] = (
                calculate_dc_power_kw(roof, point) if point is not None else 0.0
            )
        ac_by_roof = proportional_clipping(dc_by_roof, inverter_max_power_kw)
        for roof in roofs:
            point = points[roof.id]
            if point is None:
                continue
            duration_hours = max(0.0, point.duration_hours)
            intervals_by_roof[roof.id].append(
                RoofForecastInterval(
                    start=point.start,
                    end=point.end,
                    dc_power_kw=dc_by_roof[roof.id],
                    ac_power_kw=ac_by_roof[roof.id],
                    energy_kwh=_finite_result(
                        ac_by_roof[roof.id] * duration_hours, "Intervallenergie"
                    ),
                )
            )

    tomorrow = local_date + timedelta(days=1)
    roof_results: dict[str, RoofForecast] = {}
    for roof in roofs:
        intervals = tuple(intervals_by_roof[roof.id])
        roof_results[roof.id] = RoofForecast(
            roof=roof,
            intervals=intervals,
            daily=DailyYield(
                today=aggregate_energy_for_day(intervals, local_date, timezone),
                tomorrow=aggregate_energy_for_day(intervals, tomorrow, timezone),
            ),
        )

    return ForecastResult(
        local_date=local_date,
        roofs=roof_results,
        total=DailyYield(
            today=_finite_result(
                sum(result.daily.today for result in roof_results.values()),
                "Gesamtenergie heute",
            ),
            tomorrow=_finite_result(
                sum(result.daily.tomorrow for result in roof_results.values()),
                "Gesamtenergie morgen",
            ),
        ),
    )


def aggregate_energy_for_day(
    intervals: tuple[RoofForecastInterval, ...], day: date, timezone: tzinfo
) -> float:
    """Intervallenergie nach tatsächlicher Überlappung einem lokalen Tag zuordnen."""

    day_start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    day_end = datetime.combine(day + timedelta(days=1), time.min, timezone).astimezone(
        UTC
    )
    total = 0.0
    for interval in intervals:
        interval_start = interval.start.astimezone(UTC)
        interval_end = interval.end.astimezone(UTC)
        overlap_start = max(interval_start, day_start)
        overlap_end = min(interval_end, day_end)
        if overlap_end <= overlap_start:
            continue
        interval_seconds = (interval_end - interval_start).total_seconds()
        if interval_seconds <= 0:
            continue
        overlap_fraction = (
            overlap_end - overlap_start
        ).total_seconds() / interval_seconds
        total = _finite_result(
            total + interval.energy_kwh * overlap_fraction, "Tagesenergie"
        )
    return total
