"""Lesender, berechtigungsgeprüfter Zugriff auf lokale PV-Messungen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Protocol, cast

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
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .services import CONF_CONFIG_ENTRY_ID, _async_check_read_permission

if TYPE_CHECKING:
    from . import PvForecastConfigEntry

SERVICE_GET_MEASUREMENTS = "get_measurements"


class SourceIdentityView(Protocol):
    """Gemeinsame Leserechtsgrenze aktueller und archivierter Messquellen."""

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Alle betroffenen aktuellen und früheren Quell-Entities."""

    @property
    def identity_unresolved(self) -> tuple[str, ...]:
        """Nicht mehr eindeutig auflösbare Quellenidentitäten."""


def _aware_datetime(value: object) -> datetime:
    """Absolute Fenstergrenzen verlangen und eindeutig nach UTC normalisieren."""

    parsed = cv.datetime(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise vol.Invalid("Der Zeitpunkt benötigt einen UTC-Offset oder Z.")
    return parsed.astimezone(UTC)


def _interval_windows(value: list[dict[str, datetime]]) -> list[dict[str, datetime]]:
    """Positive, getrennte Fenster auf höchstens zwei absolute Tage begrenzen."""

    ordered = sorted(value, key=lambda window: window["start"])
    if any(window["end"] <= window["start"] for window in ordered):
        raise vol.Invalid("Jedes Messintervall benötigt eine positive Dauer.")
    if any(
        previous["end"] > following["start"]
        for previous, following in pairwise(ordered)
    ):
        raise vol.Invalid("Messintervalle dürfen sich nicht überschneiden.")
    if ordered and ordered[-1]["end"] - ordered[0]["start"] > timedelta(hours=48):
        raise vol.Invalid(
            "Messintervalle dürfen zusammen höchstens 48 Stunden umfassen."
        )
    return value


_MEASUREMENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("start"): _aware_datetime,
        vol.Required("end"): _aware_datetime,
        vol.Optional("interval_windows"): vol.All(
            list,
            vol.Length(max=50),
            [
                {
                    vol.Required("start"): _aware_datetime,
                    vol.Required("end"): _aware_datetime,
                }
            ],
            _interval_windows,
        ),
    }
)


@callback
def async_setup_measurement_services(hass: HomeAssistant) -> None:
    """Die Messdatenaktion unabhängig vom Ladezustand registrieren."""

    async def async_get_measurements(call: ServiceCall) -> ServiceResponse:
        """Ein explizites UTC-Fenster ohne Aktualisierung oder Netzwerk lesen."""

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
        manager = cast("PvForecastConfigEntry", entry).runtime_data.measurements
        if manager is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="measurements_unavailable"
            )
        await _async_check_source_permissions(hass, call, manager)
        start = call.data["start"]
        end = call.data["end"]
        if end <= start:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_measurement_window"
            )
        now = dt_util.utcnow()
        if "interval_windows" not in call.data:
            return manager.snapshot(start, end, now)
        intervals = await manager.async_interval_windows(
            [
                (window["start"], window["end"])
                for window in call.data["interval_windows"]
            ],
            now,
        )
        # Zwischen Quellen darf HA andere Aufgaben ausführen; vor der Antwort
        # gelten deshalb erneut die dann aktuellen externen Leserechte.
        await _async_check_source_permissions(hass, call, manager)
        if (
            entry.state is not ConfigEntryState.LOADED
            or getattr(getattr(entry, "runtime_data", None), "measurements", None)
            is not manager
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        result = manager.snapshot(start, end, now)
        result["total_intervals"] = intervals
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_MEASUREMENTS,
        async_get_measurements,
        schema=_MEASUREMENTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _async_check_source_permissions(
    hass: HomeAssistant, call: ServiceCall, manager: SourceIdentityView
) -> None:
    """Auch die ausgewählten fremden Sensoren und historische Identitäten schützen."""

    if call.context.user_id is None:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if not user.is_active:
        raise Unauthorized(context=call.context, permission=POLICY_READ)
    if user.is_admin:
        return
    if manager.identity_unresolved:
        raise Unauthorized(context=call.context, permission=POLICY_READ)
    for entity_id in manager.entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_READ):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_READ,
                perm_category=CAT_ENTITIES,
            )
