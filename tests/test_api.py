"""Tests für Transportgrenze und Open-Meteo-Parsing."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientError
from freezegun import freeze_time

from custom_components.pv_forecast.api import (
    OpenMeteoClient,
    OpenMeteoConnectionError,
    OpenMeteoDataError,
    parse_open_meteo_response,
)
from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.models import OpenMeteoForecast

from .helpers import TIMEZONE, roof, weather


def _payload() -> dict[str, object]:
    return {
        "timezone": "UTC",
        "hourly": {
            "time": [
                int(datetime(2026, 8, 22, 22, tzinfo=UTC).timestamp()),
                int(datetime(2026, 8, 22, 23, tzinfo=UTC).timestamp()),
            ],
            "global_tilted_irradiance": [None, -2],
            "temperature_2m": [20.0, None],
        },
    }


def _hourly_payload(start: str, end: str) -> dict[str, object]:
    """Referenzantwort mit konstantem GTI und inklusiver UTC-Endgrenze bauen."""

    first = datetime.fromisoformat(start).replace(tzinfo=UTC)
    last = datetime.fromisoformat(end).replace(tzinfo=UTC)
    count = int((last - first).total_seconds() / 3600) + 1
    return {
        "timezone": "UTC",
        "hourly": {
            "time": [
                int((first + timedelta(hours=hour)).timestamp())
                for hour in range(count)
            ],
            "global_tilted_irradiance": [1000] * count,
            "temperature_2m": [25] * count,
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
@freeze_time("2026-09-09T12:00:00+00:00")
@pytest.mark.parametrize(
    ("timezone", "local_day", "start", "end", "count", "today", "tomorrow"),
    [
        (
            "Europe/Berlin",
            date(2026, 9, 9),
            "2026-09-08T23:00",
            "2026-09-10T22:00",
            48,
            24,
            24,
        ),
        (
            "Europe/Berlin",
            date(2026, 3, 28),
            "2026-03-28T00:00",
            "2026-03-29T22:00",
            47,
            24,
            23,
        ),
        (
            "Europe/Berlin",
            date(2026, 10, 24),
            "2026-10-23T23:00",
            "2026-10-25T23:00",
            49,
            24,
            25,
        ),
        (
            "Asia/Kathmandu",
            date(2026, 9, 9),
            "2026-09-08T19:00",
            "2026-09-10T19:00",
            49,
            24,
            24,
        ),
        (
            "Australia/Lord_Howe",
            date(2026, 10, 3),
            "2026-10-02T14:00",
            "2026-10-04T13:00",
            48,
            24,
            23.5,
        ),
        (
            "Australia/Lord_Howe",
            date(2026, 4, 4),
            "2026-04-03T14:00",
            "2026-04-05T14:00",
            49,
            24,
            24.5,
        ),
    ],
    ids=[
        "normaler-tag",
        "sommerzeitbeginn",
        "sommerzeitende",
        "viertelstundenoffset",
        "halbstuendiger-sommerzeitbeginn",
        "halbstuendiges-sommerzeitende",
    ],
)
async def test_client_requests_complete_local_days_with_minimal_utc_window(
    timezone: str,
    local_day: date,
    start: str,
    end: str,
    count: int,
    today: float,
    tomorrow: float,
) -> None:
    """UTC-Randintervalle decken normale und verkürzte/verlängerte lokale Tage ab."""

    session = _Session(_Response(_hourly_payload(start, end)))
    client = OpenMeteoClient(session)
    forecast = await client.async_fetch(
        52,
        13,
        timezone,
        tilt_deg=30,
        open_meteo_azimuth_deg=0,
        local_date=local_day,
    )
    assert session.last_kwargs["params"] == {
        "latitude": 52,
        "longitude": 13,
        "hourly": "global_tilted_irradiance,temperature_2m",
        "timezone": "UTC",
        "start_hour": start,
        "end_hour": end,
        "timeformat": "unixtime",
        "tilt": 30,
        "azimuth": 0,
    }
    assert session.calls == 1
    assert len(forecast.intervals) == count
    assert all(point.duration_hours == 1 for point in forecast.intervals)
    result = calculate_forecast(
        (roof(power=1),),
        {"roof_1": forecast.intervals},
        None,
        local_day,
        ZoneInfo(timezone),
    )
    assert result.total.today == pytest.approx(today)
    assert result.total.tomorrow == pytest.approx(tomorrow)
    assert result.roofs["roof_1"].daily == result.total


@pytest.mark.asyncio
@freeze_time("2026-06-21T12:00:00+00:00")
async def test_last_hour_of_tomorrow_keeps_positive_polar_day_yield() -> None:
    """Positiver GTI der letzten morgigen Stunde im Polartag bleibt enthalten."""

    payload = _hourly_payload("2026-06-20T23:00", "2026-06-22T22:00")
    payload["hourly"]["global_tilted_irradiance"] = [0] * 47 + [500]
    session = _Session(_Response(payload))
    forecast = await OpenMeteoClient(session).async_fetch(
        69.65,
        18.96,
        "Europe/Oslo",
        tilt_deg=30,
        open_meteo_azimuth_deg=0,
    )
    assert session.last_kwargs["params"]["end_hour"] == "2026-06-22T22:00"
    last = forecast.intervals[-1]
    assert last.start.isoformat() == "2026-06-22T23:00:00+02:00"
    assert last.end.isoformat() == "2026-06-23T00:00:00+02:00"
    result = calculate_forecast(
        (roof(power=1),),
        {"roof_1": forecast.intervals},
        None,
        date(2026, 6, 21),
        ZoneInfo("Europe/Oslo"),
    )
    assert result.total.today == 0
    assert result.total.tomorrow == pytest.approx(0.5)


@pytest.mark.asyncio
@freeze_time("2026-09-09T10:15:00+00:00")
async def test_request_uses_local_date_when_it_differs_from_utc() -> None:
    """Nach lokaler Mitternacht gilt bereits der neue Tag trotz altem UTC-Datum."""

    session = _Session(_Response(_payload()))
    await OpenMeteoClient(session).async_fetch(
        1.87,
        -157.43,
        "Pacific/Kiritimati",
        tilt_deg=30,
        open_meteo_azimuth_deg=0,
    )
    assert session.last_kwargs["params"]["start_hour"] == "2026-09-09T11:00"
    assert session.last_kwargs["params"]["end_hour"] == "2026-09-11T10:00"


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
    [
        (object(), "Europe/Berlin"),
        ("kein Datum", "Europe/Berlin"),
        ("2026-08-23T00:00", "Europe/Berlin"),
        ("2026-08-23T00:00+00:00", "Europe/Berlin"),
        (True, "Europe/Berlin"),
        (0, "Mars/Base"),
    ],
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
    """Geometrien teilen Abrufe und behalten auch über Mitternacht denselben Tag."""

    client = OpenMeteoClient(_Session(_Response(_payload())))
    client.async_fetch = AsyncMock(
        side_effect=[OpenMeteoForecast((weather(),)), OpenMeteoForecast((weather(),))]
    )
    with patch("custom_components.pv_forecast.api.datetime", wraps=datetime) as clock:
        clock.now.side_effect = [
            datetime(2026, 9, 9, 21, 59, 59, tzinfo=UTC),
            datetime(2026, 9, 9, 22, 0, 0, tzinfo=UTC),
        ]
        result = await client.async_fetch_roofs(
            52,
            13,
            "Europe/Berlin",
            (roof("a", azimuth=180), roof("b", azimuth=180), roof("c", azimuth=90)),
        )
        clock.now.assert_called_once_with(UTC)
    assert client.async_fetch.await_count == 2
    assert set(result) == {"a", "b", "c"}
    assert result["a"] is result["b"]
    assert [call.kwargs for call in client.async_fetch.await_args_list] == [
        {
            "tilt_deg": 35,
            "open_meteo_azimuth_deg": 0,
            "local_date": date(2026, 9, 9),
        },
        {
            "tilt_deg": 35,
            "open_meteo_azimuth_deg": -90,
            "local_date": date(2026, 9, 9),
        },
    ]
