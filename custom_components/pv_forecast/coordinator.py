"""Gemeinsame Aktualisierung und Berechnung aller PV-Prognosen."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OpenMeteoClient, OpenMeteoError
from .calculations import InvalidConfigurationError, calculate_forecast
from .configuration import roofs_from_options
from .const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_TIME_ZONE,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .models import ForecastResult

_LOGGER = logging.getLogger(__name__)


class PvForecastCoordinator(DataUpdateCoordinator[ForecastResult]):
    """Lädt Wetterdaten zentral und erzeugt ein vollständiges Forecast-Ergebnis."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        client: OpenMeteoClient,
    ) -> None:
        """Coordinator initialisieren."""

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self._entry = entry
        self._client = client

    async def _async_update_data(self) -> ForecastResult:
        """Open-Meteo abrufen und die Prognose für alle Dachflächen berechnen."""

        try:
            latitude = float(self._entry.data[CONF_LATITUDE])
            longitude = float(self._entry.data[CONF_LONGITUDE])
            timezone_name = str(self._entry.data[CONF_TIME_ZONE])
            timezone = ZoneInfo(timezone_name)
            roofs = roofs_from_options(self._entry.options)
            raw_inverter_limit = self._entry.options.get(CONF_INVERTER_MAX_POWER_KW)
            inverter_limit = (
                float(raw_inverter_limit) if raw_inverter_limit is not None else None
            )
            weather_by_roof = await self._client.async_fetch_roofs(
                latitude, longitude, timezone_name, roofs
            )
            now: datetime = dt_util.now().astimezone(timezone)
            return calculate_forecast(
                roofs,
                weather_by_roof,
                inverter_limit,
                now.date(),
                timezone,
            )
        except (
            OpenMeteoError,
            InvalidConfigurationError,
            KeyError,
            ValueError,
            ZoneInfoNotFoundError,
        ) as err:
            raise UpdateFailed(
                f"PV-Prognose konnte nicht aktualisiert werden: {err}"
            ) from err
