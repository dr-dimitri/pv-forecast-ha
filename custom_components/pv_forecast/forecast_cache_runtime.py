"""Atomare, geordnete Speicherung des optionalen letzten Rohmodellstands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .configuration import inverter_groups_from_options, roofs_from_options
from .const import CONF_INVERTER_MAX_POWER_KW, CONF_TIME_ZONE, DOMAIN
from .forecast_cache import (
    CACHE_VERSION,
    CONF_FORECAST_CACHE,
    MAX_CACHE_BYTES,
    CachedForecast,
    decode_forecast,
    encode_forecast,
)
from .history_runtime import _configuration_id
from .horizon import forecast_days_from_options

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import PvForecastCoordinator


class _ForecastStore(Store[dict[str, Any]]):
    """Native atomare Schreibungen um Größen- und Fehlerstatus ergänzen."""

    write_status = "available"

    async def _async_write_data(self, data: dict) -> None:
        if len(json.dumps(data, allow_nan=False, indent=2).encode()) > MAX_CACHE_BYTES:
            self.write_status = "storage_limit"
            return
        try:
            await super()._async_write_data(data)
        except Exception:
            self.write_status = "storage_unavailable"
            raise
        self.write_status = "available"


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return _ForecastStore(
        hass,
        CACHE_VERSION,
        f"{DOMAIN}.forecast_cache.{entry_id}",
        private=True,
        atomic_writes=True,
        encoder=json.JSONEncoder,
    )


async def async_remove_forecast_cache(hass: HomeAssistant, entry_id: str) -> None:
    """Ausschließlich den Cache dieser Anlage bewusst entfernen."""
    await _store(hass, entry_id).async_remove()


class ForecastCacheManager:
    """Erfolgreiche Abrufe geordnet speichern und Schreibende beim Unload abwarten."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: PvForecastCoordinator,
    ) -> None:
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        self._store = _store(hass, entry.entry_id)
        self.configuration_id = _configuration_id(entry)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._generation = 0
        self._closed = False
        self._blocked = False
        self.status = "empty" if self.enabled else "disabled"

    @property
    def enabled(self) -> bool:
        return self.entry.options.get(CONF_FORECAST_CACHE) is True

    def _decode(self, data: dict[str, Any]) -> CachedForecast:
        return decode_forecast(
            data,
            entry_id=self.entry.entry_id,
            configuration_id=self.configuration_id,
            timezone_name=str(self.entry.data[CONF_TIME_ZONE]),
            forecast_days=forecast_days_from_options(self.entry.options),
            roofs=roofs_from_options(self.entry.options),
            groups=inverter_groups_from_options(self.entry.options),
            limit=self.entry.options.get(CONF_INVERTER_MAX_POWER_KW),
            now=dt_util.utcnow(),
        )

    async def async_load(self) -> CachedForecast | None:
        if not self.enabled:
            return None
        try:

            def size() -> int:
                path = Path(self._store.path)
                return path.stat().st_size if path.exists() else 0

            if await self.hass.async_add_executor_job(size) > MAX_CACHE_BYTES:
                self.status = "storage_limit"
                self._blocked = True
                return None
            data = await self._store.async_load()
            if data is None:
                return None
            result = self._decode(data)
        except NotImplementedError:
            self.status = "unsupported_version"
            self._blocked = True
        except (HomeAssistantError, OSError):
            self.status = "storage_unavailable"
            self._blocked = True
        except (ValueError, TypeError, KeyError, OverflowError):
            self.status = "invalid_snapshot"
        else:
            self.status = "available"
            return result
        return None

    @callback
    def async_capture(self) -> None:
        """Eine kohärente Generation vor lokalen Faktorwechseln festhalten."""
        if (
            self._closed
            or self._blocked
            or not self.enabled
            or self.coordinator.raw_data is None
        ):
            return
        if _configuration_id(self.entry) != self.configuration_id:
            return
        try:
            payload = encode_forecast(
                self.coordinator.raw_data,
                self.coordinator.last_update_success_time,
                self.entry.entry_id,
                self.configuration_id,
                str(self.entry.data[CONF_TIME_ZONE]),
                self.entry.options.get(CONF_INVERTER_MAX_POWER_KW),
            )
            self._decode(payload)
        except OverflowError:
            self.status = "storage_limit"
            return
        except (ValueError, TypeError, KeyError):
            self.status = "invalid_snapshot"
            return
        self._generation += 1
        task = self.hass.async_create_task(
            self._save(payload, self._generation),
            "PV-Prognosecache speichern",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _save(self, payload: dict[str, Any], generation: int) -> None:
        async with self._lock:
            if not self.enabled or generation != self._generation:
                return
            try:
                await self._store.async_save(payload)
            except (HomeAssistantError, OSError):
                self.status = "storage_unavailable"
            else:
                self.status = self._store.write_status

    async def async_stop(self, *, remove: bool = False) -> None:
        self._closed = True
        if remove:
            self._generation += 1
        if self._tasks:
            await asyncio.gather(*self._tasks)
        if remove:
            async with self._lock:
                await self._store.async_remove()
            self.status = "disabled"
