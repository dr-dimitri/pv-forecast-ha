"""Zusammenhängende Solarzeitfenster aus vorhandenen UTC-Intervallenergien."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from math import isclose
from typing import Any
from zoneinfo import ZoneInfo

from .card_data import _project_intervals, _window_energy
from .models import ForecastResult


def _utc(value: datetime) -> datetime:
    """Mehrdeutige lokale Zeitangaben nicht still als UTC interpretieren."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Die Planung benötigt Zeitpunkte mit UTC-Offset")
    return value.astimezone(UTC)


def plan_solar_window(
    forecast: ForecastResult,
    timezone_name: str,
    now: datetime,
    fetched_at: datetime | None,
    last_update_success: bool,
    *,
    duration_minutes: int,
    earliest_start: datetime,
    latest_end: datetime,
    previous_start: datetime | None = None,
) -> dict[str, Any]:
    """Das energiereichste zulässige Fenster ohne Wetterabruf bestimmen.

    Die Energie ist innerhalb jedes gelieferten Intervalls linear in der Zeit.
    Maxima liegen deshalb an Grenzen oder um die Laufdauer versetzten Grenzen.
    Eine Minutenraster-Suche würde Teilstunden unnötig runden und ist nicht nötig.
    """
    now, earliest_start, latest_end = map(_utc, (now, earliest_start, latest_end))
    if (
        isinstance(duration_minutes, bool)
        or not isinstance(duration_minutes, int)
        or not 1 <= duration_minutes <= 2880
        or earliest_start >= latest_end
    ):
        raise ValueError("Laufdauer oder zulässiger Zeitraum ist ungültig")
    previous_start = _utc(previous_start) if previous_start is not None else None
    duration = timedelta(minutes=duration_minutes)
    age = now - _utc(fetched_at) if fetched_at is not None else None
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "unavailable",
        "reason": None,
        "as_of": now.isoformat(),
        "fetched_at": _utc(fetched_at).isoformat() if fetched_at else None,
        "timezone": timezone_name,
        "duration_minutes": duration_minutes,
        "start": None,
        "end": None,
        "energy_kwh": None,
        "quality_flags": [],
        "basis": "effective_forecast",
        "assumption": "constant_interval_mean_power",
        "hysteresis_applied": False,
        "uncertainty": {"status": "unavailable", "reason": "unsupported_horizon"},
    }

    def unavailable(reason: str) -> dict[str, Any]:
        return {**result, "reason": reason}

    timezone = ZoneInfo(timezone_name)
    today = now.astimezone(timezone).date()
    day_start = datetime.combine(today, time.min, timezone).astimezone(UTC)
    forecast_end = datetime.combine(
        today + timedelta(days=2), time.min, timezone
    ).astimezone(UTC)
    if previous_start is not None and previous_start <= now:
        return {
            **result,
            "status": ("started" if now < previous_start + duration else "completed"),
            "start": previous_start.isoformat(),
            "end": (previous_start + duration).isoformat(),
            "reason": "previous_window_started",
        }
    if earliest_start < day_start or latest_end > forecast_end:
        return unavailable("outside_forecast")
    left = max(now, earliest_start)
    right = latest_end - duration
    if left > right:
        return unavailable("infeasible_window")
    if (
        not last_update_success
        or age is None
        or not timedelta(0) <= age <= timedelta(minutes=60)
    ):
        return unavailable("stale_forecast")

    intervals = forecast.total_intervals
    if _window_energy(intervals, left, latest_end) is None:
        return unavailable("incomplete_forecast")
    projected = _project_intervals(intervals, left, latest_end)
    result["quality_flags"] = sorted(
        {flag for interval in projected for flag in interval.quality_flags}
    )
    if result["quality_flags"]:
        return unavailable("input_fallbacks")

    candidates = {left, right}
    for interval in projected:
        for boundary in (interval.start, interval.end):
            for candidate in (boundary, boundary - duration):
                if left <= candidate <= right:
                    candidates.add(candidate)
    best_start, best_energy = left, -1.0
    for candidate in sorted(candidates):
        energy = _window_energy(intervals, candidate, candidate + duration)
        if energy is None:
            return unavailable("incomplete_forecast")
        if energy > best_energy and not isclose(
            energy, best_energy, rel_tol=1e-12, abs_tol=1e-9
        ):
            best_start, best_energy = candidate, energy
    if best_energy <= 0:
        return unavailable("no_solar_energy")

    if previous_start is not None and left <= previous_start <= right:
        previous_energy = _window_energy(
            intervals, previous_start, previous_start + duration
        )
        if previous_energy is not None and best_energy - previous_energy <= max(
            0.1, previous_energy * 0.05
        ):
            result["hysteresis_applied"] = best_start != previous_start
            best_start, best_energy = previous_start, previous_energy
    if best_energy <= 0:
        return unavailable("no_solar_energy")
    return {
        **result,
        "status": "available",
        "start": best_start.isoformat(),
        "end": (best_start + duration).isoformat(),
        "energy_kwh": best_energy,
    }
