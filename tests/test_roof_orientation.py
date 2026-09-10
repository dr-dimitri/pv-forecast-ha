"""Exakte Kompasswinkel, Schnellrichtungen und gemeinsame Geometrieabrufe prüfen."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_forecast.api import OpenMeteoClient
from custom_components.pv_forecast.calculations import InvalidConfigurationError
from custom_components.pv_forecast.config_flow import _persisted_roof, _roof_schema
from custom_components.pv_forecast.configuration import roof_from_dict
from custom_components.pv_forecast.const import (
    CONF_AZIMUTH,
    CONF_CUSTOM_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_NAME,
    CONF_SYSTEM_EFFICIENCY,
    CONF_TILT,
    DIRECTION_TO_COMPASS_AZIMUTH,
    ROOF_DIRECTION_CUSTOM,
)
from custom_components.pv_forecast.models import OpenMeteoForecast

from .helpers import weather

FORM = {
    CONF_NAME: "Dachfläche",
    CONF_INSTALLED_POWER_KWP: 8,
    CONF_AZIMUTH: ROOF_DIRECTION_CUSTOM,
    CONF_TILT: 35,
    CONF_SYSTEM_EFFICIENCY: 90,
}


@pytest.mark.parametrize("azimuth", [-1, 360.1, float("nan"), float("inf")])
def test_custom_angle_validation_rejects_outside_compass_range(azimuth: float) -> None:
    """Auch außerhalb des Selectors dürfen ungültige Winkel nicht gespeichert werden."""

    with pytest.raises(InvalidConfigurationError):
        _persisted_roof(FORM | {CONF_CUSTOM_AZIMUTH: azimuth}, "stable_id")


@pytest.mark.parametrize(("direction", "azimuth"), DIRECTION_TO_COMPASS_AZIMUTH.items())
def test_direction_shortcuts_keep_exact_values(direction: str, azimuth: float) -> None:
    """Die Schnellrichtung gilt unabhängig vom ungenutzten Winkelfeld."""

    persisted = _persisted_roof(
        FORM | {CONF_AZIMUTH: direction, CONF_CUSTOM_AZIMUTH: 158.123456789},
        "stable_id",
    )
    assert persisted[CONF_AZIMUTH] == azimuth
    assert _roof_schema(persisted)({})[CONF_AZIMUTH] == direction


@pytest.mark.asyncio
async def test_exact_geometry_is_shared_without_merging_neighboring_angles() -> None:
    """Identische freie Winkel teilen GTI; nahe Winkel bleiben getrennt."""

    client = OpenMeteoClient(MagicMock())
    client._async_fetch = AsyncMock(return_value=OpenMeteoForecast((weather(),)))
    roofs = tuple(
        roof_from_dict(_persisted_roof(FORM | {CONF_CUSTOM_AZIMUTH: azimuth}, roof_id))
        for roof_id, azimuth in (
            ("a", 158.123456789),
            ("b", 158.123456789),
            ("c", 158.123456790),
        )
    )
    result = await client.async_fetch_roofs(52, 13, "Europe/Berlin", roofs)
    assert result["a"] is result["b"]
    assert client._async_fetch.await_count == 2
    angles = [
        call.kwargs["open_meteo_azimuth_deg"]
        for call in client._async_fetch.await_args_list
    ]
    assert angles == [158.123456789 - 180, 158.123456790 - 180]
