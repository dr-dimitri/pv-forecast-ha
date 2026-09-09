"""Rein lesende HA-Aktion für den gemeinsamen Stundenvertrag."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_READ
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .card_data import UnknownRoofError, build_forecast_view
from .const import CONF_TIME_ZONE, DOMAIN
from .models import ForecastResult

if TYPE_CHECKING:
    from . import PvForecastConfigEntry

SERVICE_GET_FORECAST = "get_forecast"
CONF_CONFIG_ENTRY_ID = "config_entry_id"

_GET_FORECAST_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("include_view", default=False): cv.boolean,
        vol.Optional("day", default="today"): vol.In(("today", "tomorrow")),
        vol.Optional("roof_id"): vol.All(cv.string, vol.Length(min=1)),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Die Leseaktion unabhängig von geladenen Anlagen registrieren."""

    async def async_get_forecast(call: ServiceCall) -> ServiceResponse:
        """Einen vorhandenen Snapshot ohne Aktualisierung ausgeben."""

        entry_id = call.data[CONF_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                "Die ausgewählte PV-Anlage wurde nicht gefunden.",
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
            )

        await _async_check_read_permission(hass, call, entry_id)
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                "Die ausgewählte PV-Anlage ist nicht geladen.",
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
            )

        coordinator = cast("PvForecastConfigEntry", entry).runtime_data.coordinator
        if coordinator.data is None:
            raise ServiceValidationError(
                "Für die ausgewählte PV-Anlage liegt noch keine Prognose vor.",
                translation_domain=DOMAIN,
                translation_key="forecast_unavailable",
            )

        result = _serialize_forecast(
            coordinator.data,
            str(entry.data[CONF_TIME_ZONE]),
            coordinator.last_update_success_time,
            coordinator.last_update_success,
        )
        if call.data["include_view"]:
            try:
                result["view"] = build_forecast_view(
                    coordinator.data,
                    str(entry.data[CONF_TIME_ZONE]),
                    entry.title,
                    dt_util.utcnow(),
                    coordinator.last_update_success_time,
                    coordinator.last_update_success,
                    day=call.data["day"],
                    roof_id=call.data.get("roof_id"),
                )
            except UnknownRoofError as err:
                raise ServiceValidationError(
                    str(err),
                    translation_domain=DOMAIN,
                    translation_key="roof_not_found",
                ) from err
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FORECAST,
        async_get_forecast,
        schema=_GET_FORECAST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _async_check_read_permission(
    hass: HomeAssistant, call: ServiceCall, entry_id: str
) -> None:
    """Für den vollständigen Anlagenvertrag alle zugehörigen Leserechte prüfen."""

    if call.context.user_id is None:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if user.is_active and user.permissions.access_all_entities(POLICY_READ):
        return

    entries = er.async_entries_for_config_entry(er.async_get(hass), entry_id)
    if not user.is_active or not entries:
        raise Unauthorized(context=call.context, permission=POLICY_READ)
    for entity in entries:
        if not user.permissions.check_entity(entity.entity_id, POLICY_READ):
            raise Unauthorized(
                context=call.context,
                entity_id=entity.entity_id,
                permission=POLICY_READ,
                perm_category=CAT_ENTITIES,
            )


def _serialize_forecast(
    forecast: ForecastResult,
    timezone_name: str,
    fetched_at: datetime | None,
    last_update_success: bool,
) -> ServiceResponse:
    """Den datierten Snapshot einschließlich tatsächlicher Zeitabdeckung abbilden."""

    intervals = forecast.total_intervals
    timezone = ZoneInfo(timezone_name)
    expected_start = datetime.combine(
        forecast.local_date, time.min, timezone
    ).astimezone(UTC)
    expected_end = datetime.combine(
        forecast.local_date + timedelta(days=2), time.min, timezone
    ).astimezone(UTC)
    complete = (
        bool(intervals)
        and intervals[0].start == expected_start
        and intervals[-1].end == expected_end
        and all(
            interval.is_complete and interval.end > interval.start
            for interval in intervals
        )
        and all(
            previous.end == following.start
            for previous, following in pairwise(intervals)
        )
    )

    return {
        "schema_version": 1,
        "timezone": timezone_name,
        "forecast_start_date": forecast.local_date.isoformat(),
        "fetched_at": fetched_at.astimezone(UTC).isoformat() if fetched_at else None,
        "model_issued_at": None,
        "coverage": {
            "start": (
                intervals[0].start.astimezone(UTC).isoformat() if intervals else None
            ),
            "end": intervals[-1].end.astimezone(UTC).isoformat() if intervals else None,
            "complete": complete,
        },
        "last_update_success": last_update_success,
        "intervals": [
            {
                "start": interval.start.astimezone(UTC).isoformat(),
                "end": interval.end.astimezone(UTC).isoformat(),
                "energy_kwh": interval.energy_kwh,
                "ac_power_kw": interval.ac_power_kw,
                "quality_flags": list(interval.quality_flags),
                "is_complete": interval.is_complete,
            }
            for interval in intervals
        ],
    }
