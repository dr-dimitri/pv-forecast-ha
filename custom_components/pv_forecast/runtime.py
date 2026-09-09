"""Gemeinsame Open-Meteo-Abrufpause innerhalb einer HA-Laufzeit."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OpenMeteoClient, OpenMeteoRequestState
from .const import DOMAIN


@callback
def async_get_open_meteo_client(hass: HomeAssistant) -> OpenMeteoClient:
    """Anbieterpausen auch über Setup-, Flow- und Reload-Versuche hinweg erhalten."""

    request_state = hass.data.get(DOMAIN)
    if request_state is None:
        request_state = OpenMeteoRequestState()
        hass.data[DOMAIN] = request_state
    return OpenMeteoClient(async_get_clientsession(hass), request_state=request_state)
