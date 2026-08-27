"""Tests für die einmalige Adressauflösung."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError

from custom_components.pv_forecast.geocoding import (
    AddressNotFoundError,
    GeocodingConnectionError,
    GeocodingDataError,
    NominatimClient,
    parse_nominatim_response,
)


def _result(**overrides):
    result = {
        "lat": "52.51720765",
        "lon": "13.3978344",
        "display_name": "Pariser Platz 1, 10117 Berlin, Deutschland",
        "address": {"postcode": "10117", "country_code": "de"},
    }
    return result | overrides


def test_parse_valid_nominatim_response() -> None:
    """Koordinaten und Anzeigename werden normalisiert."""

    location = parse_nominatim_response([_result()], "10117", "DE")
    assert location.latitude == pytest.approx(52.51720765)
    assert location.longitude == pytest.approx(13.3978344)
    assert location.display_name.startswith("Pariser Platz")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        ["ungültig"],
        [_result(lat="nan")],
        [_result(address=None)],
        [_result(display_name="")],
    ],
)
def test_invalid_nominatim_responses_are_rejected(payload) -> None:
    """Strukturell unbrauchbare Antworten erzeugen einen definierten Fehler."""

    with pytest.raises(GeocodingDataError):
        parse_nominatim_response(payload, "10117", "DE")


@pytest.mark.parametrize(
    ("payload", "postal_code", "country"),
    [
        ([], "10117", "DE"),
        ([_result()], "99999", "DE"),
        ([_result()], "10117", "AT"),
    ],
)
def test_missing_or_mismatching_address_is_rejected(
    payload, postal_code: str, country: str
) -> None:
    """Nur ein Ergebnis mit passender PLZ und passendem Land wird akzeptiert."""

    with pytest.raises(AddressNotFoundError):
        parse_nominatim_response(payload, postal_code, country)


@pytest.mark.asyncio
async def test_client_uses_structured_single_request() -> None:
    """Nominatim erhält genau eine identifizierte strukturierte Suchanfrage."""

    response = AsyncMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = [_result()]
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = MagicMock()
    session.get.return_value = context

    location = await NominatimClient(session).async_geocode(
        "Pariser Platz 1", "10117", "DE", "de"
    )

    assert location.latitude == pytest.approx(52.51720765)
    session.get.assert_called_once()
    call = session.get.call_args
    assert call.kwargs["params"]["limit"] == 1
    assert call.kwargs["params"]["countrycodes"] == "de"
    assert "pv-forecast-ha" in call.kwargs["headers"]["User-Agent"]


@pytest.mark.asyncio
async def test_client_surfaces_transport_error() -> None:
    """Transportfehler werden in einen fachlichen Fehler übersetzt."""

    session = MagicMock()
    session.get.side_effect = ClientError("offline")
    with pytest.raises(GeocodingConnectionError):
        await NominatimClient(session).async_geocode(
            "Pariser Platz 1", "10117", "DE", "de"
        )
