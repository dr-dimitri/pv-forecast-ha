"""Asynchroner Client für die Open-Meteo-Forecast-API."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import (
    ClientError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
)

from .calculations import to_open_meteo_azimuth
from .const import OPEN_METEO_FORECAST_URL, REQUEST_TIMEOUT_SECONDS
from .models import OpenMeteoForecast, PvRoof, WeatherInterval


class OpenMeteoError(Exception):
    """Basisklasse für erwartete Open-Meteo-Fehler."""


class OpenMeteoConnectionError(OpenMeteoError):
    """Open-Meteo konnte nicht erreicht werden."""


class OpenMeteoDataError(OpenMeteoError):
    """Open-Meteo hat eine unbrauchbare Antwort geliefert."""


class OpenMeteoClient:
    """Kapselt ausschließlich Transport und Parsing der Open-Meteo-Daten."""

    def __init__(self, session: ClientSession) -> None:
        """Client mit einer von Home Assistant verwalteten Session initialisieren."""

        self._session = session

    async def async_fetch_roofs(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        roofs: tuple[PvRoof, ...],
        *,
        local_date: date | None = None,
    ) -> dict[str, tuple[WeatherInterval, ...]]:
        """Forecasts je unterschiedlicher Dachgeometrie parallel abrufen.

        Open-Meteo akzeptiert pro Request nur ein Tilt-/Azimut-Paar. Dächer mit
        identischer Geometrie teilen sich deshalb denselben Request.
        """

        if local_date is None:
            local_date = datetime.now(UTC).astimezone(_timezone(timezone)).date()
        roofs_by_geometry: dict[tuple[float, float], list[PvRoof]] = {}
        for roof in roofs:
            geometry = (roof.tilt_deg, to_open_meteo_azimuth(roof.compass_azimuth_deg))
            roofs_by_geometry.setdefault(geometry, []).append(roof)

        forecasts = await asyncio.gather(
            *(
                self.async_fetch(
                    latitude,
                    longitude,
                    timezone,
                    tilt_deg=geometry[0],
                    open_meteo_azimuth_deg=geometry[1],
                    local_date=local_date,
                )
                for geometry in roofs_by_geometry
            )
        )
        result: dict[str, tuple[WeatherInterval, ...]] = {}
        for roof_group, forecast in zip(
            roofs_by_geometry.values(), forecasts, strict=True
        ):
            for roof in roof_group:
                result[roof.id] = forecast.intervals
        return result

    async def async_fetch(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        *,
        tilt_deg: float,
        open_meteo_azimuth_deg: float,
        local_date: date | None = None,
    ) -> OpenMeteoForecast:
        """Eine Forecast-Antwort für eine Dachgeometrie laden und validieren."""

        location_timezone = _timezone(timezone)
        if local_date is None:
            local_date = datetime.now(UTC).astimezone(location_timezone).date()
        day_start = datetime.combine(
            local_date, time.min, location_timezone
        ).astimezone(UTC)
        day_end = datetime.combine(
            local_date + timedelta(days=2), time.min, location_timezone
        ).astimezone(UTC)
        # GTI gehört zur vorhergehenden Stunde. Nur überlappende UTC-Intervalle
        # laden; Teilstunden-Zeitzonen benötigen anteilig ausgewertete Ränder.
        first_end = day_start.replace(minute=0, second=0, microsecond=0) + timedelta(
            hours=1
        )
        last_end = day_end.replace(minute=0, second=0, microsecond=0)
        if last_end < day_end:
            last_end += timedelta(hours=1)
        params: Mapping[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "global_tilted_irradiance,temperature_2m",
            "timezone": "UTC",
            "start_hour": first_end.strftime("%Y-%m-%dT%H:%M"),
            "end_hour": last_end.strftime("%Y-%m-%dT%H:%M"),
            "timeformat": "unixtime",
            "tilt": tilt_deg,
            "azimuth": open_meteo_azimuth_deg,
        }
        try:
            async with self._session.get(
                OPEN_METEO_FORECAST_URL,
                params=params,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                try:
                    payload = await response.json()
                except (ContentTypeError, ValueError, TypeError) as err:
                    raise OpenMeteoDataError("Antwort ist kein gültiges JSON") from err
        except (TimeoutError, ClientResponseError, ClientError) as err:
            raise OpenMeteoConnectionError("Open-Meteo-Abfrage fehlgeschlagen") from err

        forecast = parse_open_meteo_response(payload, timezone)
        expected_count = int((last_end - first_end).total_seconds() / 3600) + 1
        if len(forecast.intervals) != expected_count or any(
            interval.end.astimezone(UTC) != first_end + timedelta(hours=index)
            for index, interval in enumerate(forecast.intervals)
        ):
            raise OpenMeteoDataError(
                "Zeitreihe deckt den angeforderten Zeitraum nicht lückenlos ab"
            )
        return forecast


def _timezone(name: str) -> ZoneInfo:
    """Eine gespeicherte Anlagenzeitzone kontrolliert auflösen."""

    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as err:
        raise OpenMeteoDataError("Unbekannte Zeitzone") from err


def parse_open_meteo_response(
    payload: Any, requested_timezone: str
) -> OpenMeteoForecast:
    """Eine rohe API-Antwort in typisierte Stundenintervalle umwandeln."""

    if not isinstance(payload, dict) or payload.get("error") is True:
        raise OpenMeteoDataError("Open-Meteo meldet eine fehlerhafte Antwort")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise OpenMeteoDataError("Stündliche Wetterdaten fehlen")
    times = hourly.get("time")
    gti_values = hourly.get("global_tilted_irradiance")
    temperature_values = hourly.get("temperature_2m")
    if not isinstance(times, list) or not times:
        raise OpenMeteoDataError("Zeitstempel fehlen")
    if not isinstance(gti_values, list) or not isinstance(temperature_values, list):
        raise OpenMeteoDataError("Benötigte Wetterreihen fehlen")

    timezone = _timezone(requested_timezone)

    intervals: list[WeatherInterval] = []
    for index, raw_time in enumerate(times):
        try:
            if isinstance(raw_time, int | float) and not isinstance(raw_time, bool):
                end = datetime.fromtimestamp(raw_time, UTC).astimezone(timezone)
            else:
                raise ValueError
        except (OSError, OverflowError, ValueError) as err:
            raise OpenMeteoDataError("Ungültiger Zeitstempel") from err
        gti = _non_negative_number(
            gti_values[index] if index < len(gti_values) else None
        )
        ambient_temperature = _optional_number(
            temperature_values[index] if index < len(temperature_values) else None
        )
        intervals.append(
            WeatherInterval(
                start=(end.astimezone(UTC) - timedelta(hours=1)).astimezone(timezone),
                end=end,
                gti_w_m2=gti,
                ambient_temperature_c=ambient_temperature,
            )
        )
    return OpenMeteoForecast(intervals=tuple(intervals))


def _optional_number(value: Any) -> float | None:
    """Endliche Zahl liefern, ungültige oder fehlende Einzelwerte verwerfen."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _non_negative_number(value: Any) -> float:
    """Fehlendes oder ungültiges GTI gemäß Spezifikation als null behandeln."""

    numeric = _optional_number(value)
    return max(0.0, numeric) if numeric is not None else 0.0
