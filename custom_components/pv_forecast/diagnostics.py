"""Bewusst begrenzte Diagnosedaten ohne Standort, Identitäten oder Ertragsreihen."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    OpenMeteoConnectionError,
    OpenMeteoDataError,
    OpenMeteoRateLimitError,
    OpenMeteoRequestState,
    OpenMeteoRetryPendingError,
    OpenMeteoTemporaryError,
)
from .calculations import InvalidConfigurationError, to_open_meteo_azimuth
from .calibration import RULE_VERSION
from .calibration_runtime import STORAGE_VERSION as CALIBRATION_STORAGE_VERSION
from .configuration import roofs_from_options
from .const import CONF_INVERTER_MAX_POWER_KW, CONF_TIME_ZONE, DOMAIN, UPDATE_INTERVAL
from .health import forecast_coverage
from .history import MODEL_VERSION
from .history_runtime import STORAGE_VERSION as HISTORY_STORAGE_VERSION
from .measurement_runtime import STORAGE_VERSION as MEASUREMENT_STORAGE_VERSION
from .models import ForecastResult
from .morning_runtime import STORAGE_VERSION as MORNING_STORAGE_VERSION

if TYPE_CHECKING:
    from . import PvForecastConfigEntry
    from .calibration_runtime import CalibrationManager

_STORAGE_ERRORS = frozenset(
    {"storage_unavailable", "unsupported_version", "storage_limit"}
)
_CALIBRATION_MODES = frozenset({"off", "observe", "auto"})
_CALIBRATION_STATUSES = frozenset(
    {
        "off",
        "learning",
        "testing",
        "approved",
        "rejected",
        "invalidated",
        "storage_unavailable",
        "prerequisites_missing",
        "underperformance_paused",
    }
)
_FORECAST_CACHE_STATUSES = frozenset(
    {
        "disabled",
        "empty",
        "available",
        "unsupported_version",
        "storage_limit",
        "storage_unavailable",
        "invalid_snapshot",
    }
)


def _allowed(value: Any, allowed: frozenset[str]) -> str | None:
    """Auch unerwartete gespeicherte Freitexte niemals als Status exportieren."""

    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "unknown"


def _error_class(error: BaseException | None) -> str | None:
    """Nur feste Fehlerklassen lesen, niemals Meldung, URL oder Traceback."""

    current = error
    for _ in range(8):
        if current is None:
            break
        for kind, label in (
            (OpenMeteoRateLimitError, "rate_limit"),
            (OpenMeteoRetryPendingError, "retry_pending"),
            (OpenMeteoTemporaryError, "temporary_api_error"),
            (OpenMeteoDataError, "invalid_api_data"),
            (OpenMeteoConnectionError, "connection_error"),
            (InvalidConfigurationError, "invalid_configuration"),
            (TimeoutError, "timeout"),
        ):
            if isinstance(current, kind):
                return label
        current = current.__cause__
    if error is None:
        return None
    return "update_failed" if isinstance(error, UpdateFailed) else "unexpected_error"


def _coverage(
    forecast: ForecastResult | None, timezone_name: str, now: datetime
) -> dict[str, Any]:
    """Gemeinsame Abdeckungsprüfung ohne Ertragslisten verwenden."""
    return forecast_coverage(forecast, timezone_name, now)


def _calibration_status(manager: CalibrationManager | None) -> dict[str, Any]:
    """Nur den vorhandenen Lernstatus lesen, ohne Bewertung oder Historienexport."""

    if manager is None:
        return {"available": False}
    status = manager.snapshot()
    return {
        "available": True,
        "mode": _allowed(status.get("mode"), _CALIBRATION_MODES),
        "status": _allowed(status.get("status"), _CALIBRATION_STATUSES),
        "storage_error": _allowed(status.get("storage_error"), _STORAGE_ERRORS),
        "prerequisites_met": manager.prerequisites_met,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PvForecastConfigEntry
) -> dict[str, Any]:
    """Eine feste Positivliste lokaler Metadaten für den bewussten Download liefern.

    Konfiguration, Runtime-Objekte und Fehlermeldungen werden nie serialisiert.
    Neue Felder dieser Objekte können dadurch nicht ungeprüft im Export landen.
    """

    runtime = getattr(entry, "runtime_data", None)
    result: dict[str, Any] = {
        "schema_version": 1,
        "configuration_version": {
            "major": entry.version,
            "minor": entry.minor_version,
        },
        "model_version": MODEL_VERSION,
        "supported_storage_versions": {
            "measurements": MEASUREMENT_STORAGE_VERSION,
            "history": HISTORY_STORAGE_VERSION,
            "calibration": CALIBRATION_STORAGE_VERSION,
            "morning": MORNING_STORAGE_VERSION,
        },
        "calibration_rule_version": RULE_VERSION,
        "entry_state": entry.state.value,
        "runtime_available": runtime is not None,
    }
    try:
        roofs = roofs_from_options(entry.options)
    except (InvalidConfigurationError, KeyError, TypeError, ValueError, OverflowError):
        result["configuration"] = {"valid_roofs": False}
    else:
        result["configuration"] = {
            "valid_roofs": True,
            "roof_count": len(roofs),
            "geometry_count": len(
                {
                    (roof.tilt_deg, to_open_meteo_azimuth(roof.compass_azimuth_deg))
                    for roof in roofs
                }
            ),
            "inverter_limit_configured": (
                entry.options.get(CONF_INVERTER_MAX_POWER_KW) is not None
            ),
        }
    if runtime is None:
        return result

    now = dt_util.utcnow()
    coordinator = runtime.coordinator
    fetched_at = coordinator.last_update_success_time
    request_state = hass.data.get(DOMAIN)
    retry_after = (
        request_state.retry_after
        if isinstance(request_state, OpenMeteoRequestState)
        else None
    )
    result["forecast"] = {
        "last_update_success": coordinator.last_update_success,
        "last_success_age_seconds": (
            max(0, int((now - fetched_at).total_seconds())) if fetched_at else None
        ),
        "update_interval_seconds": int(UPDATE_INTERVAL.total_seconds()),
        "error_class": _error_class(coordinator.last_exception),
        "retry_after_seconds": (
            max(1, int(retry_after)) if retry_after is not None else None
        ),
        "coverage": _coverage(coordinator.data, str(entry.data[CONF_TIME_ZONE]), now),
    }
    measurements = runtime.measurements
    result["measurements"] = (
        {
            "available": True,
            "running": measurements.running,
            "source_count": len(measurements._histories),
            "unresolved_identity_count": len(measurements.identity_unresolved),
            "storage_error": _allowed(measurements.storage_error, _STORAGE_ERRORS),
        }
        if measurements is not None
        else {"available": False}
    )
    history = runtime.history
    result["history"] = (
        {
            "available": True,
            "enabled": history.enabled,
            "loaded": history.loaded,
            "running": history.running,
            "storage_error": _allowed(history.storage_error, _STORAGE_ERRORS),
        }
        if history is not None
        else {"available": False}
    )
    cache = getattr(runtime, "forecast_cache", None)
    result["forecast_cache"] = {
        "status": _allowed(
            cache.status if cache else None,
            _FORECAST_CACHE_STATUSES,
        )
    }
    result["calibration"] = _calibration_status(runtime.calibration)
    return result
