"""Nebenwirkungsfreie Aufnahme bereits vorhandener HA-Betriebszustände."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .api import OpenMeteoRequestState
from .const import CONF_TIME_ZONE, DOMAIN
from .diagnostics import _error_class
from .health import HealthState
from .measurements import SourceConfig


def capture_health(
    hass: HomeAssistant, entry: ConfigEntry, now: datetime
) -> HealthState:
    """Keine Clients erzeugen, Stores lesen oder Archivbewertungen anstoßen."""
    runtime = (
        getattr(entry, "runtime_data", None)
        if entry.state is ConfigEntryState.LOADED
        else None
    )
    coordinator = getattr(runtime, "coordinator", None)
    manager = getattr(runtime, "measurements", None)
    history = getattr(runtime, "history", None)
    calibration = getattr(runtime, "calibration", None)
    cache = getattr(runtime, "forecast_cache", None)
    request_state = hass.data.get(DOMAIN)
    registry = er.async_get(hass)
    sources = []
    configured = entry.options.get("measurement_sources", [])
    if manager is not None:
        for raw in configured:
            try:
                source = SourceConfig.from_dict(raw)
            except (ValueError, TypeError, KeyError):
                sources.append("source_identity")
                continue
            registered = (
                registry.async_get(source.registry_id) if source.registry_id else None
            )
            if source.registry_id and registered is None:
                sources.append("source_removed")
            elif registered is not None and registered.disabled_by is not None:
                sources.append("source_disabled")
            elif not source.registry_id:
                sources.append("source_identity")
            elif not manager._identity_matches(
                replace(source, entity_id=registered.entity_id)
            ):
                sources.append("source_identity")
            else:
                state = hass.states.get(registered.entity_id)
                if state is None or state.state in ("unavailable", "unknown"):
                    sources.append("source_unavailable")
                else:
                    captured = manager._histories.get(source.source_id)
                    last = captured.latest_reading if captured else None
                    if (
                        last is None
                        or last.value is None
                        or last.timestamp > now
                        or captured._pending_gap
                        or not manager._upstream_valid(source, now)
                        or now - last.timestamp
                        > timedelta(minutes=source.max_interval_minutes)
                    ):
                        sources.append("source_gap")
                    else:
                        sources.append("source_ok")
            if source.derived_energy:
                sources.append("source_derived")
            if source.kind == "daily":
                sources.append("source_daily")
    return HealthState(
        loaded=entry.state is ConfigEntryState.LOADED,
        forecast=getattr(coordinator, "data", None),
        timezone=str(entry.data[CONF_TIME_ZONE]),
        fetched_at=getattr(coordinator, "last_update_success_time", None),
        last_success=getattr(coordinator, "last_update_success", None),
        error=_error_class(getattr(coordinator, "last_exception", None)),
        retry_after=(
            request_state.retry_after
            if isinstance(request_state, OpenMeteoRequestState)
            else None
        ),
        polling_disabled=entry.pref_disable_polling,
        sources=tuple(sources),
        source_count=len(configured),
        measurements_available=manager is not None,
        measurement_storage_error=bool(getattr(manager, "_storage_error", None)),
        archive_enabled=entry.options.get("history_enabled") is True,
        archive_available=history is not None,
        archive_loaded=bool(getattr(history, "loaded", False)),
        archive_count=len(history._archive.records) if history else 0,
        archive_storage_error=bool(getattr(history, "_storage_error", None)),
        archive_truncated=bool(history and history._archive.retention_truncated),
        calibration_status=(
            calibration.snapshot().get("status") if calibration else None
        ),
        calibration_mode=entry.options.get("calibration_mode", "off"),
        cache_status=(
            cache.status
            if cache
            else "disabled" if not entry.options.get("forecast_cache_enabled") else None
        ),
    )
