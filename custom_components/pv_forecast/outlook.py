"""Tagesaussicht mit getrennt belegter Messung, geschätzter Brücke und Zukunft."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
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
    """Strengen Messnachweis um eine eigenständig gekennzeichnete Schätzung ergänzen."""
    result = _build_exact_outlook(
        forecast,
        histories,
        timezone_name,
        now,
        fetched_at,
        last_update_success,
        identity_unresolved=identity_unresolved,
    )
    zone = ZoneInfo(timezone_name)
    now = now.astimezone(UTC)
    today = now.astimezone(zone).date()
    start = datetime.combine(today, time.min, zone).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(UTC)
    age = (
        now - fetched_at.astimezone(UTC)
        if fetched_at and fetched_at.utcoffset() is not None
        else None
    )
    estimate: dict[str, Any] = {
        "schema_version": 1,
        "status": "unavailable",
        "reason": "incomplete_forecast",
        "basis": "forecast_only",
        "measured_kwh": None,
        "estimated_past_kwh": None,
        "remaining_kwh": result["remaining_kwh"],
        "total_kwh": None,
        "measurement_coverage_seconds": 0,
        "forecast_stale": not last_update_success
        or age is None
        or not timedelta(0) <= age <= timedelta(minutes=60),
        "forecast_quality_flags": [],
    }
    result["estimate"] = estimate
    try:
        windows = (
            []
            if identity_unresolved or now == start
            else _measured_windows(histories, start, now)
        )
        measured = fsum(value for _, _, value in windows)
        # Nur das Komplement der Messabschnitte schätzen. Die gemeinsame
        # Prognose wird weder skaliert noch um ganze Teilmessmengen erhöht.
        cursor = start
        missing = []
        for left, right, _ in windows:
            if cursor < left:
                missing.append((cursor, left))
            cursor = right
        if cursor < now:
            missing.append((cursor, now))
        past = [
            _window_energy(forecast.total_intervals, left, right)
            for left, right in missing
        ]
        estimate["forecast_quality_flags"] = sorted(
            {
                flag
                for left, right in [*missing, (now, end)]
                for interval in _project_intervals(
                    forecast.total_intervals, left, right
                )
                for flag in interval.quality_flags
            }
        )
        estimate["measurement_coverage_seconds"] = fsum(
            (right - left).total_seconds() for left, right, _ in windows
        )
        estimate["measured_kwh"] = measured if windows else None
        estimate["basis"] = "measurements_and_forecast" if windows else "forecast_only"
        if any(value is None for value in past) or estimate["remaining_kwh"] is None:
            return result
        estimated_past = fsum(value for value in past if value is not None)
        total = fsum((measured, estimated_past, estimate["remaining_kwh"]))
        if not all(isfinite(value) for value in (measured, estimated_past, total)):
            raise OverflowError
    except OverflowError:
        estimate.update(reason="arithmetic_overflow", measured_kwh=None)
        return result
    estimate.update(
        status="available",
        reason=None,
        estimated_past_kwh=estimated_past,
        total_kwh=total,
    )
    return result


def _measured_windows(
    histories: Sequence[SourceHistory], start: datetime, now: datetime
) -> list[tuple[datetime, datetime, float]]:
    """Ganze aktuelle Zählerdifferenzen an gemeinsamen exakten Grenzen verbinden."""
    sources = [
        history.current_location_view()
        for history in histories
        if _has_energy_in_window(history, start, now)
    ]
    if not sources:
        return []
    # Die zentrale Auswahl entfernt Korrekturen, ungültige Integral-Lücken und
    # angeschnittene positive Differenzen; identische Quellen bleiben verboten.
    snapshots = {
        history.source.source_id: history.snapshot(start, now, now)
        for history in sources
    }
    aggregate_energy(sources, start, now, now, cached_snapshots=snapshots)
    source_bounds = []
    source_values = []
    common = None
    for history in sources:
        # Technische Zählerneustarts bewahren die Quellenidentität. Frühere
        # Messgrenzen bleiben auch am selben Standort ausgeschlossen.
        segments = {
            segment_id
            for segment_id, source in history.segment_sources.items()
            if source.measurement_identity == history.source.measurement_identity
        }
        bounds: dict[datetime, tuple[int, int]] = {}
        values = []
        previous_end = None
        run = 0
        for delta in snapshots[history.source.source_id]["deltas"]:
            if delta["segment_id"] not in segments:
                continue
            left, right = datetime.fromisoformat(
                delta["start"]
            ), datetime.fromisoformat(delta["end"])
            if left != previous_end:
                run += 1
            bounds[left] = (run, len(values))
            values.append(delta["energy_kwh"])
            bounds[right] = (run, len(values))
            previous_end = right
        source_bounds.append(bounds)
        source_values.append(values)
        common = set(bounds) if common is None else common & bounds.keys()
    boundaries = sorted(common or ())
    windows = []
    window_values: list[float] = []
    window_start = window_end = start
    for left, right in pairwise(boundaries):
        if any(bounds[left][0] != bounds[right][0] for bounds in source_bounds):
            continue
        energy = fsum(
            value
            for bounds, values in zip(source_bounds, source_values, strict=True)
            for value in values[bounds[left][1] : bounds[right][1]]
        )
        if left != window_end:
            if window_values:
                windows.append((window_start, window_end, fsum(window_values)))
            window_start, window_values = left, []
        window_end = right
        window_values.append(energy)
    if window_values:
        windows.append((window_start, window_end, fsum(window_values)))
    return windows


def _build_exact_outlook(
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
        "measurement_age_minutes": None,
        "measurement_stale": False,
        "measured_kwh": None,
        "bridge_kwh": None,
        "remaining_kwh": _window_energy(intervals, now, end),
        "total_kwh": None,
        "quality_flags": [],
        "measurement_quality_flags": [],
        "forecast_quality_flags": [],
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
        result["measurement_quality_flags"] = total["quality_flags"]
        # snapshot() prüft die letzte gültige Meldung jeder Quelle gegen deren
        # bestätigte Meldefrist. Eine ältere gemeinsame Grenze allein bedeutet
        # bei weiterhin versetzt meldenden Quellen noch keinen stummen Zähler.
        result["measurement_stale"] = "stale" in total["quality_flags"]
        if not total["energy_complete"]:
            return unavailable("incomplete_measurements")
        measured = total["energy_kwh"]
    bridge = _window_energy(intervals, measured_until, now)
    result.update(
        measured_until=measured_until.isoformat(),
        measurement_age_minutes=(now - measured_until).total_seconds() / 60,
        measured_kwh=measured,
        bridge_kwh=bridge,
    )
    flags = {
        flag
        for interval in _project_intervals(intervals, measured_until, end)
        for flag in interval.quality_flags
    }
    result["forecast_quality_flags"] = sorted(flags)
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
