"""Tests für Transportgrenze und Open-Meteo-Parsing."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientError, ClientResponseError, ContentTypeError
from freezegun import freeze_time

from custom_components.pv_forecast.api import (
    OpenMeteoClient,
    OpenMeteoConnectionError,
    OpenMeteoDataError,
    OpenMeteoRateLimitError,
    OpenMeteoRequestState,
    OpenMeteoRetryError,
    OpenMeteoRetryPendingError,
    OpenMeteoTemporaryError,
    parse_open_meteo_response,
)
from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.const import REQUEST_TIMEOUT_SECONDS
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


@pytest.mark.parametrize("sign", [1, -1], ids=["positiv", "negativ"])
@pytest.mark.parametrize("field", ["global_tilted_irradiance", "temperature_2m"])
def test_weather_integer_overflow_only_discards_affected_value(
    field: str, sign: int
) -> None:
    """Überlaufende Einzelwerte lassen das übrige Intervall und die Reihe intakt."""

    payload = _payload()
    payload["hourly"]["global_tilted_irradiance"] = [500, 1000]
    payload["hourly"]["temperature_2m"] = [20, 25]
    payload["hourly"][field][0] = sign * 10**400

    forecast = parse_open_meteo_response(payload, "Europe/Berlin")

    first, second = forecast.intervals
    assert first.gti_w_m2 == (0 if field == "global_tilted_irradiance" else 500)
    assert first.ambient_temperature_c == (None if field == "temperature_2m" else 20)
    assert first.duration_hours == 1
    assert second.gti_w_m2 == 1000
    assert second.ambient_temperature_c == 25


@pytest.mark.parametrize(
    ("value", "expected_gti", "expected_temperature"),
    [
        (0, 0, 0),
        (1000, 1000, 1000),
        (-3, 0, -3),
        (12.5, 12.5, 12.5),
        (None, 0, None),
        (True, 0, None),
        (False, 0, None),
        ("20", 0, None),
        ([], 0, None),
        ({}, 0, None),
        (float("nan"), 0, None),
        (float("inf"), 0, None),
        (float("-inf"), 0, None),
    ],
)
def test_weather_number_validation_preserves_existing_value_policy(
    value: object, expected_gti: float, expected_temperature: float | None
) -> None:
    """Normale Zahlen, negative Werte und ungültige Typen behalten ihre Behandlung."""

    payload = _payload()
    payload["hourly"]["global_tilted_irradiance"][0] = value
    payload["hourly"]["temperature_2m"][0] = value

    first = parse_open_meteo_response(payload, "Europe/Berlin").intervals[0]

    assert first.gti_w_m2 == expected_gti
    assert first.ambient_temperature_c == expected_temperature


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
@pytest.mark.parametrize("timezone", ["Asia/Tokyo", "Europe/Berlin", "Etc/UTC"])
async def test_timezone_resolution_requests_only_metadata(timezone: str) -> None:
    """Der einmalige Zeitzonenabruf benötigt keine Wetterdaten oder Dachgeometrie."""

    session = _Session(_Response({"timezone": timezone}))
    resolved = await OpenMeteoClient(session).async_resolve_timezone(35.68, 139.69)

    assert resolved == timezone
    assert session.calls == 1
    assert session.last_kwargs["params"] == {
        "latitude": 35.68,
        "longitude": 139.69,
        "timezone": "auto",
    }
    assert session.last_kwargs["timeout"].total == REQUEST_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_timezone_resolution_preserves_dst_rules() -> None:
    """Die IANA-Zone behält Sommerzeitregeln trotz eines aktuell festen API-Offsets."""

    session = _Session(
        _Response(
            {
                "timezone": "America/New_York",
                "utc_offset_seconds": -14400,
                "timezone_abbreviation": "EDT",
            }
        )
    )
    resolved = await OpenMeteoClient(session).async_resolve_timezone(40.71, -74.01)

    assert resolved == "America/New_York"
    timezone = ZoneInfo(resolved)
    assert datetime(2026, 1, 15, tzinfo=timezone).utcoffset() == timedelta(hours=-5)
    assert datetime(2026, 7, 15, tzinfo=timezone).utcoffset() == timedelta(hours=-4)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"error": True, "timezone": "Asia/Tokyo"},
        {"timezone": None},
        {"timezone": False},
        {"timezone": ["Asia/Tokyo"]},
        {"timezone": ""},
        {"timezone": " "},
        {"timezone": "auto"},
        {"timezone": "Mars/Base"},
        {"timezone": "/UTC"},
    ],
    ids=[
        "null",
        "liste",
        "fehlende-zone",
        "api-fehler",
        "zone-null",
        "zone-bool",
        "zone-liste",
        "leere-zone",
        "leerzeichen",
        "nicht-aufgeloest",
        "unbekannte-zone",
        "ungueltiger-zonenschluessel",
    ],
)
async def test_timezone_resolution_rejects_invalid_metadata(payload: object) -> None:
    """Fehlende oder ungültige Standortzeitzonen führen zu keinem stillen Fallback."""

    client = OpenMeteoClient(_Session(_Response(payload)))
    with pytest.raises(OpenMeteoDataError):
        await client.async_resolve_timezone(35.68, 139.69)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("kein JSON"),
        ContentTypeError(None, (), status=200, message="kein JSON-Inhaltstyp"),
    ],
    ids=["ungueltiges-json", "ungueltiger-inhaltstyp"],
)
async def test_timezone_resolution_rejects_invalid_json(error: Exception) -> None:
    """Ungültiger JSON-Inhalt bleibt auch beim Metadatenabruf ein Datenfehler."""

    client = OpenMeteoClient(_Session(_Response(error)))
    with pytest.raises(OpenMeteoDataError):
        await client.async_resolve_timezone(35.68, 139.69)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ClientError("offline"),
        ClientResponseError(None, (), status=503, message="Dienst nicht verfügbar"),
        TimeoutError(),
    ],
    ids=["verbindungsfehler", "http-fehler", "timeout"],
)
async def test_timezone_resolution_surfaces_transport_errors(error: Exception) -> None:
    """Verbindungs- und HTTP-Fehler sowie Timeouts behalten ihren Fehlertyp."""

    client = OpenMeteoClient(_Session(_Response({}, error)))
    with pytest.raises(OpenMeteoConnectionError):
        await client.async_resolve_timezone(35.68, 139.69)


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
@pytest.mark.parametrize(
    "defect",
    [
        "anfang-fehlt",
        "schluss-fehlt",
        "innere-luecke",
        "duplikat",
        "widerspruechliches-duplikat",
        "fremder-zeitpunkt",
        "verschobenes-raster",
        "unsortiertes-raster",
    ],
)
async def test_client_rejects_incomplete_or_unexpected_hourly_grid(defect: str) -> None:
    """Unvollständige oder abweichende Zeitachsen sind kontrollierte Datenfehler."""

    payload = _hourly_payload("2026-09-08T23:00", "2026-09-10T22:00")
    hourly = payload["hourly"]
    times = hourly["time"]
    if defect in ("anfang-fehlt", "schluss-fehlt", "innere-luecke"):
        missing_index = {"anfang-fehlt": 0, "schluss-fehlt": -1, "innere-luecke": 20}[
            defect
        ]
        for values in hourly.values():
            del values[missing_index]
    elif defect in ("duplikat", "widerspruechliches-duplikat"):
        times[20] = times[19]
        if defect == "widerspruechliches-duplikat":
            hourly["global_tilted_irradiance"][20] = 500
    elif defect == "fremder-zeitpunkt":
        times[-1] += 3600
    elif defect == "verschobenes-raster":
        hourly["time"] = [timestamp + 1800 for timestamp in times]
    elif defect == "unsortiertes-raster":
        times[19], times[20] = times[20], times[19]

    client = OpenMeteoClient(_Session(_Response(payload)))
    with pytest.raises(OpenMeteoDataError):
        await client.async_fetch(
            52,
            13,
            "Europe/Berlin",
            tilt_deg=30,
            open_meteo_azimuth_deg=0,
            local_date=date(2026, 9, 9),
        )


@pytest.mark.asyncio
async def test_complete_hourly_grid_accepts_missing_weather_at_boundaries() -> None:
    """Vorhandene Randzeitpunkte dürfen weiterhin fehlende Wetterwerte enthalten."""

    payload = _hourly_payload("2026-09-08T23:00", "2026-09-10T22:00")
    for index in (0, -1):
        payload["hourly"]["global_tilted_irradiance"][index] = None
        payload["hourly"]["temperature_2m"][index] = None
    forecast = await OpenMeteoClient(_Session(_Response(payload))).async_fetch(
        52,
        13,
        "Europe/Berlin",
        tilt_deg=30,
        open_meteo_azimuth_deg=0,
        local_date=date(2026, 9, 9),
    )
    assert len(forecast.intervals) == 48
    for interval in (forecast.intervals[0], forecast.intervals[-1]):
        assert interval.gti_w_m2 == 0
        assert interval.ambient_temperature_c is None


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

    session = _Session(
        _Response(_hourly_payload("2026-09-09T11:00", "2026-09-11T10:00"))
    )
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
    client._async_fetch = AsyncMock(
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
    assert client._async_fetch.await_count == 2
    assert set(result) == {"a", "b", "c"}
    assert result["a"] is result["b"]
    assert [call.kwargs for call in client._async_fetch.await_args_list] == [
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


def _http_error(status: int, retry_after: str | None = None) -> ClientResponseError:
    """Eine HTTP-Fehlerantwort mit optionaler Anbieterpause erzeugen."""

    return ClientResponseError(
        None,
        (),
        status=status,
        headers={"Retry-After": retry_after} if retry_after is not None else None,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_http_error(429), OpenMeteoRateLimitError),
        *(
            (_http_error(status), OpenMeteoTemporaryError)
            for status in (408, 500, 502, 503, 504)
        ),
        (TimeoutError(), OpenMeteoTemporaryError),
        (ClientError("Verbindung abgebrochen"), OpenMeteoTemporaryError),
        (_http_error(400), OpenMeteoConnectionError),
        (_http_error(401), OpenMeteoConnectionError),
        (_http_error(404), OpenMeteoConnectionError),
        (_http_error(501), OpenMeteoConnectionError),
    ],
)
async def test_http_errors_have_controlled_retry_classification(
    error: Exception, expected: type[OpenMeteoConnectionError]
) -> None:
    """Nur Ratenbegrenzung und festgelegte temporäre Fehler erzeugen eine Pause."""

    session = _Session(_Response({}, error))
    client = OpenMeteoClient(session)
    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000):
        with pytest.raises(expected) as raised:
            await client.async_resolve_timezone(52, 13)
        assert type(raised.value) is expected
        if isinstance(error, ClientResponseError):
            assert f"HTTP {error.status}" in str(raised.value)
        if issubclass(expected, OpenMeteoRetryError):
            assert client.retry_after == 3600
        else:
            assert client.retry_after is None
    assert session.calls == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("90", 90),
        (" 120 ", 120),
        ("28800", 28800),
        ("100000000000000000000", 1e20),
        ("Wed, 09 Sep 2026 14:00:00 GMT", 7200),
        ("Wednesday, 09-Sep-26 14:00:00 GMT", 7200),
        ("Wed Sep  9 14:00:00 2026", 7200),
        (
            "Tuesday, 09-Sep-70 12:00:00 GMT",
            (
                datetime(2070, 9, 9, 12, tzinfo=UTC)
                - datetime(2026, 9, 9, 12, tzinfo=UTC)
            ).total_seconds(),
        ),
        (
            "Wednesday, 09-Sep-76 12:00:00 GMT",
            (
                datetime(2076, 9, 9, 12, tzinfo=UTC)
                - datetime(2026, 9, 9, 12, tzinfo=UTC)
            ).total_seconds(),
        ),
        ("Wednesday, 09-Sep-76 12:00:01 GMT", 3600),
        ("Thursday, 09-Sep-77 12:00:00 GMT", 3600),
        (None, 3600),
        ("0", 3600),
        ("-20", 3600),
        ("+20", 3600),
        ("1.5", 3600),
        ("1e4", 3600),
        ("nan", 3600),
        ("inf", 3600),
        ("١٢٠", 3600),
        ("", 3600),
        ("unbekannt", 3600),
        ("9" * 400, 3600),
        ("Wed, 09 Sep 2026 12:00:00 GMT", 3600),
        ("Wed, 09 Sep 2026 11:00:00 GMT", 3600),
        ("Wed, 09 Sep 2026 14:00:00", 3600),
        ("Wed, 09 Sep 2026 14:00:00 +0000", 3600),
    ],
)
@freeze_time("2026-09-09T12:00:00+00:00")
async def test_retry_after_header_validation(
    header: str | None, expected: float
) -> None:
    """Positive Anbieterfristen gelten; unbrauchbare Angaben nutzen den Backoff."""

    client = OpenMeteoClient(_Session(_Response({}, _http_error(429, header))))
    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000):
        with pytest.raises(OpenMeteoRateLimitError):
            await client.async_resolve_timezone(52, 13)
        assert client.retry_after == expected


@freeze_time("2090-09-09T12:00:00+00:00")
async def test_rfc850_year_resolves_across_century_boundary() -> None:
    """Das Rohjahr 00 gehört bei Empfang 2090 zu 2100 statt fest zu 2000."""

    client = OpenMeteoClient(
        _Session(_Response({}, _http_error(503, "Thursday, 09-Sep-00 12:00:00 GMT")))
    )
    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000):
        with pytest.raises(OpenMeteoTemporaryError):
            await client.async_resolve_timezone(52, 13)
        assert (
            client.retry_after
            == (
                datetime(2100, 9, 9, 12, tzinfo=UTC)
                - datetime(2090, 9, 9, 12, tzinfo=UTC)
            ).total_seconds()
        )


async def test_shared_pause_blocks_every_public_operation_without_http() -> None:
    """Clients für Flow und Forecast beachten dieselbe verbleibende Abrufpause."""

    state = OpenMeteoRequestState()
    session = _Session(_Response({}, _http_error(429, "7200")))
    first = OpenMeteoClient(session, request_state=state)
    second = OpenMeteoClient(session, request_state=state)
    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000) as now:
        with pytest.raises(OpenMeteoRateLimitError):
            await first.async_resolve_timezone(52, 13)
        now.return_value += 60
        assert second.retry_after == 7140
        with pytest.raises(OpenMeteoRetryPendingError):
            await second.async_fetch_roofs(52, 13, "Europe/Berlin", (roof(),))
        with pytest.raises(OpenMeteoRetryPendingError):
            await second.async_fetch(
                52, 13, "Europe/Berlin", tilt_deg=35, open_meteo_azimuth_deg=0
            )
        with pytest.raises(OpenMeteoRetryPendingError):
            await second.async_resolve_timezone(52, 13)
        assert session.calls == 1
        now.return_value += 7140
        assert first.retry_after is None
        with pytest.raises(OpenMeteoRateLimitError):
            await second.async_resolve_timezone(52, 13)
        assert session.calls == 2


async def test_backoff_caps_and_only_validated_success_resets_failure_sequence() -> (
    None
):
    """60/120/240 Minuten zählen Operationen; ein kaputtes 200 ist kein Erfolg."""

    session = _Session(_Response({}, _http_error(503)))
    client = OpenMeteoClient(session)
    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000) as now:
        for expected in (3600, 7200, 14400, 14400):
            with pytest.raises(OpenMeteoTemporaryError):
                await client.async_resolve_timezone(52, 13)
            assert client.retry_after == expected
            now.return_value += expected

        session.response = _Response({"timezone": "ungültig"})
        with pytest.raises(OpenMeteoDataError):
            await client.async_resolve_timezone(52, 13)
        assert client.retry_after is None
        session.response = _Response({}, _http_error(503))
        with pytest.raises(OpenMeteoTemporaryError):
            await client.async_resolve_timezone(52, 13)
        assert client.retry_after == 14400
        now.return_value += 14400

        session.response = _Response({"timezone": "Europe/Berlin"})
        assert await client.async_resolve_timezone(52, 13) == "Europe/Berlin"
        assert client.retry_after is None
        session.response = _Response({}, _http_error(503))
        with pytest.raises(OpenMeteoTemporaryError):
            await client.async_resolve_timezone(52, 13)
        assert client.retry_after == 3600


@pytest.mark.parametrize("grouped", [False, True])
async def test_only_complete_forecast_operation_resets_backoff(grouped: bool) -> None:
    """Erst validierte Stundenraster beenden die Fehlerfolge eines Forecasts."""

    session = _Session(_Response({}, _http_error(503)))
    client = OpenMeteoClient(session)

    async def fetch():
        if grouped:
            return await client.async_fetch_roofs(
                52,
                13,
                "Europe/Berlin",
                (roof("a"), roof("b", azimuth=90)),
                local_date=date(2026, 9, 9),
            )
        return await client.async_fetch(
            52,
            13,
            "Europe/Berlin",
            tilt_deg=35,
            open_meteo_azimuth_deg=0,
            local_date=date(2026, 9, 9),
        )

    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000) as now:
        with pytest.raises(OpenMeteoTemporaryError):
            await fetch()
        now.return_value += 3600
        session.response = _Response(_payload())
        with pytest.raises(OpenMeteoDataError):
            await fetch()
        session.response = _Response({}, _http_error(503))
        with pytest.raises(OpenMeteoTemporaryError):
            await fetch()
        assert client.retry_after == 7200
        now.return_value += 7200
        session.response = _Response(
            _hourly_payload("2026-09-08T23:00", "2026-09-10T22:00")
        )
        await fetch()
        session.response = _Response({}, _http_error(503))
        with pytest.raises(OpenMeteoTemporaryError):
            await fetch()
        assert client.retry_after == 3600


class _GatedResponse(_Response):
    """Laufende Antworten gezielt freigeben oder durch Abbruch beenden."""

    def __init__(self, payload: object, error: Exception | None = None) -> None:
        super().__init__(payload, error)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelling = asyncio.Event()
        self.cancel_cleanup_release = None
        self.cancelled = False
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.current_task()
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self.cancelling.set()
            if self.cancel_cleanup_release is not None:
                await self.cancel_cleanup_release.wait()
            self.finished.set()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.finished.set()
        return False


class _SequenceSession:
    """Getrennte Antworten ohne echten Transport in Aufrufreihenfolge liefern."""

    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = []

    def get(self, *args, **kwargs):
        response = self.responses[len(self.calls)]
        self.calls.append(kwargs)
        return response


async def test_parallel_wave_keeps_longest_pause_and_counts_one_failure() -> None:
    """Vier laufende Geometrien werden beendet, wartende starten nach 429 nicht."""

    payload = _hourly_payload("2026-09-08T23:00", "2026-09-10T22:00")
    responses = [
        _GatedResponse({}, _http_error(429, "120")),
        _GatedResponse({}, _http_error(503, "28800")),
        _GatedResponse(payload),
        _GatedResponse({}, _http_error(503, "3600")),
    ]
    session = _SequenceSession(responses)
    client = OpenMeteoClient(session)
    with patch("custom_components.pv_forecast.api.monotonic", return_value=1000) as now:
        operation = asyncio.create_task(
            client.async_fetch_roofs(
                52,
                13,
                "Europe/Berlin",
                tuple(roof(str(index), tilt=index) for index in range(6)),
                local_date=date(2026, 9, 9),
            )
        )
        await asyncio.gather(*(response.entered.wait() for response in responses))
        assert len(session.calls) == 4
        for index, expected in ((0, 120), (2, 120), (1, 28800), (3, 28800)):
            responses[index].release.set()
            await responses[index].finished.wait()
            assert client.retry_after == expected
            assert len(session.calls) == 4
        with pytest.raises(OpenMeteoRateLimitError):
            await operation
        assert all(response.task.done() for response in responses)
        assert client.retry_after == 28800

        now.return_value += 28800
        session.responses.append(_Response({}, _http_error(503)))
        with pytest.raises(OpenMeteoTemporaryError):
            await client.async_resolve_timezone(52, 13)
        assert client.retry_after == 7200


async def test_expired_short_pause_still_stops_failed_wave() -> None:
    """Eine abgelaufene Frist startet keine weiteren Dächer derselben Fehlerwelle."""

    payload = _hourly_payload("2026-09-08T23:00", "2026-09-10T22:00")
    responses = [_GatedResponse({}, _http_error(429, "1"))] + [
        _GatedResponse(payload) for _ in range(3)
    ]
    session = _SequenceSession([*responses, _Response(payload), _Response(payload)])
    client = OpenMeteoClient(session)
    failure_recorded = False

    def monotonic_time():
        nonlocal failure_recorded
        if responses[0].finished.is_set():
            if failure_recorded:
                return 1002
            failure_recorded = True
        return 1000

    with patch("custom_components.pv_forecast.api.monotonic", monotonic_time):
        operation = asyncio.create_task(
            client.async_fetch_roofs(
                52,
                13,
                "Europe/Berlin",
                tuple(roof(str(index), tilt=index) for index in range(6)),
                local_date=date(2026, 9, 9),
            )
        )
        await asyncio.gather(*(response.entered.wait() for response in responses))
        for response in responses:
            response.release.set()
        with pytest.raises(OpenMeteoRateLimitError):
            await operation
        assert client.retry_after is None
        assert len(session.calls) == 4
        session.responses[4] = _Response({"timezone": "Europe/Berlin"})
        assert await client.async_resolve_timezone(52, 13) == "Europe/Berlin"
        assert len(session.calls) == 5


async def test_distinct_clients_serialize_whole_operations() -> None:
    """Ein wartender Einzelabruf beginnt erst nach allen Geometrien der Welle."""

    payload = _hourly_payload("2026-09-08T23:00", "2026-09-10T22:00")
    responses = [_GatedResponse(payload), _GatedResponse(payload)]
    session = _SequenceSession([*responses, _Response({"timezone": "Europe/Berlin"})])
    state = OpenMeteoRequestState()
    first = OpenMeteoClient(session, state)
    second = OpenMeteoClient(session, state)
    grouped = asyncio.create_task(
        first.async_fetch_roofs(
            52,
            13,
            "Europe/Berlin",
            (roof("a"), roof("b", azimuth=90)),
            local_date=date(2026, 9, 9),
        )
    )
    await asyncio.gather(*(response.entered.wait() for response in responses))
    metadata = asyncio.create_task(second.async_resolve_timezone(52, 13))
    await asyncio.sleep(0)
    assert len(session.calls) == 2
    responses[0].release.set()
    await responses[0].finished.wait()
    assert len(session.calls) == 2
    assert not metadata.done()
    responses[1].release.set()
    assert set(await grouped) == {"a", "b"}
    assert await metadata == "Europe/Berlin"
    assert len(session.calls) == 3


async def test_cancelled_wave_drains_active_and_waiting_geometry_tasks() -> None:
    """Abbruch räumt alle Unteraufgaben auf und gibt die Operation wieder frei."""

    responses = [_GatedResponse({}) for _ in range(4)]
    responses[0].cancel_cleanup_release = asyncio.Event()
    session = _SequenceSession([*responses, _Response({"timezone": "Europe/Berlin"})])
    state = OpenMeteoRequestState()
    client = OpenMeteoClient(session, state)
    other_client = OpenMeteoClient(session, state)
    operation = asyncio.create_task(
        client.async_fetch_roofs(
            52,
            13,
            "Europe/Berlin",
            tuple(roof(str(index), tilt=index) for index in range(6)),
            local_date=date(2026, 9, 9),
        )
    )
    await asyncio.gather(*(response.entered.wait() for response in responses))
    metadata = asyncio.create_task(other_client.async_resolve_timezone(52, 13))
    await asyncio.sleep(0)
    assert len(session.calls) == 4
    operation.cancel()
    await responses[0].cancelling.wait()
    assert not operation.done()
    assert not metadata.done()
    assert len(session.calls) == 4
    responses[0].cancel_cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert all(response.cancelled for response in responses)
    assert all(response.task.done() for response in responses)
    assert await metadata == "Europe/Berlin"
    assert len(session.calls) == 5
    assert client.retry_after is None
