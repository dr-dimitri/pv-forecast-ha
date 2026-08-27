"""Tests für Sensorwerte, Metadaten und stabile IDs."""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    RoofForecast,
)
from custom_components.pv_forecast.sensor import (
    PvForecastRoofSensor,
    PvForecastTotalSensor,
)

from .helpers import persisted_roof, roof


def _sensor_setup(hass, roof_name: str = "Süddach"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        entry_id="entry_1",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={CONF_ROOFS: [persisted_roof("stable_roof", name=roof_name)]},
    )
    entry.add_to_hass(hass)
    coordinator = PvForecastCoordinator(hass, entry, AsyncMock())
    coordinator.async_set_updated_data(
        ForecastResult(
            local_date=date(2026, 8, 23),
            roofs={
                "stable_roof": RoofForecast(
                    roof("stable_roof", name=roof_name), (), DailyYield(12.345, 20.126)
                )
            },
            total=DailyYield(12.345, 20.126),
        )
    )
    return entry, coordinator


@pytest.mark.asyncio
async def test_sensor_values_and_metadata(hass) -> None:
    """Sensoren liefern ausschließlich Tagesenergie in kWh."""

    entry, coordinator = _sensor_setup(hass)
    total = PvForecastTotalSensor(coordinator, entry, "today")
    roof_sensor = PvForecastRoofSensor(
        coordinator, entry, "stable_roof", "Süddach", "tomorrow"
    )
    assert total.native_value == 12.35
    assert roof_sensor.native_value == 20.13
    assert total.device_class is SensorDeviceClass.ENERGY
    assert total.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert total.state_class is None


@pytest.mark.asyncio
async def test_renaming_roof_does_not_change_unique_id(hass) -> None:
    """Die sichtbare Dachbezeichnung ist kein Teil der Entity-Identität."""

    entry, coordinator = _sensor_setup(hass, "Süddach")
    before = PvForecastRoofSensor(coordinator, entry, "stable_roof", "Süddach", "today")
    after = PvForecastRoofSensor(coordinator, entry, "stable_roof", "Garage", "today")
    assert before.unique_id == after.unique_id == "entry_1_stable_roof_today"


@pytest.mark.asyncio
async def test_removing_roof_removes_its_entities(hass) -> None:
    """Entfernte Dachflächen hinterlassen keine verwaisten Sensor-Entities."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={
            CONF_ROOFS: [
                persisted_roof("keep", name="Süddach"),
                persisted_roof("drop", name="Ostdach"),
            ]
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    unique_ids_before = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert unique_ids_before == {
        f"{entry.entry_id}_total_today",
        f"{entry.entry_id}_total_tomorrow",
        f"{entry.entry_id}_keep_today",
        f"{entry.entry_id}_keep_tomorrow",
        f"{entry.entry_id}_drop_today",
        f"{entry.entry_id}_drop_tomorrow",
    }

    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        hass.config_entries.async_update_entry(
            entry, options={CONF_ROOFS: [persisted_roof("keep", name="Süddach")]}
        )
        await hass.async_block_till_done()

    unique_ids_after = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert unique_ids_after == {
        f"{entry.entry_id}_total_today",
        f"{entry.entry_id}_total_tomorrow",
        f"{entry.entry_id}_keep_today",
        f"{entry.entry_id}_keep_tomorrow",
    }
