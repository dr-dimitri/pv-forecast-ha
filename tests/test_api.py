"""Tests für Transportgrenze und Open-Meteo-Parsing."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError

from custom_components.pv_forecast.api import (
    OpenMeteoClient,
    OpenMeteoConnectionError,
    OpenMeteoDataError,
    parse_open_meteo_response,
)
from custom_components.pv_forecast.models import OpenMeteoForecast

from .helpers import TIMEZONE, roof, weather


def _payload() -> dict[str, object]:
    return {
        "timezone": "Europe/Berlin",
        "hourly": {
            "time": ["2026-08-23T00:00", "2026-08-23T01:00"],
            "global_tilted_irradiance": [None, -2],
            "temperature_2m": [20.0, None],
        },
    }


def test_parse_valid_response_and_missing_values() -> None:
    """Fehlendes/negatives GTI wird null, Temperatur darf fehlen."""

    forecast = parse_open_meteo_response(_payload(), "Europe/Berlin")
    assert len(forecast.intervals) == 2
    assert forecast.intervals[0].gti_w_m2 == 0
    assert forecast.intervals[1].gti_w_m2 == 0
    assert forecast.intervals[1].ambient_temperature_c is None
    assert forecast.intervals[0].end == datetime(2026, 8, 23, 0, tzinfo=TIMEZONE)
    assert forecast.intervals[0].duration_hours == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"error": True}, {"hourly": {}}, {"hourly": {"time": []}}],
)
def test_invalid_response_is_rejected(payload: object) -> None:
    """Kaputte API-Strukturen werden nicht als Forecast weitergereicht."""

    with pytest.raises(OpenMeteoDataError):
        parse_open_meteo_response(payload, "Europe/Berlin")


class _Response:
    def __init__(self, payload: object, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = 0
        self.last_kwargs = {}

    def get(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self.response


@pytest.mark.asyncio
async def test_client_surfaces_transport_error() -> None:
    """Netzwerkfehler werden als erwarteter Clientfehler gekapselt."""

    client = OpenMeteoClient(_Session(_Response({}, ClientError("offline"))))
    with pytest.raises(OpenMeteoConnectionError):
        await client.async_fetch(
            52, 13, "Europe/Berlin", tilt_deg=30, open_meteo_azimuth_deg=0
        )


@pytest.mark.asyncio
async def test_client_surfaces_invalid_json() -> None:
    """Nicht lesbares JSON ist ein Daten- und kein Verbindungsfehler."""

    client = OpenMeteoClient(_Session(_Response(ValueError("kein JSON"))))
    with pytest.raises(OpenMeteoDataError):
        await client.async_fetch(
            52, 13, "Europe/Berlin", tilt_deg=30, open_meteo_azimuth_deg=0
        )


@pytest.mark.asyncio
async def test_client_requests_exact_two_day_unix_forecast() -> None:
    """Der Request ist minimal und nutzt DST-eindeutige Unix-Zeitstempel."""

    session = _Session(_Response(_payload()))
    client = OpenMeteoClient(session)
    await client.async_fetch(
        52,
        13,
        "Europe/Berlin",
        tilt_deg=30,
        open_meteo_azimuth_deg=0,
    )
    assert session.last_kwargs["params"] == {
        "latitude": 52,
        "longitude": 13,
        "hourly": "global_tilted_irradiance,temperature_2m",
        "timezone": "Europe/Berlin",
        "forecast_days": 2,
        "timeformat": "unixtime",
        "tilt": 30,
        "azimuth": 0,
    }


def test_unix_timestamps_disambiguate_dst_fallback() -> None:
    """Beide lokalen 02:00-Stunden beim DST-Rücksprung bleiben unterscheidbar."""

    first = int(datetime(2026, 10, 25, 0, tzinfo=UTC).timestamp())
    second = int(datetime(2026, 10, 25, 1, tzinfo=UTC).timestamp())
    forecast = parse_open_meteo_response(
        {
            "hourly": {
                "time": [first, second],
                "global_tilted_irradiance": [100, 100],
                "temperature_2m": [10, 10],
            }
        },
        "Europe/Berlin",
    )
    assert forecast.intervals[0].end.hour == 2
    assert forecast.intervals[1].end.hour == 2
    assert (
        forecast.intervals[0].end.utcoffset() != forecast.intervals[1].end.utcoffset()
    )
    assert all(point.duration_hours == 1 for point in forecast.intervals)


@pytest.mark.parametrize(
    ("timestamp", "timezone"),
    [(object(), "Europe/Berlin"), ("kein Datum", "Europe/Berlin"), (0, "Mars/Base")],
)
def test_invalid_time_metadata_is_rejected(timestamp: object, timezone: str) -> None:
    """Ungültige Zeitstempel und Zeitzonen werden früh abgewiesen."""

    with pytest.raises(OpenMeteoDataError):
        parse_open_meteo_response(
            {
                "hourly": {
                    "time": [timestamp],
                    "global_tilted_irradiance": [100],
                    "temperature_2m": [20],
                }
            },
            timezone,
        )


@pytest.mark.asyncio
async def test_roofs_share_requests_for_equal_geometry() -> None:
    """Gleiche Dachgeometrien lösen keinen doppelten API-Aufruf aus."""

    client = OpenMeteoClient(_Session(_Response(_payload())))
    client.async_fetch = AsyncMock(
        side_effect=[OpenMeteoForecast((weather(),)), OpenMeteoForecast((weather(),))]
    )
    result = await client.async_fetch_roofs(
        52,
        13,
        "Europe/Berlin",
        (roof("a", azimuth=180), roof("b", azimuth=180), roof("c", azimuth=90)),
    )
    assert client.async_fetch.await_count == 2
    assert set(result) == {"a", "b", "c"}
