"""Experimentelle Horizontabschattung mit rein geometrischen Sonnenständen."""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .models import PvRoof, WeatherInterval

CONF_HORIZON_PROFILES = "horizon_profiles"
HORIZON_RULE_VERSION = 1
SAMPLES_PER_INTERVAL = 12


def validate_profile(values: object) -> tuple[float, ...]:
    """Gleichmäßig verteilte Höhenwinkel prüfen; leere und Nullprofile deaktivieren."""

    if not isinstance(values, list | tuple) or len(values) not in (0, 12, 24):
        raise ValueError("Ein Horizontprofil benötigt 12 oder 24 Höhenwinkel")
    if any(
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= 90
        for value in values
    ):
        raise ValueError("Höhenwinkel müssen endliche Zahlen von 0 bis 90 Grad sein")
    profile = tuple(float(value) for value in values)
    return profile if any(profile) else ()


def parse_profile(text: object) -> tuple[float, ...]:
    """Dezimalpunkte und übliche Listentrenner ohne Azimutspalten einlesen."""

    if not isinstance(text, str) or len(text) > 2048:
        raise ValueError("Das Horizontprofil ist zu lang oder kein Text")
    if not text.strip():
        return ()
    return validate_profile(
        [float(value) for value in re.split(r"[,;\s]+", text.strip())]
    )


def horizon_elevation(profile: tuple[float, ...], azimuth_deg: float) -> float:
    """Nord beginnend im Uhrzeigersinn, über den Nordübergang linear interpolieren."""

    if not profile:
        return 0.0
    position = (azimuth_deg % 360) * len(profile) / 360
    index = math.floor(position)
    fraction = position - index
    return (
        profile[index] * (1 - fraction) + profile[(index + 1) % len(profile)] * fraction
    )


def solar_position(
    instant: datetime, latitude: float, longitude: float
) -> tuple[float, float]:
    """NOAA-Näherung: geometrische Höhe und Kompassazimut, ohne Refraktion.

    Quelle: https://gml.noaa.gov/grad/solcalc/solareqns.PDF
    UTC vermeidet lokale Offset- und Fold-Mehrdeutigkeiten.
    """

    if (
        instant.utcoffset() is None
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError(
            "Sonnenstand benötigt einen absoluten Zeitpunkt und gültige Koordinaten"
        )
    utc = instant.astimezone(UTC)
    hour = utc.hour + utc.minute / 60 + (utc.second + utc.microsecond / 1e6) / 3600
    gamma = (
        2
        * math.pi
        / (366 if calendar.isleap(utc.year) else 365)
        * (utc.timetuple().tm_yday - 1 + (hour - 12) / 24)
    )
    equation = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    angle = math.radians((hour * 60 + equation + 4 * longitude) / 4 - 180)
    lat = math.radians(latitude)
    elevation = math.asin(
        max(
            -1.0,
            min(
                1.0,
                math.sin(lat) * math.sin(declination)
                + math.cos(lat) * math.cos(declination) * math.cos(angle),
            ),
        )
    )
    azimuth = (
        math.degrees(
            math.atan2(
                math.sin(angle),
                math.cos(angle) * math.sin(lat) - math.tan(declination) * math.cos(lat),
            )
        )
        + 180
    ) % 360
    return math.degrees(elevation), azimuth


def adjusted_weather(
    roof: PvRoof, point: WeatherInterval, latitude: float, longitude: float
) -> WeatherInterval:
    """Nur den geschätzten blockierten Direktanteil vom vorhandenen GTI abziehen."""

    if not any(roof.horizon_profile):
        return point
    dni, dhi = point.direct_normal_irradiance_w_m2, point.diffuse_radiation_w_m2
    if any(
        value is None or not math.isfinite(value) or value < 0 for value in (dni, dhi)
    ):
        return replace(
            point,
            quality_flags=tuple(
                sorted(set(point.quality_flags) | {"horizon_input_fallback"})
            ),
        )
    start, end = point.start.astimezone(UTC), point.end.astimezone(UTC)
    if end <= start:
        raise ValueError("Horizontabschattung benötigt ein positives UTC-Intervall")
    tilt = math.radians(roof.tilt_deg)
    projected, blocked = [], []
    for index in range(SAMPLES_PER_INTERVAL):
        instant = start + (end - start) * ((index + 0.5) / SAMPLES_PER_INTERVAL)
        elevation, azimuth = solar_position(instant, latitude, longitude)
        height = math.radians(elevation)
        incidence = math.sin(height) * math.cos(tilt) + math.cos(height) * math.sin(
            tilt
        ) * math.cos(math.radians(azimuth - roof.compass_azimuth_deg))
        direct = dni * max(0.0, incidence) if elevation > 0 else 0.0
        projected.append(direct)
        blocked.append(
            direct
            if elevation < horizon_elevation(roof.horizon_profile, azimuth)
            else 0.0
        )
    total = math.fsum(projected)
    if total == 0:
        return point
    diffuse = min(point.gti_w_m2, dhi * (1 + math.cos(tilt)) / 2)
    removable = min(total / SAMPLES_PER_INTERVAL, max(0.0, point.gti_w_m2 - diffuse))
    removed = removable * math.fsum(blocked) / total
    return (
        replace(point, gti_w_m2=max(0.0, point.gti_w_m2 - removed))
        if removed
        else point
    )


def shading_metadata(active: bool) -> dict[str, Any]:
    """Modellgrenze sichtbar halten, ohne eine gemessene Verbesserung zu behaupten."""

    return {
        "rule_version": HORIZON_RULE_VERSION,
        "active": active,
        "experimental": active,
        "measured_improvement": "unavailable",
        "samples_per_interval": SAMPLES_PER_INTERVAL,
        "method": "geometric_direct_horizon_with_diffuse_floor",
    }
