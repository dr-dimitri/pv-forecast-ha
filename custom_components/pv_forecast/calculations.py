"""Deterministische PV-Ertragsberechnung ohne Home-Assistant-Abhängigkeit."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise

from .const import DEFAULT_TEMPERATURE_COEFFICIENT, REFERENCE_TEMPERATURE_C
from .models import (
    DailyYield,
    ForecastBasisInterval,
    ForecastCalibrationBasis,
    ForecastResult,
    PlanningValues,
    PvRoof,
    RoofForecast,
    RoofForecastInterval,
    TotalForecastInterval,
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
    *,
    calibration_factor: float = 1.0,
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
    total_intervals: list[TotalForecastInterval] = []
    forecast_start = datetime.combine(local_date, time.min, timezone).astimezone(UTC)
    forecast_end = datetime.combine(
        local_date + timedelta(days=2), time.min, timezone
    ).astimezone(UTC)

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
        # Die API liefert gemeinsame UTC-Stunden. Randbeschnitt und fehlende
        # Dachbeiträge behalten ihre tatsächlichen zeitlichen Abdeckungsgrenzen.
        boundaries = sorted(
            {
                max(point.start.astimezone(UTC), forecast_start)
                for point in points.values()
                if point is not None
                and point.start.astimezone(UTC) < timestamp
                and timestamp > forecast_start
                and point.start.astimezone(UTC) < forecast_end
            }
            | {min(timestamp, forecast_end)}
        )
        for start, end in pairwise(boundaries):
            if end <= start:
                continue
            covered = {
                roof_id: point
                for roof_id, point in points.items()
                if point is not None and point.start.astimezone(UTC) <= start
            }
            if not covered:
                continue
            power = sum(ac_by_roof[roof_id] for roof_id in covered)
            flags = {flag for point in covered.values() for flag in point.quality_flags}
            is_complete = len(covered) == len(roofs)
            if not is_complete:
                flags.add("missing_roof_data")
            total_intervals.append(
                TotalForecastInterval(
                    start=start,
                    end=end,
                    energy_kwh=_finite_result(
                        power * ((end - start).total_seconds() / 3600),
                        "Gesamtintervallenergie",
                    ),
                    ac_power_kw=power,
                    quality_flags=tuple(sorted(flags)),
                    is_complete=is_complete,
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

    combined_intervals = tuple(total_intervals)
    result = ForecastResult(
        local_date=local_date,
        roofs=roof_results,
        total=DailyYield(
            today=aggregate_energy_for_day(combined_intervals, local_date, timezone),
            tomorrow=aggregate_energy_for_day(combined_intervals, tomorrow, timezone),
        ),
        total_intervals=combined_intervals,
    )
    return apply_calibration(
        result, calibration_factor, inverter_max_power_kw, timezone
    )


def _validate_calibration_factor(factor: float) -> None:
    if (
        isinstance(factor, bool)
        or not math.isfinite(factor)
        or not 0.5 <= factor <= 1.5
    ):
        raise InvalidConfigurationError(
            "Der Anlagenfaktor muss zwischen 0,5 und 1,5 liegen"
        )


def apply_calibration(
    raw_forecast: ForecastResult,
    factor: float,
    inverter_max_power_kw: float | None,
    timezone: tzinfo,
) -> ForecastResult:
    """Einen Anlagenfaktor lokal vor Clipping anwenden und die Roh-DC-Werte bewahren."""

    _validate_calibration_factor(factor)
    if factor == 1.0:
        return raw_forecast
    by_end = {
        roof_id: {item.end.astimezone(UTC): item for item in roof.intervals}
        for roof_id, roof in raw_forecast.roofs.items()
    }
    powers = {
        end: proportional_clipping(
            {
                roof_id: _finite_result(
                    items[end].dc_power_kw * factor, "kalibrierte Dachleistung"
                )
                for roof_id, items in by_end.items()
                if end in items
            },
            inverter_max_power_kw,
        )
        for end in {end for items in by_end.values() for end in items}
    }
    roofs = {}
    for roof_id, roof in raw_forecast.roofs.items():
        intervals = tuple(
            replace(
                item,
                ac_power_kw=powers[item.end.astimezone(UTC)][roof_id],
                energy_kwh=_finite_result(
                    powers[item.end.astimezone(UTC)][roof_id]
                    * max(
                        0.0,
                        (
                            item.end.astimezone(UTC) - item.start.astimezone(UTC)
                        ).total_seconds()
                        / 3600,
                    ),
                    "kalibrierte Intervallenergie",
                ),
            )
            for item in roof.intervals
        )
        roofs[roof_id] = replace(
            roof,
            intervals=intervals,
            daily=DailyYield(
                aggregate_energy_for_day(intervals, raw_forecast.local_date, timezone),
                aggregate_energy_for_day(
                    intervals, raw_forecast.local_date + timedelta(days=1), timezone
                ),
            ),
        )
    total_intervals = []
    for item in raw_forecast.total_intervals:
        start, end = item.start.astimezone(UTC), item.end.astimezone(UTC)
        power = _finite_result(
            sum(
                interval.ac_power_kw
                for roof in roofs.values()
                for interval in roof.intervals
                if interval.start.astimezone(UTC) <= start
                and interval.end.astimezone(UTC) >= end
            ),
            "kalibrierte Gesamtleistung",
        )
        total_intervals.append(
            replace(
                item,
                ac_power_kw=power,
                energy_kwh=_finite_result(
                    power * (end - start).total_seconds() / 3600,
                    "kalibrierte Gesamtenergie",
                ),
            )
        )
    intervals = tuple(total_intervals)
    return replace(
        raw_forecast,
        roofs=roofs,
        total_intervals=intervals,
        total=DailyYield(
            aggregate_energy_for_day(intervals, raw_forecast.local_date, timezone),
            aggregate_energy_for_day(
                intervals, raw_forecast.local_date + timedelta(days=1), timezone
            ),
        ),
    )


def forecast_basis(
    forecast: ForecastResult,
    start: datetime,
    end: datetime,
    inverter_max_power_kw: float | None,
) -> ForecastCalibrationBasis | None:
    """Die vollständige Rohleistung eines UTC-Fensters ohne Rückrechnung einfrieren."""

    if start.utcoffset() is None or end.utcoffset() is None:
        raise InvalidConfigurationError(
            "Die Kalibrierungsbasis benötigt absolute Zeitpunkte"
        )
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if not forecast.roofs or end <= start:
        return None
    cursor = start
    result = []
    for item in forecast.total_intervals:
        left, right = max(start, item.start.astimezone(UTC)), min(
            end, item.end.astimezone(UTC)
        )
        if right <= left:
            continue
        if left != cursor or not item.is_complete:
            return None
        powers = []
        for roof in forecast.roofs.values():
            matches = [
                interval.dc_power_kw
                for interval in roof.intervals
                if interval.start.astimezone(UTC) <= left
                and interval.end.astimezone(UTC) >= right
            ]
            if len(matches) != 1:
                return None
            powers.extend(matches)
        result.append(
            ForecastBasisInterval(
                left, right, _finite_result(sum(powers), "ungekürzte Gesamtleistung")
            )
        )
        cursor = right
    if cursor != end:
        return None
    return ForecastCalibrationBasis(tuple(result), inverter_max_power_kw)


def calibrated_energy(basis: ForecastCalibrationBasis, factor: float) -> float:
    """Den Faktor vor dem damaligen AC-Limit auf eingefrorene Rohleistung anwenden."""

    _validate_calibration_factor(factor)
    values = []
    for interval in basis.intervals:
        power = proportional_clipping(
            {
                "plant": _finite_result(
                    interval.dc_power_kw * factor, "kalibrierte Leistung"
                )
            },
            basis.inverter_max_power_kw,
        )["plant"]
        values.append(
            power
            * (
                interval.end.astimezone(UTC) - interval.start.astimezone(UTC)
            ).total_seconds()
            / 3600
        )
    return _finite_result(math.fsum(values), "kalibrierte Tagesenergie")


def fit_calibration_factor(
    samples: Sequence[tuple[ForecastCalibrationBasis, float]],
) -> float:
    """Fest nach Tages-MAE suchen; Gleichstände bevorzugen das Grundmodell."""

    if not samples or any(
        not math.isfinite(actual) or actual < 0 for _, actual in samples
    ):
        raise InvalidConfigurationError("Der Lernlauf benötigt gültige Tagesmessungen")
    best_factor, best_error = 1.0, math.inf
    for step in range(50, 151):
        factor = step / 100
        error = math.fsum(
            abs(calibrated_energy(basis, factor) - actual) / len(samples)
            for basis, actual in samples
        )
        tied = math.isclose(error, best_error, rel_tol=1e-12, abs_tol=1e-12)
        if (error < best_error and not tied) or (
            tied and abs(factor - 1) < abs(best_factor - 1)
        ):
            best_factor, best_error = factor, error
    return best_factor


def aggregate_energy_for_day(
    intervals: tuple[RoofForecastInterval | TotalForecastInterval, ...],
    day: date,
    timezone: tzinfo,
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


def _covered_window_energy(
    intervals: tuple[TotalForecastInterval, ...], start: datetime, end: datetime
) -> float | None:
    """Energie eines lückenlos abgedeckten, halb offenen UTC-Fensters liefern."""

    cursor = start
    energy = 0.0
    for interval in intervals:
        interval_start = interval.start.astimezone(UTC)
        interval_end = interval.end.astimezone(UTC)
        overlap_start = max(interval_start, start)
        overlap_end = min(interval_end, end)
        if overlap_end <= overlap_start:
            continue
        if overlap_start != cursor or not interval.is_complete:
            return None
        overlap_fraction = (overlap_end - overlap_start).total_seconds() / (
            interval_end - interval_start
        ).total_seconds()
        energy += interval.energy_kwh * overlap_fraction
        cursor = overlap_end
    return energy if cursor == end else None


def calculate_planning_values(
    forecast: ForecastResult, now: datetime, timezone: tzinfo
) -> PlanningValues:
    """Planungswerte ausschließlich aus der vorhandenen Gesamtzeitreihe ableiten.

    Die stärkste Prognosestunde ist das höchste mittlere AC-Leistungsniveau
    des ganzen lokalen Tages. Teilstunden an Tagesgrenzen werden dadurch nicht
    allein wegen ihrer kürzeren Überlappung als schwächer eingestuft.
    """

    now_utc = now.astimezone(UTC)
    local_day = now.astimezone(timezone).date()
    day_start = datetime.combine(local_day, time.min, timezone).astimezone(UTC)
    day_end = datetime.combine(
        local_day + timedelta(days=1), time.min, timezone
    ).astimezone(UTC)
    intervals = forecast.total_intervals
    remaining = _covered_window_energy(intervals, now_utc, day_end)
    next_hour = _covered_window_energy(intervals, now_utc, now_utc + timedelta(hours=1))
    running = next(
        (
            interval
            for interval in intervals
            if interval.start.astimezone(UTC) <= now_utc < interval.end.astimezone(UTC)
        ),
        None,
    )
    power_now = (
        running.ac_power_kw if running is not None and running.is_complete else None
    )
    peak_complete = _covered_window_energy(intervals, day_start, day_end) is not None
    peak = None
    peak_power = 0.0
    if peak_complete:
        for interval in intervals:
            start = max(interval.start.astimezone(UTC), day_start)
            end = min(interval.end.astimezone(UTC), day_end)
            if end > start and interval.ac_power_kw > peak_power:
                peak_power = interval.ac_power_kw
                peak = start
    return PlanningValues(
        remaining_today_kwh=remaining,
        next_60_minutes_kwh=next_hour,
        power_now_kw=power_now,
        peak_today=peak,
        peak_today_complete=peak_complete,
    )
