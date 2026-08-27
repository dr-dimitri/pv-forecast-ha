"""Kleine, deterministische Testdaten-Helfer."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.pv_forecast.const import (
    CONF_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_LOSS_FACTOR,
    CONF_NAME,
    CONF_ROOF_ID,
    CONF_TILT,
)
from custom_components.pv_forecast.models import PvRoof, WeatherInterval

TIMEZONE = ZoneInfo("Europe/Berlin")


def roof(
    roof_id: str = "roof_1",
    *,
    name: str = "Süddach",
    power: float = 10.0,
    azimuth: float = 180.0,
    tilt: float = 35.0,
    loss: float = 0.0,
) -> PvRoof:
    """Gültige Dachfläche erzeugen."""

    return PvRoof(roof_id, name, power, azimuth, tilt, loss)


def persisted_roof(
    roof_id: str = "roof_1", *, name: str = "Süddach"
) -> dict[str, object]:
    """Gültige persistierte Dachkonfiguration erzeugen."""

    return {
        CONF_ROOF_ID: roof_id,
        CONF_NAME: name,
        CONF_INSTALLED_POWER_KWP: 10.0,
        CONF_AZIMUTH: 180.0,
        CONF_TILT: 35.0,
        CONF_LOSS_FACTOR: 0.0,
    }


def weather(
    gti: float = 1000.0,
    temperature: float | None = 25.0,
    *,
    end: datetime | None = None,
    minutes: int = 60,
) -> WeatherInterval:
    """Wetterintervall mit expliziter Dauer erzeugen."""

    interval_end = end or datetime(2026, 8, 23, 12, tzinfo=TIMEZONE)
    return WeatherInterval(
        start=interval_end - timedelta(minutes=minutes),
        end=interval_end,
        gti_w_m2=gti,
        ambient_temperature_c=temperature,
    )
