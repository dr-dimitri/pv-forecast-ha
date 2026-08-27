"""Tests der Coordinator-Orchestrierung."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import OpenMeteoConnectionError
from custom_components.pv_forecast.const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.coordinator import PvForecastCoordinator

from .helpers import TIMEZONE, persisted_roof, weather


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={
            CONF_ROOFS: [persisted_roof("a"), persisted_roof("b")],
            CONF_INVERTER_MAX_POWER_KW: 15,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_coordinator_uses_one_shared_client_update(hass) -> None:
    """Alle Dächer werden aus genau einem gebündelten Client-Aufruf berechnet."""

    client = AsyncMock()
    client.async_fetch_roofs.return_value = {
        "a": (weather(),),
        "b": (weather(),),
    }
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with patch(
        "custom_components.pv_forecast.coordinator.dt_util.now",
        return_value=datetime(2026, 8, 23, 10, tzinfo=TIMEZONE),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert client.async_fetch_roofs.await_count == 1
    assert set(coordinator.data.roofs) == {"a", "b"}
    assert coordinator.data.total.today == pytest.approx(15)


@pytest.mark.asyncio
async def test_coordinator_converts_api_error_to_update_failed(hass) -> None:
    """Externe Fehler werden in den HA-Coordinator-Lebenszyklus übersetzt."""

    client = AsyncMock()
    client.async_fetch_roofs.side_effect = OpenMeteoConnectionError("offline")
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
