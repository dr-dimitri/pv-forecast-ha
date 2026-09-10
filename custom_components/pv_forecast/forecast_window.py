"""Energie und mittlere Leistung für explizite Gesamtanlagenfenster."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from math import fsum, isclose
from typing import Any
from zoneinfo import ZoneInfo

from .forecast_intervals import _utc, _valid_number, project_intervals, window_energy
from .models import ForecastResult


def query_forecast_window(
    forecast: ForecastResult,
    timezone_name: str,
    now: datetime,
    fetched_at: datetime | None,
    last_update_success: bool,
    *,
    start: datetime,
    end: datetime,
    step_minutes: int | None = None,
) -> dict[str, Any]:
    """Halb offene UTC-Fenster ohne Abruf, Runden oder erneutes Clipping lesen."""
    now, start, end = map(_utc, (now, start, end))
    duration = end - start
    if duration <= timedelta(0):
        raise ValueError("Das Fenster benötigt ein späteres Ende")
    step = None
    if step_minutes is not None:
        if type(step_minutes) is not int or step_minutes not in (5, 15, 30, 60):
            raise ValueError("Das Raster benötigt 5, 15, 30 oder 60 ganze Minuten")
        step = timedelta(minutes=step_minutes)
        if duration % step or duration // step > 336:
            raise ValueError(
                "Das Raster muss exakt teilbar sein und hat maximal 336 Schritte"
            )

    timezone = ZoneInfo(timezone_name)
    current_start = _utc(
        datetime.combine(now.astimezone(timezone).date(), time.min, timezone)
    )
    current_end = _utc(
        datetime.combine(
            now.astimezone(timezone).date() + timedelta(days=forecast.forecast_days),
            time.min,
            timezone,
        )
    )
    snapshot_start = _utc(datetime.combine(forecast.local_date, time.min, timezone))
    snapshot_end = _utc(
        datetime.combine(
            forecast.local_date + timedelta(days=forecast.forecast_days),
            time.min,
            timezone,
        )
    )
    fetched = _utc(fetched_at) if fetched_at is not None else None
    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": "total",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": now.isoformat(),
        "fetched_at": fetched.isoformat() if fetched else None,
        "timezone": timezone_name,
        "status": "unavailable",
        "reason": None,
        "coverage": {"start": None, "end": None, "complete": False},
        "quality_flags": [],
        "energy_kwh": None,
        "mean_ac_power_kw": None,
        "assumption": "constant_interval_mean_power",
        "uncertainty": {"status": "unavailable", "reason": "unsupported_horizon"},
    }
    if step is not None:
        result.update(step_minutes=step_minutes, intervals=[])

    def unavailable(reason: str) -> dict[str, Any]:
        return {**result, "reason": reason}

    if start < max(current_start, snapshot_start) or end > min(
        current_end, snapshot_end
    ):
        return unavailable("outside_forecast")

    # Ungültige oder doppelte Intervalle nicht durch Projektion verschwinden lassen.
    relevant = []
    for interval in forecast.total_intervals:
        try:
            left, right = _utc(interval.start), _utc(interval.end)
        except ValueError:
            return unavailable("incomplete_forecast")
        if right <= left:
            if start <= left < end or start < right <= end:
                return unavailable("incomplete_forecast")
            continue
        if left >= end or right <= start:
            continue
        if not _valid_number(interval.energy_kwh) or not _valid_number(
            interval.ac_power_kw
        ):
            return unavailable("incomplete_forecast")
        relevant.append(interval)
    projected = project_intervals(relevant, start, end)
    energy = window_energy(relevant, start, end)
    result["coverage"] = {
        "start": projected[0].start.isoformat() if projected else None,
        "end": max(item.end for item in projected).isoformat() if projected else None,
        "complete": energy is not None,
    }
    result["quality_flags"] = sorted(
        {flag for item in projected for flag in item.quality_flags}
    )
    if (
        not last_update_success
        or fetched is None
        or not timedelta(0) <= now - fetched <= timedelta(minutes=60)
    ):
        return unavailable("stale_forecast")
    if energy is None:
        return unavailable("incomplete_forecast")
    if result["quality_flags"]:
        return unavailable("input_fallbacks")
    mean_power = energy / (duration.total_seconds() / 3600)
    if not _valid_number(mean_power):
        return unavailable("incomplete_forecast")
    if step is not None:
        intervals = []
        for index in range(duration // step):
            left = start + index * step
            right = left + step
            value = window_energy(relevant, left, right)
            power = value / (step.total_seconds() / 3600) if value is not None else None
            if not _valid_number(value) or not _valid_number(power):
                return unavailable("incomplete_forecast")
            intervals.append(
                {
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    "energy_kwh": value,
                    "mean_ac_power_kw": power,
                }
            )
        try:
            conserved = isclose(
                fsum(item["energy_kwh"] for item in intervals),
                energy,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
        except OverflowError:
            conserved = False
        if not conserved:
            return unavailable("incomplete_forecast")
        result["intervals"] = intervals
    return {
        **result,
        "status": "available",
        "energy_kwh": energy,
        "mean_ac_power_kw": mean_power,
    }
