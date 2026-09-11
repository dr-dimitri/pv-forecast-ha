"""Reine Kartenansicht vorhandener Prognoseintervalle ohne eigenes PV-Modell."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .forecast_intervals import (
    project_intervals as _project_intervals,
)
from .forecast_intervals import (
    window_energy as _window_energy,
)
from .models import ForecastDay, ForecastResult, TotalForecastInterval
from .shading import shading_metadata


class UnknownRoofError(ValueError):
    """Die angefragte Dach-ID gehört nicht zum gespeicherten Forecast."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Die Kartenansicht benötigt eindeutige Zeitpunkte")
    return value.astimezone(UTC)


def _bounds(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, timezone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, timezone).astimezone(UTC),
    )


def _roof_intervals(
    forecast: ForecastResult, roof_id: str
) -> tuple[TotalForecastInterval, ...]:
    """Geclippte Dachbeiträge mit der gemeinsamen Abdeckungsqualität abbilden."""
    if roof_id not in forecast.roofs:
        raise UnknownRoofError("Die ausgewählte Dachfläche wurde nicht gefunden.")
    projected = []
    for contribution in forecast.roofs[roof_id].intervals:
        source = TotalForecastInterval(
            _utc(contribution.start),
            _utc(contribution.end),
            contribution.energy_kwh,
            contribution.ac_power_kw,
        )
        for total in forecast.total_intervals:
            for interval in _project_intervals(
                (source,), _utc(total.start), _utc(total.end)
            ):
                projected.append(
                    TotalForecastInterval(
                        interval.start,
                        interval.end,
                        interval.energy_kwh,
                        interval.ac_power_kw,
                        total.quality_flags,
                        total.is_complete,
                    )
                )
    return tuple(sorted(projected, key=lambda interval: interval.start))


def build_forecast_view(
    forecast: ForecastResult,
    timezone_name: str,
    plant_name: str,
    now: datetime,
    fetched_at: datetime | None,
    last_update_success: bool,
    *,
    origin: str = "live",
    restored_at: datetime | None = None,
    day: ForecastDay = "today",
    roof_id: str | None = None,
) -> dict[str, Any]:
    """Tage und Kennzahlen aus Serverzeit und gespeicherter Anlagenzone auswählen."""
    if day not in ("today", "tomorrow"):
        raise ValueError("Die Kartenansicht unterstützt heute und morgen")
    now = _utc(now)
    timezone = ZoneInfo(timezone_name)
    today = now.astimezone(timezone).date()
    today_start, today_end = _bounds(today, timezone)
    tomorrow_start, tomorrow_end = _bounds(today + timedelta(days=1), timezone)
    source = (
        forecast.total_intervals
        if roof_id is None
        else _roof_intervals(forecast, roof_id)
    )
    today_energy = _window_energy(source, today_start, today_end)
    tomorrow_energy = _window_energy(source, tomorrow_start, tomorrow_end)
    age = now - _utc(fetched_at) if fetched_at is not None else None
    stale_snapshot = (
        not last_update_success
        or age is None
        or not timedelta(0) <= age <= timedelta(minutes=60)
    )
    # Beide Kurven gehören zu denselben Kennzahlen und demselben Serverzeitpunkt.
    day_views = {}
    for selected_day, target_date, start, end, energy in (
        ("today", today, today_start, today_end, today_energy),
        (
            "tomorrow",
            today + timedelta(days=1),
            tomorrow_start,
            tomorrow_end,
            tomorrow_energy,
        ),
    ):
        complete = energy is not None
        day_views[selected_day] = {
            "day": selected_day,
            "date": target_date.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "intervals": [
                {
                    "start": interval.start.isoformat(),
                    "end": interval.end.isoformat(),
                    "energy_kwh": interval.energy_kwh,
                    "ac_power_kw": interval.ac_power_kw,
                    "is_complete": interval.is_complete,
                    "quality_flags": list(interval.quality_flags),
                }
                for interval in _project_intervals(source, start, end)
            ],
            "complete": complete,
            "stale": stale_snapshot or not complete,
        }
    return {
        "view_version": 1,
        "origin": origin,
        "restored_at": _utc(restored_at).isoformat() if restored_at else None,
        "as_of": now.isoformat(),
        "timezone": timezone_name,
        "plant_name": plant_name,
        "roofs": [
            {"id": roof.roof.id, "name": roof.roof.name}
            for roof in forecast.roofs.values()
        ],
        "roof_id": roof_id,
        "today_start": today_start.isoformat(),
        "today_end": today_end.isoformat(),
        "forecast_days": forecast.forecast_days,
        "horizon_shading": shading_metadata(forecast.horizon_shading),
        "daily_forecasts": [
            {
                "date": (forecast.local_date + timedelta(days=offset)).isoformat(),
                "energy_kwh": _window_energy(
                    source,
                    *_bounds(forecast.local_date + timedelta(days=offset), timezone),
                ),
                "tendency": forecast.local_date + timedelta(days=offset)
                >= today + timedelta(days=2),
                "quality_flags": sorted(
                    {
                        flag
                        for interval in _project_intervals(
                            source,
                            *_bounds(
                                forecast.local_date + timedelta(days=offset), timezone
                            ),
                        )
                        for flag in interval.quality_flags
                    }
                ),
                "measured_quality": {
                    "status": "unavailable",
                    "reason": "no_horizon_evaluation",
                },
            }
            for offset in range(
                max(0, (today - forecast.local_date).days), forecast.forecast_days
            )
        ],
        "summary": {
            "today_kwh": today_energy,
            "tomorrow_kwh": tomorrow_energy,
            "remaining_today_kwh": _window_energy(source, now, today_end),
        },
        "day_views": day_views,
        **day_views[day],
    }
