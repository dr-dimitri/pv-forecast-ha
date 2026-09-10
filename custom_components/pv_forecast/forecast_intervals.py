"""Gemeinsame reine UTC-Projektion vorhandener Prognoseenergien."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from math import fsum, isfinite

from .models import TotalForecastInterval


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Die Prognose benötigt eindeutige Zeitpunkte")
    return value.astimezone(UTC)


def _valid_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return isfinite(value) and value >= 0
    except OverflowError:
        return False


def project_intervals(
    intervals: Sequence[TotalForecastInterval], start: datetime, end: datetime
) -> tuple[TotalForecastInterval, ...]:
    """Vorhandene Prognoseenergie ausschließlich nach UTC-Überlappung darstellen."""
    result = []
    for interval in intervals:
        original_start, original_end = _utc(interval.start), _utc(interval.end)
        left, right = max(start, original_start), min(end, original_end)
        if (
            right <= left
            or not _valid_number(interval.energy_kwh)
            or not _valid_number(interval.ac_power_kw)
        ):
            continue
        fraction = (right - left).total_seconds() / (
            original_end - original_start
        ).total_seconds()
        result.append(
            TotalForecastInterval(
                left,
                right,
                interval.energy_kwh * fraction,
                interval.ac_power_kw,
                interval.quality_flags,
                interval.is_complete,
            )
        )
    return tuple(sorted(result, key=lambda interval: interval.start))


def window_energy(
    intervals: Sequence[TotalForecastInterval], start: datetime, end: datetime
) -> float | None:
    """Nur vollständig belegte Prognosefenster als Kennzahl anbieten."""
    if start == end:
        return 0.0
    cursor = start
    values = []
    for interval in project_intervals(intervals, start, end):
        if interval.start != cursor or not interval.is_complete:
            return None
        values.append(interval.energy_kwh)
        cursor = interval.end
    if cursor != end:
        return None
    try:
        result = fsum(values)
    except OverflowError:
        return None
    return result if _valid_number(result) else None
