"""Bewusster lesender Zugriff auf das lokale Prognosearchiv und seine Exporte."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .measurement_services import _async_check_source_permissions
from .services import CONF_CONFIG_ENTRY_ID, _async_check_read_permission

if TYPE_CHECKING:
    from . import PvForecastConfigEntry
    from .history_runtime import ArchiveManager

SERVICE_GET_HISTORY = "get_history"
SERVICE_EXPORT_HISTORY = "export_history"

_BASE_SCHEMA = {
    vol.Required(CONF_CONFIG_ENTRY_ID): vol.All(cv.string, vol.Length(min=1)),
    vol.Optional("days", default=30): vol.All(
        vol.In((7, 30, 90, "7", "30", "90")), vol.Coerce(int)
    ),
}
_HISTORY_SCHEMA = vol.Schema(
    {**_BASE_SCHEMA, vol.Optional("include_records", default=False): cv.boolean}
)
_EXPORT_SCHEMA = vol.Schema(
    {**_BASE_SCHEMA, vol.Required("format"): vol.In(("json", "csv"))}
)


@callback
def async_setup_history_services(hass: HomeAssistant) -> None:
    """Archivansicht und Export ohne automatische Veröffentlichung registrieren."""

    async def async_get_history(call: ServiceCall) -> ServiceResponse:
        manager = await _async_get_archive(hass, call)
        return manager.snapshot(
            call.data["days"],
            dt_util.utcnow(),
            include_records=call.data["include_records"],
        )

    async def async_export_history(call: ServiceCall) -> ServiceResponse:
        manager = await _async_get_archive(hass, call)
        output_format = call.data["format"]
        content = manager.export(call.data["days"], output_format, dt_util.utcnow())
        if output_format == "json":
            content = json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False)
        return {
            "schema_version": 1,
            "filename": f"pv-forecast-archiv-{call.data['days']}-tage.{output_format}",
            "mime_type": "application/json" if output_format == "json" else "text/csv",
            "content": content,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_HISTORY,
        async_get_history,
        schema=_HISTORY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_HISTORY,
        async_export_history,
        schema=_EXPORT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _async_get_archive(hass: HomeAssistant, call: ServiceCall) -> ArchiveManager:
    """Anlage und alle aufbewahrten externen Identitäten vor Ausgabe prüfen."""

    entry_id = call.data[CONF_CONFIG_ENTRY_ID]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_found"
        )
    await _async_check_read_permission(hass, call, entry_id)
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded"
        )
    manager = cast("PvForecastConfigEntry", entry).runtime_data.history
    if manager is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="history_unavailable"
        )
    await _async_check_source_permissions(hass, call, manager)
    return manager
