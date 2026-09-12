"""Sensoren für Tagesprognosen und zeitabhängige Planungswerte."""

from __future__ import annotations

from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PvForecastConfigEntry
from .coordinator import PvForecastCoordinator
from .entity import PvForecastEntity
from .forecast_intervals import _valid_number
from .models import ForecastDay

PLANNING_SENSOR_DESCRIPTIONS = (
    SensorEntityDescription(
        key="remaining_today",
        translation_key="total_remaining_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="next_60_minutes",
        translation_key="total_next_60_minutes",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="power_now",
        translation_key="total_power_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="peak_today",
        translation_key="total_peak_today",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PvForecastConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sechs Gesamtsensoren und zwei Sensoren je Dachfläche anlegen."""

    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        PvForecastTotalSensor(coordinator, entry, "today"),
        PvForecastTotalSensor(coordinator, entry, "tomorrow"),
        *(
            PvForecastPlanningSensor(coordinator, entry, description)
            for description in PLANNING_SENSOR_DESCRIPTIONS
        ),
    ]
    for forecast in coordinator.data.roofs.values():
        roof = forecast.roof
        entities.extend(
            (
                PvForecastRoofSensor(coordinator, entry, roof.id, roof.name, "today"),
                PvForecastRoofSensor(
                    coordinator, entry, roof.id, roof.name, "tomorrow"
                ),
            )
        )
    async_add_entities(entities)
    _async_remove_stale_entities(hass, entry, {entity.unique_id for entity in entities})


def _async_remove_stale_entities(
    hass: HomeAssistant, entry: PvForecastConfigEntry, valid_unique_ids: set[str | None]
) -> None:
    """Registry-Einträge entfernter Dachflächen aus früheren Setups aufräumen."""

    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id not in valid_unique_ids:
            registry.async_remove(registry_entry.entity_id)


class PvForecastBaseSensor(PvForecastEntity, SensorEntity):
    """Gemeinsame Metadaten der Ertragsprognose-Sensoren."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: PvForecastCoordinator,
        entry: PvForecastConfigEntry,
        day: ForecastDay,
    ) -> None:
        """Basissensor initialisieren."""

        super().__init__(coordinator, entry)
        self._day = day

    @property
    @override
    def available(self) -> bool:
        """Nur gültig abgedeckte Zieltage mit erfolgreichem Datenstand anbieten."""

        return super().available and self.native_value is not None


class PvForecastTotalSensor(PvForecastBaseSensor):
    """Prognose der Gesamtanlage für einen Tag."""

    def __init__(
        self,
        coordinator: PvForecastCoordinator,
        entry: PvForecastConfigEntry,
        day: ForecastDay,
    ) -> None:
        """Gesamtsensor mit stabiler ID initialisieren."""

        super().__init__(coordinator, entry, day)
        self._attr_unique_id = f"{entry.entry_id}_total_{day}"
        self._attr_translation_key = f"total_{day}"

    @property
    @override
    def available(self) -> bool:
        """SAX liest nur den Zustand: veraltete oder unbrauchbare Daten ausblenden."""

        return super().available and self.coordinator.is_energy_forecast_available(
            self._day
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Aktuelle Tagesprognose aus dem Coordinator lesen."""

        value = self.coordinator.get_daily_yield(self._day)
        return round(value, 2) if _valid_number(value) else None


class PvForecastRoofSensor(PvForecastBaseSensor):
    """Prognose einer Dachfläche für einen Tag."""

    def __init__(
        self,
        coordinator: PvForecastCoordinator,
        entry: PvForecastConfigEntry,
        roof_id: str,
        roof_name: str,
        day: ForecastDay,
    ) -> None:
        """Dachsensor mit namensunabhängiger ID initialisieren."""

        super().__init__(coordinator, entry, day)
        self._roof_id = roof_id
        self._attr_unique_id = f"{entry.entry_id}_{roof_id}_{day}"
        self._attr_translation_key = f"roof_{day}"
        self._attr_translation_placeholders = {"roof_name": roof_name}

    @property
    @override
    def native_value(self) -> float | None:
        """Aktuelle Tagesprognose der Dachfläche lesen."""

        value = self.coordinator.get_daily_yield(self._day, self._roof_id)
        return round(value, 2) if _valid_number(value) else None


class PvForecastPlanningSensor(PvForecastEntity, SensorEntity):
    """Vorberechnete Stundenwerte der Gesamtanlage ohne eigenen Abruf abbilden."""

    def __init__(
        self,
        coordinator: PvForecastCoordinator,
        entry: PvForecastConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        """Planungssensor mit stabiler ID und seinen fachlichen Metadaten anlegen."""

        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_total_{description.key}"

    @property
    @override
    def available(self) -> bool:
        """Abdeckung beachten; ein vollständiger Nulltag hat keinen Spitzenwert."""

        if not super().available or self.coordinator.planning_values is None:
            return False
        if self.entity_description.key == "peak_today":
            return self.coordinator.planning_values.peak_today_complete
        if self.entity_description.key == "remaining_today":
            return (
                self.native_value is not None
                and self.coordinator.is_energy_forecast_available("remaining_today")
            )
        return self.native_value is not None

    @property
    @override
    def native_value(self) -> float | datetime | None:
        """Den im Coordinator vorbereiteten Planungswert lesen."""

        if (values := self.coordinator.planning_values) is None:
            return None
        match self.entity_description.key:
            case "remaining_today":
                value = values.remaining_today_kwh
            case "next_60_minutes":
                value = values.next_60_minutes_kwh
            case "power_now":
                value = values.power_now_kw
            case "peak_today":
                return values.peak_today
            case _:
                return None
        return round(value, 2) if _valid_number(value) else None
