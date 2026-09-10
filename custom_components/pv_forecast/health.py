"""Reine, begrenzte Betriebsprüfung vorhandener lokaler Zustände."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .forecast_intervals import window_energy
from .models import ForecastResult


@dataclass(frozen=True, slots=True)
class HealthFinding:
    """Stabile Ergebniscodes ohne Namen, Exceptiontexte oder dynamische Navigation."""

    code: str
    group: str
    severity: Literal["info", "warning", "error"] = "info"
    value: int = 0


@dataclass(frozen=True, slots=True)
class HealthState:
    """Ein gemeinsam aufgenommener Zustand ohne ausgelöste Nebenwirkungen."""

    loaded: bool
    forecast: ForecastResult | None
    timezone: str
    fetched_at: datetime | None = None
    last_success: bool | None = None
    error: str | None = None
    retry_after: float | None = None
    polling_disabled: bool = False
    sources: tuple[str, ...] = ()
    source_count: int = 0
    measurements_available: bool = False
    measurement_storage_error: bool = False
    archive_enabled: bool = False
    archive_available: bool = False
    archive_loaded: bool = False
    archive_count: int = 0
    archive_storage_error: bool = False
    archive_truncated: bool = False
    calibration_status: str | None = None
    calibration_mode: str = "off"
    cache_status: str | None = None


def forecast_coverage(
    forecast: ForecastResult | None, timezone_name: str, now: datetime
) -> dict:
    """Gespeicherten Horizont und aktuelle Energy-Tage getrennt nach UTC prüfen."""
    if forecast is None:
        return {"available": False}
    timezone = ZoneInfo(timezone_name)
    today = now.astimezone(timezone).date()
    start = datetime.combine(forecast.local_date, time.min, timezone).astimezone(UTC)
    end = datetime.combine(
        forecast.local_date + timedelta(days=forecast.forecast_days), time.min, timezone
    ).astimezone(UTC)
    current_start = datetime.combine(today, time.min, timezone).astimezone(UTC)
    current_end = datetime.combine(
        today + timedelta(days=2), time.min, timezone
    ).astimezone(UTC)
    intervals = forecast.total_intervals
    return {
        "available": True,
        "current_local_days": forecast.local_date == today,
        "interval_count": len(intervals),
        "complete": window_energy(intervals, start, end) is not None,
        "energy_current_complete": window_energy(intervals, current_start, current_end)
        is not None,
        "incomplete_intervals": sum(not i.is_complete for i in intervals),
        "quality_marked_intervals": sum(bool(i.quality_flags) for i in intervals),
    }


def check_health(state: HealthState, now: datetime) -> tuple[HealthFinding, ...]:
    """Technische Fehler von Datenmangel und ausgeschalteten Funktionen trennen."""
    findings = []

    def add(code, group, severity="info", value=0):
        findings.append(HealthFinding(code, group, severity, value))

    if not state.loaded:
        add("not_loaded", "weather", "warning")
    if state.retry_after is not None and state.retry_after > 0:
        add("provider_pause", "weather", "warning", int((state.retry_after + 59) // 60))
    if state.polling_disabled:
        add("polling_disabled", "weather")
    if state.forecast is None:
        add("no_forecast", "weather")
    else:
        if state.last_success is False:
            add(
                (
                    "invalid_response"
                    if state.error == "invalid_api_data"
                    else "fetch_failed"
                ),
                "weather",
                "error",
            )
        elif state.last_success is None:
            add("fetch_unknown", "weather")
        else:
            add("fetch_success", "weather")
        age = (
            now - state.fetched_at
            if state.fetched_at and state.fetched_at.utcoffset() is not None
            else None
        )
        if age is None or age < timedelta(0):
            add("age_unknown", "weather", "warning")
        else:
            add(
                "stale" if age > timedelta(minutes=60) else "age",
                "weather",
                "warning" if age > timedelta(minutes=60) else "info",
                int(age.total_seconds() // 60),
            )
        coverage = forecast_coverage(state.forecast, state.timezone, now)
        add(
            "horizon_complete" if coverage["complete"] else "horizon_incomplete",
            "weather",
            "info" if coverage["complete"] else "warning",
            state.forecast.forecast_days,
        )
        add(
            (
                "energy_complete"
                if coverage["energy_current_complete"]
                else "energy_incomplete"
            ),
            "weather",
            "info" if coverage["energy_current_complete"] else "warning",
        )
        if coverage["quality_marked_intervals"]:
            add(
                "input_fallbacks",
                "weather",
                "warning",
                coverage["quality_marked_intervals"],
            )
    if not state.source_count:
        add("no_sources", "measurements")
    elif not state.measurements_available:
        add("measurements_unknown", "measurements")
    else:
        if state.measurement_storage_error:
            add("measurement_store", "measurements", "error")
        for code in sorted(set(state.sources)):
            add(
                code,
                "measurements",
                (
                    "info"
                    if code in ("source_ok", "source_derived", "source_daily")
                    else "warning"
                ),
                state.sources.count(code),
            )
    if not state.archive_enabled:
        add("archive_off", "archive")
    elif not state.archive_available:
        add("archive_unknown", "archive")
    elif state.archive_storage_error:
        add("archive_store", "archive", "error")
    elif not state.archive_loaded:
        add("archive_not_loaded", "archive", "warning")
    elif not state.archive_count:
        add("archive_empty", "archive")
    else:
        add("archive_ready", "archive", "info", state.archive_count)
    if state.archive_truncated:
        add("archive_truncated", "archive", "warning")
    if state.calibration_mode == "off":
        add("learning_off", "calibration")
    elif state.calibration_status is None:
        add("learning_unknown", "calibration")
    elif state.calibration_status == "storage_unavailable":
        add("learning_store", "calibration", "error")
    elif state.calibration_status == "prerequisites_missing":
        add("learning_prerequisites", "calibration")
    elif state.calibration_status in (
        "learning",
        "testing",
        "approved",
        "rejected",
        "invalidated",
        "underperformance_paused",
    ):
        add("learning_" + state.calibration_status, "calibration")
    else:
        add("learning_unknown", "calibration")
    if state.cache_status in (
        "disabled",
        "empty",
        "available",
        "unsupported_version",
        "storage_limit",
        "storage_unavailable",
        "invalid_snapshot",
    ):
        add(
            "cache_" + state.cache_status,
            "cache",
            (
                "error"
                if state.cache_status in ("unsupported_version", "storage_unavailable")
                else (
                    "warning"
                    if state.cache_status in ("storage_limit", "invalid_snapshot")
                    else "info"
                )
            ),
        )
    else:
        add("cache_unknown", "cache")
    return tuple(findings)
