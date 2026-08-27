"""Gemeinsames Verhalten aller PV-Forecast-Entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PvForecastConfigEntry
from .const import DOMAIN
from .coordinator import PvForecastCoordinator


class PvForecastEntity(CoordinatorEntity[PvForecastCoordinator]):
    """Coordinator-basierte Entity der PV-Ertragsprognose."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PvForecastCoordinator, entry: PvForecastConfigEntry
    ) -> None:
        """Entity mit gemeinsamem Gerät verknüpfen."""

        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )
