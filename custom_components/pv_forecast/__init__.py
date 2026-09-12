"""Einrichtung der PV-Ertragsprognose-Integration."""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import OpenMeteoError
from .calibration_runtime import CalibrationManager, async_remove_calibration_store
from .const import DOMAIN, PLATFORMS
from .coordinator import PvForecastCoordinator
from .dashboard import DashboardManager, dashboard_issue_id
from .forecast_cache_runtime import ForecastCacheManager, async_remove_forecast_cache
from .frontend import async_setup_frontend
from .history_runtime import ArchiveManager, async_remove_history_store
from .history_services import async_setup_history_services
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
    history: ArchiveManager | None = None
    calibration: CalibrationManager | None = None
    dashboard: DashboardManager | None = None
    forecast_cache: ForecastCacheManager | None = None


type PvForecastConfigEntry = ConfigEntry[PvForecastRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Die lesende Prognoseaktion unabhängig vom Ladezustand registrieren."""

    async_setup_services(hass)
    async_setup_measurement_services(hass)
    async_setup_history_services(hass)
    async_setup_frontend(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> bool:
    """Integration aus einem Config Entry einrichten."""

    client = async_get_open_meteo_client(hass)
    coordinator = PvForecastCoordinator(hass, entry, client)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    cache = ForecastCacheManager(hass, entry, coordinator)
    coordinator.forecast_cache = cache
    try:
        restored = await cache.async_load()
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryNotReady:
            cause = coordinator.last_exception
            if restored is None or not isinstance(
                getattr(cause, "__cause__", cause), OpenMeteoError
            ):
                raise
            coordinator.raw_data = coordinator.data = restored.forecast
            coordinator.last_update_success_time = restored.fetched_at
            coordinator.origin = "restored"
            coordinator.restored_at = dt_util.utcnow()
            coordinator.async_build_explanation()
    except (Exception, CancelledError):
        await cache.async_stop()
        raise
    coordinator.async_start_day_updates()
    coordinator.async_start_planning_updates()

    measurements = MeasurementManager(hass, entry)
    history = ArchiveManager(hass, entry, coordinator, measurements)
    calibration = CalibrationManager(hass, entry, coordinator, history)
    entry.runtime_data = PvForecastRuntimeData(
        coordinator, measurements, history, calibration, forecast_cache=cache
    )
    try:
        await measurements.async_start(fresh_after=coordinator.restored_at)
        await history.async_start()
        await calibration.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        dashboard = DashboardManager(hass, entry)
        entry.runtime_data.dashboard = dashboard
        entry.async_on_unload(dashboard.async_stop)
        await dashboard.async_sync()
    except (Exception, CancelledError):
        await cache.async_stop()
        if entry.runtime_data.dashboard is not None:
            entry.runtime_data.dashboard.async_stop()
        try:
            await calibration.async_stop()
        finally:
            try:
                await history.async_stop()
            finally:
                await measurements.async_stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> bool:
    """Alle Plattformen eines Config Entries sauber entladen."""

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        if entry.runtime_data.forecast_cache is not None:
            await entry.runtime_data.forecast_cache.async_stop(
                remove=not entry.runtime_data.forecast_cache.enabled
            )
        if entry.runtime_data.dashboard is not None:
            entry.runtime_data.dashboard.async_stop()
        try:
            if entry.runtime_data.calibration is not None:
                await entry.runtime_data.calibration.async_stop()
        finally:
            try:
                if entry.runtime_data.history is not None:
                    await entry.runtime_data.history.async_stop()
            finally:
                if entry.runtime_data.measurements is not None:
                    await entry.runtime_data.measurements.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PvForecastConfigEntry) -> None:
    """Beim Entfernen einer Anlage ihre lokalen Messdaten und ihr Archiv löschen."""

    await async_remove_forecast_cache(hass, entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, dashboard_issue_id(entry.entry_id))
    try:
        await async_remove_calibration_store(hass, entry.entry_id)
    finally:
        try:
            await async_remove_history_store(hass, entry.entry_id)
        finally:
            await async_remove_measurement_store(hass, entry.entry_id)


async def _async_update_listener(
    hass: HomeAssistant, entry: PvForecastConfigEntry
) -> None:
    """Dashboard lokal aktualisieren, fachliche Änderungen durch Reload übernehmen."""

    runtime = getattr(entry, "runtime_data", None)
    dashboard = getattr(runtime, "dashboard", None)
    history = getattr(runtime, "history", None)
    local_morning_update = (
        history is not None and history.async_update_morning_options()
    )
    if dashboard is not None and dashboard.only_dashboard_options_changed():
        if dashboard.display_options_changed():
            await dashboard.async_sync()
        return
    if local_morning_update:
        return
    await hass.config_entries.async_reload(entry.entry_id)
