"""Ertragssensoren für heute und morgen."""

from __future__ import annotations

from typing import Literal, override

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PvForecastConfigEntry
from .configuration import roofs_from_options
from .coordinator import PvForecastCoordinator
from .entity import PvForecastEntity

type ForecastDay = Literal["today", "tomorrow"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PvForecastConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Genau zwei Gesamtsensoren und zwei Sensoren je Dachfläche anlegen."""

    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        PvForecastTotalSensor(coordinator, entry, "today"),
        PvForecastTotalSensor(coordinator, entry, "tomorrow"),
    ]
    for roof in roofs_from_options(entry.options):
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
    def native_value(self) -> float:
        """Aktuelle Tagesprognose aus dem Coordinator lesen."""

        return round(getattr(self.coordinator.data.total, self._day), 2)


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
    def native_value(self) -> float:
        """Aktuelle Tagesprognose der Dachfläche lesen."""

        forecast = self.coordinator.data.roofs[self._roof_id]
        return round(getattr(forecast.daily, self._day), 2)
