"""Einrichtung der PV-Ertragsprognose-Integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OpenMeteoClient
from .const import PLATFORMS
from .coordinator import PvForecastCoordinator


@dataclass(slots=True)
class PvForecastRuntimeData:
    """Nur zur Laufzeit benötigte Objekte eines Config Entries."""

    coordinator: PvForecastCoordinator


type PvForecastConfigEntry = ConfigEntry[PvForecastRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> bool:
    """Integration aus einem Config Entry einrichten."""

    client = OpenMeteoClient(async_get_clientsession(hass))
    coordinator = PvForecastCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PvForecastRuntimeData(coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> bool:
    """Alle Plattformen eines Config Entries sauber entladen."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: PvForecastConfigEntry
) -> None:
    """Geänderte Optionen durch vollständiges Reload übernehmen."""

    await hass.config_entries.async_reload(entry.entry_id)
