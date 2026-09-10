"""Tagesaussicht mit getrennt belegter Messung, geschätzter Brücke und Zukunft."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from math import fsum, isfinite
from typing import Any
from zoneinfo import ZoneInfo

from .forecast_intervals import (
    project_intervals as _project_intervals,
)
from .forecast_intervals import (
    window_energy as _window_energy,
)
from .measurements import SourceHistory, _has_energy_in_window, aggregate_energy
from .models import ForecastResult


def build_day_outlook(
    forecast: ForecastResult,
    histories: Sequence[SourceHistory],
    timezone_name: str,
    now: datetime,
    fetched_at: datetime | None,
    last_update_success: bool,
    *,
    identity_unresolved: bool = False,
) -> dict[str, Any]:
    """Nur ein gemeinsames exaktes Messpräfix mit anschließender Prognose addieren."""
    if now.utcoffset() is None:
        raise ValueError("Die Tagesaussicht benötigt einen eindeutigen Zeitpunkt")
    now = now.astimezone(UTC)
    zone = ZoneInfo(timezone_name)
    today = now.astimezone(zone).date()
    start = datetime.combine(today, time.min, zone).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(UTC)
    intervals = forecast.total_intervals
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "unavailable",
        "reason": None,
        "as_of": now.isoformat(),
        "timezone": timezone_name,
        "measured_until": None,
        "measured_kwh": None,
        "bridge_kwh": None,
        "remaining_kwh": _window_energy(intervals, now, end),
        "total_kwh": None,
        "quality_flags": [],
        "correction": "off",
    }

    def unavailable(reason: str) -> dict[str, Any]:
        return {**result, "reason": reason}

    if identity_unresolved:
        return unavailable("unresolved_measurement_identity")
    sources = [h for h in histories if _has_energy_in_window(h, start, now)]
    if not sources:
        return unavailable("no_energy_sources")
    if now == start:
        measured_until, measured = now, 0.0
    else:
        # Unterschiedliche Meldezeiten werden nicht durch Teilung von Zähler-
        # differenzen passend gemacht. Nur gemeinsame exakte Grenzen zählen.
        boundaries: set[datetime] | None = None
        for history in sources:
            ends = {delta.end for delta in history.deltas if start < delta.end <= now}
            boundaries = ends if boundaries is None else boundaries & ends
        if not boundaries:
            return unavailable("no_common_measurement_boundary")
        measured_until = max(boundaries)
        total = aggregate_energy(sources, start, measured_until, now)
        result["quality_flags"] = total["quality_flags"]
        if not total["energy_complete"]:
            return unavailable("incomplete_measurements")
        measured = total["energy_kwh"]
    bridge = _window_energy(intervals, measured_until, now)
    result.update(
        measured_until=measured_until.isoformat(),
        measured_kwh=measured,
        bridge_kwh=bridge,
    )
    age = (
        now - fetched_at.astimezone(UTC)
        if fetched_at is not None and fetched_at.utcoffset() is not None
        else None
    )
    if (
        not last_update_success
        or age is None
        or not timedelta(0) <= age <= timedelta(minutes=60)
    ):
        return unavailable("stale_forecast")
    if bridge is None or result["remaining_kwh"] is None:
        return unavailable("incomplete_forecast")
    flags = {
        flag
        for interval in _project_intervals(intervals, measured_until, end)
        for flag in interval.quality_flags
    }
    result["quality_flags"] = sorted(set(result["quality_flags"]) | flags)
    if flags:
        return unavailable("input_fallbacks")
    try:
        total_kwh = fsum((measured, bridge, result["remaining_kwh"]))
    except OverflowError:
        return unavailable("arithmetic_overflow")
    if not isfinite(total_kwh):
        return unavailable("arithmetic_overflow")
    return {**result, "status": "available", "total_kwh": total_kwh}
