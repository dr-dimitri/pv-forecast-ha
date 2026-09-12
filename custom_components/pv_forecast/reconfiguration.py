"""Standortwechsel erst nach vollständiger Prüfung über die Speichergrenzen führen."""

from __future__ import annotations

from asyncio import CancelledError
from copy import deepcopy
from pathlib import Path
from sys import float_info
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .calibration import CalibrationState
from .calibration_runtime import _calibration_store
from .const import CONF_TIME_ZONE
from .history import HistoryArchive
from .history_runtime import _history_store
from .measurement_runtime import _measurement_store
from .measurements import SourceConfig, SourceHistory
from .morning_runtime import _store as _morning_store
from .morning_runtime import validate_morning_store


class ReconfigurationChangedError(HomeAssistantError):
    """Eine parallele Änderung verhindert das Überschreiben ihres neueren Stands."""


def _checked_store_exists(store_path: str) -> bool:
    """Auch nach HA-Umbenennung einen ungeklärten defekten Speicher erkennen."""

    path = Path(store_path)
    if path.exists():
        return True
    if next(path.parent.glob(f"{path.name}.corrupt.*"), None) is not None:
        raise ValueError("Der Speicher enthält ungeklärte beschädigte Altdaten")
    return False


async def _async_validate_stores(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Alle Speicher im alten Standortkontext lesen, bevor eine Änderung erlaubt ist."""

    timezone = str(entry.data[CONF_TIME_ZONE])
    original_data = deepcopy(dict(entry.data))
    original_options = deepcopy(dict(entry.options))

    def check_unchanged() -> None:
        if dict(entry.data) != original_data or dict(entry.options) != original_options:
            raise ReconfigurationChangedError(
                "Die Anlage wurde während der Prüfung geändert"
            )

    async def load_checked(store: Store[dict[str, Any]]) -> dict[str, Any] | None:
        existed = await hass.async_add_executor_job(_checked_store_exists, store.path)
        check_unchanged()
        data = await store.async_load()
        check_unchanged()
        if existed and data is None:
            raise ValueError("Der vorhandene Speicher konnte nicht gelesen werden")
        return data

    async def load_persisted(store: Store[dict[str, Any]]) -> dict[str, Any] | None:
        await load_checked(store)
        # HA sichert Migrationen selbst, unterdrückt jedoch einige Schreibfehler.
        # Eine frische Instanz ohne Migration muss den aktuellen Stand lesen können.
        # Bewusst inzwischen gelöschte Daten bleiben gelöscht; nie erneut schreiben.
        persisted = Store[dict[str, Any]](
            hass, store.version, store.key, minor_version=store.minor_version
        )
        persisted.make_read_only()
        return await load_checked(persisted)

    measurements = await load_persisted(_measurement_store(hass, entry.entry_id))
    if measurements is not None:
        if not isinstance(measurements, dict) or not isinstance(
            measurements.get("sources"), dict
        ):
            raise ValueError("Der Messspeicher enthält keine gültige Quellenliste")
        for data in measurements["sources"].values():
            history = SourceHistory.from_dict(
                SourceConfig.from_dict(data["source"]), data, timezone, float_info.max
            )
            if any(
                context["location_id"] is None
                for context in history.to_dict()["segment_contexts"].values()
            ):
                raise ValueError(
                    "Der Messspeicher hat keinen bestätigten Standortbezug"
                )
    history = await load_persisted(_history_store(hass, entry.entry_id))
    if history is not None:
        HistoryArchive.from_dict(history["archive"], timezone)
    calibration = await load_persisted(_calibration_store(hass, entry.entry_id))
    if calibration is not None:
        CalibrationState.from_dict(calibration["state"])
    morning = await load_persisted(_morning_store(hass, entry.entry_id))
    if morning is not None:
        validate_morning_store(morning)


async def async_prepare_location_change(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Alte Manager stoppen und Migrationen vor dem Standortwechsel sichern."""

    original_data = deepcopy(dict(entry.data))
    original_options = deepcopy(dict(entry.options))
    await _async_validate_stores(hass, entry)
    was_loaded = entry.state is ConfigEntryState.LOADED
    if was_loaded and not await hass.config_entries.async_unload(entry.entry_id):
        raise HomeAssistantError("Die laufende Anlage konnte nicht entladen werden")
    try:
        if dict(entry.data) != original_data or dict(entry.options) != original_options:
            raise ReconfigurationChangedError("Die Anlage wurde beim Entladen geändert")
        await _async_validate_stores(hass, entry)
    except (Exception, CancelledError):
        if was_loaded:
            await hass.config_entries.async_setup(entry.entry_id)
        raise
