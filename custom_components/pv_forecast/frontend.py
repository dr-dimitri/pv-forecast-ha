"""Das optionale Kartenmodul lokal bereitstellen, ohne Dashboards zu verändern."""

from pathlib import Path

from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_when_setup

from .const import DOMAIN

CARD_URL = "/pv_forecast/pv-forecast-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "pv-forecast-card.js"
_DATA_REGISTERED = f"{DOMAIN}.frontend_registered"


@callback
def async_setup_frontend(hass: HomeAssistant) -> None:
    """HTTP optional abwarten und das Modul einmal pro HA-Laufzeit anmelden."""

    if hass.data.get(_DATA_REGISTERED):
        return
    hass.data[_DATA_REGISTERED] = True

    async def register_module(hass: HomeAssistant, _component: str) -> None:
        """Nur die feste, datenfreie JavaScript-Datei öffentlich ausliefern."""

        if hass.http is None:
            return
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_FILE), False)]
        )

    async_when_setup(hass, "http", register_module)
