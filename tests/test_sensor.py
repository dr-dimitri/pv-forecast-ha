"""Tests für Sensorwerte, Metadaten und stabile IDs."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast import PvForecastRuntimeData
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
    PlanningValues,
    RoofForecast,
)
from custom_components.pv_forecast.sensor import (
    PLANNING_SENSOR_DESCRIPTIONS,
    PvForecastPlanningSensor,
    PvForecastRoofSensor,
    PvForecastTotalSensor,
)
from custom_components.pv_forecast.sensor import (
    async_setup_entry as async_setup_sensor_entry,
)

from .helpers import TIMEZONE, persisted_roof, roof, weather


@pytest.fixture(autouse=True)
def fixed_sensor_date(freezer) -> None:
    """Sensor-Fixtures beziehen sich deterministisch auf den 23. August."""

    freezer.move_to("2026-08-23T12:00:00+00:00")


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
@pytest.mark.parametrize(("day", "expected"), [("today", 12.35), ("tomorrow", 20.13)])
async def test_sensor_values_and_metadata(hass, day, expected) -> None:
    """Tagesprognosen behalten ihre Energieeinheit und Statistikmetadaten."""

    entry, coordinator = _sensor_setup(hass)
    total = PvForecastTotalSensor(coordinator, entry, day)
    roof_sensor = PvForecastRoofSensor(
        coordinator, entry, "stable_roof", "Süddach", day
    )
    for sensor in (total, roof_sensor):
        assert sensor.native_value == expected
        assert sensor.device_class is SensorDeviceClass.ENERGY
        assert sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
        assert sensor.state_class is None


@pytest.mark.asyncio
async def test_planning_sensor_values_and_metadata(hass) -> None:
    """Planungswerte verwenden stabile IDs, passende Einheiten und keine Statistik."""

    entry, coordinator = _sensor_setup(hass)
    peak = datetime(2026, 8, 23, 10, tzinfo=UTC)
    coordinator.planning_values = PlanningValues(
        remaining_today_kwh=12.345,
        next_60_minutes_kwh=3.456,
        power_now_kw=2.567,
        peak_today=peak,
        peak_today_complete=True,
    )
    expectations = {
        "remaining_today": (
            12.35,
            SensorDeviceClass.ENERGY,
            UnitOfEnergy.KILO_WATT_HOUR,
        ),
        "next_60_minutes": (
            3.46,
            SensorDeviceClass.ENERGY,
            UnitOfEnergy.KILO_WATT_HOUR,
        ),
        "power_now": (2.57, SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
        "peak_today": (peak, SensorDeviceClass.TIMESTAMP, None),
    }
    for description in PLANNING_SENSOR_DESCRIPTIONS:
        sensor = PvForecastPlanningSensor(coordinator, entry, description)
        value, device_class, unit = expectations[description.key]
        assert sensor.native_value == value
        assert sensor.device_class is device_class
        assert sensor.native_unit_of_measurement == unit
        assert sensor.unique_id == f"entry_1_total_{description.key}"
        assert sensor.translation_key == f"total_{description.key}"
        assert sensor.state_class is None
        assert sensor.extra_state_attributes is None
        assert sensor.available


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_zero_day", [False, True])
async def test_planning_sensors_distinguish_zero_from_missing_coverage(
    hass, complete_zero_day: bool
) -> None:
    """Null ist ein verfügbarer Wert; der Nulltag hat ein unbekanntes Maximum."""

    entry, coordinator = _sensor_setup(hass)
    zero_or_missing = 0.0 if complete_zero_day else None
    coordinator.planning_values = PlanningValues(
        remaining_today_kwh=zero_or_missing,
        next_60_minutes_kwh=zero_or_missing,
        power_now_kw=zero_or_missing,
        peak_today=None,
        peak_today_complete=complete_zero_day,
    )
    for description in PLANNING_SENSOR_DESCRIPTIONS:
        sensor = PvForecastPlanningSensor(coordinator, entry, description)
        expected = None if description.key == "peak_today" else zero_or_missing
        assert sensor.native_value == expected
        assert sensor.available is complete_zero_day

    coordinator.async_set_update_error(UpdateFailed("Open-Meteo ist offline"))
    for description in PLANNING_SENSOR_DESCRIPTIONS:
        assert not PvForecastPlanningSensor(coordinator, entry, description).available


@pytest.mark.asyncio
async def test_planning_sensors_without_snapshot_are_unavailable(hass) -> None:
    """Vorhandene Entities können einen fehlenden Planungsstand sicher abbilden."""

    entry, coordinator = _sensor_setup(hass)
    coordinator.planning_values = None
    for description in PLANNING_SENSOR_DESCRIPTIONS:
        sensor = PvForecastPlanningSensor(coordinator, entry, description)
        assert sensor.native_value is None
        assert not sensor.available


@pytest.mark.asyncio
async def test_zero_day_is_available_with_unknown_peak_in_home_assistant(hass) -> None:
    """HA speichert den Nulltag als 0 und seine fehlende Spitze als unknown."""

    entry, _coordinator = _sensor_setup(hass)
    intervals = tuple(
        weather(
            0,
            end=datetime(2026, 8, 23, tzinfo=TIMEZONE) + timedelta(hours=hour),
        )
        for hour in range(1, 49)
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={"stable_roof": intervals},
    ) as fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        for description in PLANNING_SENSOR_DESCRIPTIONS:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"entry_1_total_{description.key}"
            )
            state = hass.states.get(entity_id)
            assert state is not None
            if description.key == "peak_today":
                assert state.state == STATE_UNKNOWN
            else:
                assert float(state.state) == 0
            assert "state_class" not in state.attributes
            assert "intervals" not in state.attributes
        fetch.assert_awaited_once()

        entry.runtime_data.coordinator.async_set_update_error(
            UpdateFailed("Open-Meteo ist offline")
        )
        await hass.async_block_till_done()
        assert all(
            hass.states.get(registry_entry.entity_id).state == STATE_UNAVAILABLE
            for registry_entry in er.async_entries_for_config_entry(
                registry, entry.entry_id
            )
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instant", "today", "tomorrow"),
    [
        ("2026-08-23T22:00:00+00:00", 20.13, None),
        ("2026-08-24T22:00:00+00:00", None, None),
    ],
    ids=["erster-lokaler-tageswechsel", "zweiter-lokaler-tageswechsel"],
)
async def test_sensor_values_follow_local_date_without_fresh_forecast(
    hass, freezer, instant: str, today: float | None, tomorrow: float | None
) -> None:
    """Altes Morgen wird heute; für noch nicht abgerufene Tage fehlt die Prognose."""

    entry, coordinator = _sensor_setup(hass)
    snapshot = coordinator.data
    sensors = [
        (
            PvForecastTotalSensor(coordinator, entry, day),
            PvForecastRoofSensor(coordinator, entry, "stable_roof", "Süddach", day),
        )
        for day in ("today", "tomorrow")
    ]
    freezer.move_to(instant)

    for day_sensors, expected in zip(sensors, (today, tomorrow), strict=True):
        for sensor in day_sensors:
            assert sensor.native_value == expected
            assert sensor.available is (expected is not None)
    assert coordinator.data is snapshot
    assert coordinator.last_update_success


@pytest.mark.asyncio
async def test_local_date_mapping_keeps_sensors_unavailable_after_api_error(
    hass, freezer
) -> None:
    """Ein vorhandener Tageswert überschreibt keinen fehlgeschlagenen API-Status."""

    entry, coordinator = _sensor_setup(hass)
    today_sensors = (
        PvForecastTotalSensor(coordinator, entry, "today"),
        PvForecastRoofSensor(coordinator, entry, "stable_roof", "Süddach", "today"),
    )
    snapshot = coordinator.data
    error = UpdateFailed("Open-Meteo ist vorübergehend nicht erreichbar")
    coordinator.async_set_update_error(error)
    freezer.move_to("2026-08-23T22:00:00+00:00")

    for sensor in today_sensors:
        assert sensor.native_value == 20.13
        assert not sensor.available
    assert coordinator.data is snapshot
    assert not coordinator.last_update_success
    assert coordinator.last_exception is error


@pytest.mark.asyncio
async def test_renaming_roof_does_not_change_unique_id(hass) -> None:
    """Die sichtbare Dachbezeichnung ist kein Teil der Entity-Identität."""

    entry, coordinator = _sensor_setup(hass, "Süddach")
    before = PvForecastRoofSensor(coordinator, entry, "stable_roof", "Süddach", "today")
    after = PvForecastRoofSensor(coordinator, entry, "stable_roof", "Garage", "today")
    assert before.unique_id == after.unique_id == "entry_1_stable_roof_today"


@pytest.mark.asyncio
@pytest.mark.parametrize("day", ["today", "tomorrow"])
async def test_roof_missing_from_snapshot_is_unavailable(hass, day: str) -> None:
    """Ein nicht mehr abgedecktes Dach erzeugt keinen Wert und keinen KeyError."""

    entry, coordinator = _sensor_setup(hass)
    sensor = PvForecastRoofSensor(coordinator, entry, "missing_roof", "Garage", day)

    assert sensor.native_value is None
    assert not sensor.available
    assert coordinator.last_update_success


@pytest.mark.asyncio
async def test_sensor_setup_uses_roofs_from_snapshot(hass) -> None:
    """Neue Optionen ändern die Sensoren erst zusammen mit dem nächsten Datenstand."""

    entry, coordinator = _sensor_setup(hass)
    entry.runtime_data = PvForecastRuntimeData(coordinator)
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_ROOFS: [
                persisted_roof("stable_roof", name="Name aus neueren Optionen"),
                persisted_roof("options_only_roof", name="Neues Dach"),
            ]
        },
    )
    add_entities = Mock()
    await async_setup_sensor_entry(hass, entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args.args[0]
    assert {entity.unique_id for entity in entities} == {
        "entry_1_total_today",
        "entry_1_total_tomorrow",
        "entry_1_total_remaining_today",
        "entry_1_total_next_60_minutes",
        "entry_1_total_power_now",
        "entry_1_total_peak_today",
        "entry_1_stable_roof_today",
        "entry_1_stable_roof_tomorrow",
    }
    roof_sensors = [
        entity for entity in entities if isinstance(entity, PvForecastRoofSensor)
    ]
    assert len(roof_sensors) == 2
    assert all(
        sensor.translation_placeholders == {"roof_name": "Süddach"}
        for sensor in roof_sensors
    )
    assert all(
        sensor.available
        for sensor in entities
        if not isinstance(sensor, PvForecastPlanningSensor)
    )
    assert all(
        not sensor.available
        for sensor in entities
        if isinstance(sensor, PvForecastPlanningSensor)
    )


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
        f"{entry.entry_id}_total_remaining_today",
        f"{entry.entry_id}_total_next_60_minutes",
        f"{entry.entry_id}_total_power_now",
        f"{entry.entry_id}_total_peak_today",
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
        f"{entry.entry_id}_total_remaining_today",
        f"{entry.entry_id}_total_next_60_minutes",
        f"{entry.entry_id}_total_power_now",
        f"{entry.entry_id}_total_peak_today",
        f"{entry.entry_id}_keep_today",
        f"{entry.entry_id}_keep_tomorrow",
    }
