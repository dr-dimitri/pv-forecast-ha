"""Einmalige Adressauflösung über OpenStreetMap Nominatim."""

from __future__ import annotations

import math
from typing import Any

from aiohttp import (
    ClientError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
)

from .const import (
    NOMINATIM_SEARCH_URL,
    NOMINATIM_USER_AGENT,
    REQUEST_TIMEOUT_SECONDS,
)
from .models import GeocodedLocation


class GeocodingError(Exception):
    """Basisklasse für erwartete Fehler der Adressauflösung."""


class GeocodingConnectionError(GeocodingError):
    """Der Geocoding-Dienst konnte nicht erreicht werden."""


class AddressNotFoundError(GeocodingError):
    """Für die Anschrift wurde kein Standort gefunden."""


class GeocodingDataError(GeocodingError):
    """Der Geocoding-Dienst hat ungültige Daten geliefert."""


class NominatimClient:
    """Kleiner asynchroner Client für nutzergesteuerte Einzelabfragen."""

    def __init__(self, session: ClientSession) -> None:
        """Client mit der von Home Assistant verwalteten Session erstellen."""

        self._session = session

    async def async_geocode(
        self,
        street: str,
        postal_code: str,
        country_code: str,
        language: str,
    ) -> GeocodedLocation:
        """Eine strukturierte Anschrift in Koordinaten umwandeln."""

        params = {
            "street": street,
            "postalcode": postal_code,
            "countrycodes": country_code.lower(),
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
            "accept-language": language,
        }
        try:
            async with self._session.get(
                NOMINATIM_SEARCH_URL,
                params=params,
                headers={"User-Agent": NOMINATIM_USER_AGENT},
                timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                try:
                    payload = await response.json()
                except (ContentTypeError, TypeError, ValueError) as err:
                    raise GeocodingDataError(
                        "Geocoding-Antwort ist kein gültiges JSON"
                    ) from err
        except (TimeoutError, ClientResponseError, ClientError) as err:
            raise GeocodingConnectionError(
                "OpenStreetMap Nominatim konnte nicht erreicht werden"
            ) from err

        return parse_nominatim_response(payload, postal_code, country_code)


def parse_nominatim_response(
    payload: Any, requested_postal_code: str, requested_country_code: str
) -> GeocodedLocation:
    """Eine Nominatim-Antwort validieren und normalisieren."""

    if not isinstance(payload, list):
        raise GeocodingDataError("Geocoding-Antwort hat ein ungültiges Format")
    if not payload:
        raise AddressNotFoundError("Anschrift wurde nicht gefunden")
    result = payload[0]
    if not isinstance(result, dict):
        raise GeocodingDataError("Geocoding-Ergebnis hat ein ungültiges Format")

    try:
        latitude = float(result["lat"])
        longitude = float(result["lon"])
    except (KeyError, TypeError, ValueError) as err:
        raise GeocodingDataError("Koordinaten fehlen im Geocoding-Ergebnis") from err
    if (
        not math.isfinite(latitude)
        or not -90 <= latitude <= 90
        or not math.isfinite(longitude)
        or not -180 <= longitude <= 180
    ):
        raise GeocodingDataError("Geocoding-Ergebnis enthält ungültige Koordinaten")

    address = result.get("address")
    if not isinstance(address, dict):
        raise GeocodingDataError("Adressdetails fehlen im Geocoding-Ergebnis")
    returned_country = str(address.get("country_code", "")).upper()
    returned_postal_code = str(address.get("postcode", ""))
    if returned_country != requested_country_code.upper() or _compact_postal_code(
        returned_postal_code
    ) != _compact_postal_code(requested_postal_code):
        raise AddressNotFoundError(
            "Gefundener Standort stimmt nicht mit Land und Postleitzahl überein"
        )

    display_name = result.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise GeocodingDataError("Lesbarer Standortname fehlt")
    return GeocodedLocation(latitude, longitude, display_name.strip())


def _compact_postal_code(value: str) -> str:
    """Postleitzahlen ohne Leer- und Trennzeichen vergleichen."""

    return "".join(character for character in value.casefold() if character.isalnum())
