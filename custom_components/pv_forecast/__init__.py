"""Einrichtung der PV-Ertragsprognose-Integration."""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import PvForecastCoordinator
from .measurement_runtime import MeasurementManager, async_remove_measurement_store
from .measurement_services import async_setup_measurement_services
from .runtime import async_get_open_meteo_client
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class PvForecastRuntimeData:
    """Nur zur Laufzeit benötigte Objekte eines Config Entries."""

    coordinator: PvForecastCoordinator
    measurements: MeasurementManager | None = None


type PvForecastConfigEntry = ConfigEntry[PvForecastRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Die lesende Prognoseaktion unabhängig vom Ladezustand registrieren."""

    async_setup_services(hass)
    async_setup_measurement_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> bool:
    """Integration aus einem Config Entry einrichten."""

    client = async_get_open_meteo_client(hass)
    coordinator = PvForecastCoordinator(hass, entry, client)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_day_updates()
    coordinator.async_start_planning_updates()

    measurements = MeasurementManager(hass, entry)
    entry.runtime_data = PvForecastRuntimeData(coordinator, measurements)
    try:
        await measurements.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, CancelledError):
        await measurements.async_stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> bool:
    """Alle Plattformen eines Config Entries sauber entladen."""

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.measurements is not None:
        await entry.runtime_data.measurements.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> None:
    """Beim Entfernen einer Anlage ausschließlich deren Messdaten löschen."""

    await async_remove_measurement_store(hass, entry.entry_id)


async def _async_update_listener(
    hass: HomeAssistant, entry: PvForecastConfigEntry
) -> None:
    """Geänderte Optionen durch vollständiges Reload übernehmen."""

    await hass.config_entries.async_reload(entry.entry_id)
