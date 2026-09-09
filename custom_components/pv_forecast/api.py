"""Asynchroner Client für die Open-Meteo-Forecast-API."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from time import monotonic
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

_HTTP_DATE_PATTERN = re.compile(
    r"(?:[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|[A-Z][a-z]+, [0-9]{2}-[A-Z][a-z]{2}-(?P<short_year>[0-9]{2}) "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|[A-Z][a-z]{2} [A-Z][a-z]{2} [ 0-9][0-9] "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4})"
)


class OpenMeteoError(Exception):
    """Basisklasse für erwartete Open-Meteo-Fehler."""


class OpenMeteoConnectionError(OpenMeteoError):
    """Open-Meteo konnte nicht erreicht werden."""


class OpenMeteoDataError(OpenMeteoError):
    """Open-Meteo hat eine unbrauchbare Antwort geliefert."""


class OpenMeteoRetryError(OpenMeteoConnectionError):
    """Ein vorübergehender Fehler erfordert eine gemeinsame Abrufpause."""


class OpenMeteoRateLimitError(OpenMeteoRetryError):
    """Open-Meteo begrenzt weitere Anfragen mit HTTP 429."""


class OpenMeteoTemporaryError(OpenMeteoRetryError):
    """Transport oder Dienst sind vorübergehend nicht verfügbar."""


class OpenMeteoRetryPendingError(OpenMeteoRetryError):
    """Eine laufende Abrufpause verhindert einen weiteren HTTP-Aufruf."""


class OpenMeteoRequestState:
    """Flüchtige Abrufpause und Parallelitätsgrenzen gemeinsam verwalten."""

    def __init__(self) -> None:
        self._retry_deadline = 0.0
        self._consecutive_failures = 0
        self._operation_failed = False
        self._operation_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(4)

    @property
    def retry_after(self) -> float | None:
        """Verbleibende Pause unabhängig von Änderungen der Wanduhr liefern."""

        remaining = self._retry_deadline - monotonic()
        return remaining if remaining > 0 else None

    def _raise_if_paused(self) -> None:
        if self.retry_after is not None:
            raise OpenMeteoRetryPendingError("Open-Meteo-Abrufpause läuft noch")

    def _record_temporary_failure(self, retry_after: str | None = None) -> None:
        """Sofort pausieren; mehrere Fehler derselben Welle nur einmal zählen."""

        delay = _retry_after_seconds(retry_after)
        if delay is None:
            delay = 3600 * 2 ** min(self._consecutive_failures, 2)
        self._retry_deadline = max(self._retry_deadline, monotonic() + delay)
        self._operation_failed = True


class OpenMeteoClient:
    """Kapselt ausschließlich Transport und Parsing der Open-Meteo-Daten."""

    def __init__(
        self,
        session: ClientSession,
        request_state: OpenMeteoRequestState | None = None,
    ) -> None:
        """Client mit einer von Home Assistant verwalteten Session initialisieren."""

        self._session = session
        self._request_state = (
            request_state if request_state is not None else OpenMeteoRequestState()
        )

    @property
    def retry_after(self) -> float | None:
        """Die derzeit verbleibende gemeinsame Abrufpause liefern."""

        return self._request_state.retry_after

    @asynccontextmanager
    async def _async_operation(self) -> AsyncIterator[None]:
        """Eine vollständige Metadaten- oder Prognoseoperation serialisieren."""

        state = self._request_state
        async with state._operation_lock:
            state._raise_if_paused()
            state._operation_failed = False
            succeeded = False
            try:
                yield
                succeeded = True
            finally:
                if state._operation_failed:
                    state._consecutive_failures = min(
                        state._consecutive_failures + 1, 3
                    )
                elif succeeded:
                    state._consecutive_failures = 0
                    state._retry_deadline = 0.0

    async def async_resolve_timezone(self, latitude: float, longitude: float) -> str:
        """Die IANA-Zeitzone einer Anschrift einmalig über Metadaten bestimmen."""

        async with self._async_operation():
            payload = await self._async_request(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "timezone": "auto",
                }
            )
            if not isinstance(payload, dict) or payload.get("error") is True:
                raise OpenMeteoDataError("Open-Meteo meldet eine fehlerhafte Antwort")
            timezone = payload.get("timezone")
            if not isinstance(timezone, str) or not timezone:
                raise OpenMeteoDataError("Zeitzone fehlt in der Open-Meteo-Antwort")
            return _timezone(timezone).key

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

        async with self._async_operation():
            if local_date is None:
                local_date = datetime.now(UTC).astimezone(_timezone(timezone)).date()
            roofs_by_geometry: dict[tuple[float, float], list[PvRoof]] = {}
            for roof in roofs:
                geometry = (
                    roof.tilt_deg,
                    to_open_meteo_azimuth(roof.compass_azimuth_deg),
                )
                roofs_by_geometry.setdefault(geometry, []).append(roof)

            tasks = [
                asyncio.create_task(
                    self._async_fetch(
                        latitude,
                        longitude,
                        timezone,
                        tilt_deg=geometry[0],
                        open_meteo_azimuth_deg=geometry[1],
                        local_date=local_date,
                    )
                )
                for geometry in roofs_by_geometry
            ]
            try:
                forecasts = await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            # Schon laufende Requests können noch längere Anbieterpausen melden.
            # Erst nach der gesamten Welle Fehler weiterreichen; wartende Requests
            # werden bereits vor ihrem HTTP-Aufruf von der gemeinsamen Pause geblockt.
            for forecast in forecasts:
                if isinstance(
                    forecast, OpenMeteoRateLimitError | OpenMeteoTemporaryError
                ):
                    raise forecast
            for forecast in forecasts:
                if isinstance(forecast, BaseException):
                    raise forecast
            result: dict[str, tuple[WeatherInterval, ...]] = {}
            for roof_group, forecast in zip(
                roofs_by_geometry.values(), forecasts, strict=True
            ):
                assert isinstance(forecast, OpenMeteoForecast)
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

        async with self._async_operation():
            return await self._async_fetch(
                latitude,
                longitude,
                timezone,
                tilt_deg=tilt_deg,
                open_meteo_azimuth_deg=open_meteo_azimuth_deg,
                local_date=local_date,
            )

    async def _async_fetch(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        *,
        tilt_deg: float,
        open_meteo_azimuth_deg: float,
        local_date: date | None = None,
    ) -> OpenMeteoForecast:
        """Eine Geometrie innerhalb der bereits laufenden Operation validieren."""

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
        payload = await self._async_request(params)
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

    async def _async_request(self, params: Mapping[str, str | int | float]) -> Any:
        """JSON für Wetter- und Metadaten mit derselben Fehlerbehandlung abrufen."""

        state = self._request_state
        async with state._request_slots:
            if state._operation_failed:
                raise OpenMeteoRetryPendingError(
                    "Weitere Geometrien der fehlgeschlagenen Abfrage entfallen"
                )
            state._raise_if_paused()
            try:
                async with self._session.get(
                    OPEN_METEO_FORECAST_URL,
                    params=params,
                    timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                ) as response:
                    response.raise_for_status()
                    try:
                        return await response.json()
                    except (ContentTypeError, ValueError, TypeError) as err:
                        raise OpenMeteoDataError(
                            "Antwort ist kein gültiges JSON"
                        ) from err
            except ClientResponseError as err:
                if err.status == 429 or err.status in {408, 500, 502, 503, 504}:
                    state._record_temporary_failure(
                        err.headers.get("Retry-After") if err.headers else None
                    )
                    if err.status == 429:
                        raise OpenMeteoRateLimitError(
                            "Open-Meteo begrenzt weitere Anfragen (HTTP 429)"
                        ) from err
                    raise OpenMeteoTemporaryError(
                        f"Open-Meteo vorübergehend nicht verfügbar (HTTP {err.status})"
                    ) from err
                raise OpenMeteoConnectionError(
                    f"Open-Meteo-Abfrage fehlgeschlagen (HTTP {err.status})"
                ) from err
            except (TimeoutError, ClientError) as err:
                state._record_temporary_failure()
                raise OpenMeteoTemporaryError(
                    "Open-Meteo-Abfrage vorübergehend fehlgeschlagen"
                ) from err


def _retry_after_seconds(value: str | None) -> float | None:
    """Positive ASCII-Sekunden oder ein zukünftiges HTTP-Datum validieren."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdecimal():
            delay = float(value)
        else:
            date_match = _HTTP_DATE_PATTERN.fullmatch(value)
            if date_match is None:
                return None
            now = datetime.now(UTC)
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                # Das alte HTTP-asctime-Format hat keine ausgeschriebene Zone,
                # bezeichnet aber gemäß RFC 9110 Abschnitt 5.6.7 ebenfalls UTC.
                deadline = deadline.replace(tzinfo=UTC)
            if short_year := date_match.group("short_year"):
                # RFC850-Jahre sind relativ zum Empfangsdatum auszulegen, nicht
                # mit der festen 1969/2068-Grenze des E-Mail-Parsers.
                year = now.year + 50 - (now.year + 50 - int(short_year)) % 100
                if year == now.year + 50 and (
                    deadline.month,
                    deadline.day,
                    deadline.time(),
                ) > (now.month, now.day, now.time()):
                    year -= 100
                deadline = deadline.replace(year=year)
            delay = (deadline - now).total_seconds()
    except (OverflowError, TypeError, ValueError):
        return None
    return delay if math.isfinite(delay) and delay > 0 else None


def _timezone(name: str) -> ZoneInfo:
    """Eine Anlagenzeitzone kontrolliert auflösen."""

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
        raw_gti = _optional_number(
            gti_values[index] if index < len(gti_values) else None
        )
        gti = max(0.0, raw_gti) if raw_gti is not None else 0.0
        ambient_temperature = _optional_number(
            temperature_values[index] if index < len(temperature_values) else None
        )
        quality_flags: list[str] = []
        if raw_gti is None or raw_gti < 0:
            quality_flags.append("gti_fallback")
        if ambient_temperature is None:
            quality_flags.append("temperature_fallback")
        intervals.append(
            WeatherInterval(
                start=(end.astimezone(UTC) - timedelta(hours=1)).astimezone(timezone),
                end=end,
                gti_w_m2=gti,
                ambient_temperature_c=ambient_temperature,
                quality_flags=tuple(quality_flags),
            )
        )
    return OpenMeteoForecast(intervals=tuple(intervals))


def _optional_number(value: Any) -> float | None:
    """Endliche Zahl liefern, ungültige oder fehlende Einzelwerte verwerfen."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        numeric = float(value)
    except OverflowError:
        return None
    return numeric if math.isfinite(numeric) else None
