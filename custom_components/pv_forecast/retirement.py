"""Ausgebaute Archivfunktionen ohne Einlesen ihrer Daten endgültig bereinigen."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_RETIRED_STORES = ("history", "calibration", "morning")
_RETIRED_OPTIONS = frozenset(
    {
        "history_enabled",
        "comparison_forecast",
        "calibration_mode",
        "calibration_exclusions",
        "temperature_comparison_enabled",
        "temperature_mountings",
        "short_term_enabled",
        "underperformance_enabled",
        "underperformance_notifications",
        "morning_mode",
        "morning_threshold_kw",
    }
)


def _remove_corrupt_copies(store_path: str) -> None:
    """Nur die von HA neben genau diesem Store abgelegten Fehlerkopien entfernen."""
    path = Path(store_path)
    for candidate in path.parent.glob(f"{path.name}.corrupt.*"):
        candidate.unlink(missing_ok=True)


async def async_remove_retired_stores(hass: HomeAssistant, entry_id: str) -> None:
    """Nur die drei ehemaligen Stores dieser Anlage unabhängig von Version löschen."""
    for name in _RETIRED_STORES:
        # Löschen benötigt weder Deserialisierung noch eine Storemigration.
        store = Store(hass, 1, f"{DOMAIN}.{name}.{entry_id}")
        await store.async_remove()
        await hass.async_add_executor_job(_remove_corrupt_copies, store.path)
        for domain, issue_id in tuple(ir.async_get(hass).issues):
            if domain == HOMEASSISTANT_DOMAIN and issue_id.startswith(
                f"storage_corruption_{store.key}_"
            ):
                ir.async_delete_issue(hass, domain, issue_id)
    persistent_notification.async_dismiss(hass, f"{DOMAIN}.observation.{entry_id}")


async def async_retire_archive(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Altbestände vor dem Setup löschen und danach veraltete Optionen entfernen."""
    try:
        await async_remove_retired_stores(hass, entry.entry_id)
    except OSError as err:
        raise ConfigEntryNotReady(
            "Die ehemaligen Archivdaten konnten nicht entfernt werden"
        ) from err
    options = {
        key: value
        for key, value in entry.options.items()
        if key not in _RETIRED_OPTIONS
    }
    if len(options) != len(entry.options):
        hass.config_entries.async_update_entry(entry, options=options)
