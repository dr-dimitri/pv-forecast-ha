"""Kohärente, rein lokale Erklärung der tatsächlich angewendeten Modellstufen."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .calculations import apply_inverter_limits
from .forecast_intervals import window_energy
from .models import ForecastResult


@dataclass(frozen=True, slots=True)
class ExplanationInterval:
    """kWh je absolutem Intervall; Faktorbeitrag darf negativ sein."""

    start: datetime
    end: datetime
    before_calibration_kwh: float
    calibration_delta_kwh: float
    group_clipping_kwh: float
    total_clipping_kwh: float
    effective_kwh: float
    morning_delta_kwh: float = 0.0


@dataclass(frozen=True, slots=True)
class ExplanationSnapshot:
    """Identitäten der unveränderten produktiven Roh-/Wirkgeneration bewahren."""

    raw: ForecastResult | None
    effective: ForecastResult | None
    timezone: str
    factor: float
    intervals: tuple[ExplanationInterval, ...] = ()
    reason: str | None = None
    morning_factor: float = 1.0


def _same(a: float, b: float) -> bool:
    return (
        math.isfinite(a)
        and math.isfinite(b)
        and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    )


def build_explanation(
    raw: ForecastResult | None,
    effective: ForecastResult | None,
    factor: float,
    limit: float | None,
    timezone: str,
    *,
    morning_factor: float = 1.0,
    morning_windows: tuple[tuple[datetime, datetime], ...] = (),
) -> ExplanationSnapshot:
    """Einmal je Generation die produktiven Clippingstufen nachvollziehen."""
    base = ExplanationSnapshot(
        raw, effective, timezone, factor, morning_factor=morning_factor
    )
    if raw is None or effective is None or not raw.roofs:
        return replace(base, reason="missing_raw_basis")
    try:
        morning_active = morning_factor != 1
        if not 0.8 <= morning_factor <= 1.2:
            raise ValueError("Ungültiger Morgenfaktor")
        windows = sorted(morning_windows) if morning_active else ()
        for index, (left, right) in enumerate(windows):
            if (
                left.utcoffset() is None
                or right.utcoffset() is None
                or left >= right
                or (index and windows[index - 1][1] > left)
            ):
                raise ValueError("Ungültige Morgenfenster")
        if (
            not 0.5 <= factor <= 1.5
            or raw.local_date != effective.local_date
            or raw.forecast_days != effective.forecast_days
            or raw.horizon_shading != effective.horizon_shading
            or raw.inverter_groups != effective.inverter_groups
            or raw.roofs.keys() != effective.roofs.keys()
            or (
                not morning_active
                and len(raw.total_intervals) != len(effective.total_intervals)
            )
        ):
            raise ValueError("Inkompatible Generation")
        for key, roof in raw.roofs.items():
            other = effective.roofs[key]
            if roof.roof != other.roof or (
                not morning_active
                and [(i.start, i.end, i.dc_power_kw) for i in roof.intervals]
                != [(i.start, i.end, i.dc_power_kw) for i in other.intervals]
            ):
                raise ValueError("Inkompatible Rohbeiträge")
            if morning_active:
                for item in other.intervals:
                    originals = [
                        i
                        for i in roof.intervals
                        if i.start <= item.start and i.end >= item.end
                    ]
                    if (
                        len(originals) != 1
                        or originals[0].dc_power_kw != item.dc_power_kw
                    ):
                        raise ValueError("Inkompatible Morgen-Rohbeiträge")
        intervals = []
        previous_end = None
        for index, output in enumerate(effective.total_intervals):
            if morning_active:
                originals = [
                    item
                    for item in raw.total_intervals
                    if item.start <= output.start and item.end >= output.end
                ]
                if len(originals) != 1:
                    raise ValueError("Fehlende gemeinsame Rohabdeckung")
                original = originals[0]
            else:
                original = raw.total_intervals[index]
            if not morning_active and (original.start, original.end) != (
                output.start,
                output.end,
            ):
                raise ValueError("Inkompatible UTC-Grenzen")
            start, end = output.start.astimezone(UTC), output.end.astimezone(UTC)
            hours = (end - start).total_seconds() / 3600
            if hours <= 0 or (previous_end is not None and start < previous_end):
                raise ValueError("Ungültige Dauer")
            previous_end = end
            if any(start < boundary < end for window in windows for boundary in window):
                raise ValueError("Das Intervall überlappt eine Morgenfenstergrenze")
            local_morning_factor = (
                morning_factor
                if any(left <= start and end <= right for left, right in windows)
                else 1.0
            )
            powers = {}
            for key, roof in raw.roofs.items():
                matches = [
                    i
                    for i in roof.intervals
                    if i.start.astimezone(UTC) <= start and i.end.astimezone(UTC) >= end
                ]
                if len(matches) != 1:
                    raise ValueError("Fehlende Rohabdeckung")
                powers[key] = matches[0].dc_power_kw
            if any(not math.isfinite(p) or p < 0 for p in powers.values()):
                raise ValueError("Ungültige Rohleistung")
            morning_powers = (
                {key: power * local_morning_factor for key, power in powers.items()}
                if local_morning_factor != 1
                else powers
            )
            scaled = {key: power * factor for key, power in morning_powers.items()}
            stages = apply_inverter_limits(
                scaled, limit, raw.inverter_groups, include_stages=True
            )
            before = sum(powers.values()) * hours
            after_morning = sum(morning_powers.values()) * hours
            morning_delta = after_morning - before
            adjusted = sum(scaled.values()) * hours
            grouped = sum(stages.grouped.values()) * hours
            after = sum(stages.effective.values()) * hours
            group_loss, total_loss = max(0.0, adjusted - grouped), max(
                0.0, grouped - after
            )
            delta = adjusted - after_morning
            if (
                not all(
                    math.isfinite(v)
                    for v in (before, morning_delta, delta, group_loss, total_loss)
                )
                or not _same(
                    before + morning_delta + delta - group_loss - total_loss,
                    output.energy_kwh,
                )
                or not _same(after, output.energy_kwh)
            ):
                raise ValueError("Widersprüchliche Bilanz")
            # Die Rohkurve muss ebenfalls zur gespeicherten produktiven AC-Basis passen.
            raw_power = sum(
                i.ac_power_kw
                for roof in raw.roofs.values()
                for i in roof.intervals
                if i.start.astimezone(UTC) <= start and i.end.astimezone(UTC) >= end
            )
            raw_energy = original.energy_kwh * (
                (end - start) / (original.end - original.start)
            )
            if not _same(raw_power * hours, raw_energy):
                raise ValueError("Widersprüchliche Rohkurve")
            intervals.append(
                ExplanationInterval(
                    start,
                    end,
                    before,
                    delta,
                    group_loss,
                    total_loss,
                    output.energy_kwh,
                    morning_delta,
                )
            )
        return replace(base, intervals=tuple(intervals))
    except (ValueError, OverflowError, KeyError, TypeError):
        return replace(base, reason="incompatible_raw_basis")


def explanation_view(
    snapshot: ExplanationSnapshot | None,
    raw: ForecastResult | None,
    effective: ForecastResult,
    timezone: str,
    now: datetime,
    fetched_at: datetime | None,
    last_update_success: bool,
    *,
    day: str = "today",
    origin: str = "live",
) -> dict[str, Any]:
    """Nur UTC-Überlappungen projizieren; keine Begrenzung in der Leseaktion."""
    target = now.astimezone(ZoneInfo(timezone)).date() + timedelta(
        days=day == "tomorrow"
    )
    start = datetime.combine(target, time.min, ZoneInfo(timezone)).astimezone(UTC)
    end = datetime.combine(
        target + timedelta(days=1), time.min, ZoneInfo(timezone)
    ).astimezone(UTC)
    expected = window_energy(effective.total_intervals, start, end)
    result = {
        "schema_version": 1,
        "scope": "total",
        "date": target.isoformat(),
        "timezone": timezone,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": now.isoformat(),
        "origin": origin,
        "fetched_at": fetched_at.isoformat() if fetched_at else None,
        "last_update_success": last_update_success,
        "stale": not last_update_success
        or fetched_at is None
        or not timedelta(0) <= now - fetched_at <= timedelta(minutes=60),
        "complete": expected is not None,
        "status": "unavailable",
        "reason": "missing_raw_basis",
        "assumptions": [
            "temperature_model",
            "configured_efficiency",
            "constant_interval_mean_power",
        ],
        "quality_flags": sorted(
            {
                flag
                for i in effective.total_intervals
                if i.start < end and i.end > start
                for flag in i.quality_flags
            }
        ),
    }
    if effective.horizon_shading:
        result["assumptions"].append("horizon_profile")
    if snapshot is None or raw is None:
        return result
    if (
        snapshot.raw is not raw
        or snapshot.effective is not effective
        or snapshot.timezone != timezone
    ):
        return result | {"reason": "incompatible_generation"}
    if snapshot.reason:
        return result | {"reason": snapshot.reason}
    if expected is None:
        return result | {"reason": "incomplete_coverage"}
    fields = (
        "before_calibration_kwh",
        "morning_delta_kwh",
        "calibration_delta_kwh",
        "group_clipping_kwh",
        "total_clipping_kwh",
        "effective_kwh",
    )
    intervals, raw_intervals = [], []
    covered = 0.0
    for index, item in enumerate(snapshot.intervals):
        left, right = max(start, item.start), min(end, item.end)
        if left >= right:
            continue
        fraction = (right - left) / (item.end - item.start)
        covered += (right - left).total_seconds()
        intervals.append(
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                **{field: getattr(item, field) * fraction for field in fields},
            }
        )
        if snapshot.morning_factor == 1:
            original = raw.total_intervals[index]
            raw_fraction = fraction
        else:
            originals = [
                original
                for original in raw.total_intervals
                if original.start <= left and original.end >= right
            ]
            if len(originals) != 1:
                return result | {"reason": "incomplete_coverage"}
            original = originals[0]
            raw_fraction = (right - left) / (original.end - original.start)
        raw_intervals.append(
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                "energy_kwh": original.energy_kwh * raw_fraction,
                "is_complete": original.is_complete,
                "quality_flags": list(original.quality_flags),
            }
        )
    if covered != (end - start).total_seconds():
        return result | {"reason": "incomplete_coverage"}
    totals = {field: sum(item[field] for item in intervals) for field in fields}
    # Die UTC-Zeitreihe enthält auch nach Mitternacht vollständig belegte
    # Folgetage. Vorhandene gespeicherte Tageskennzahlen bleiben gegenprüfbar.
    stored_daily = (
        effective.total.today
        if target == effective.local_date
        else (
            effective.total.tomorrow
            if target == effective.local_date + timedelta(days=1)
            else None
        )
    )
    if not _same(totals["effective_kwh"], expected) or (
        stored_daily is not None and not _same(stored_daily, expected)
    ):
        return result | {"reason": "inconsistent_daily_total"}
    baseline = sum(item["energy_kwh"] for item in raw_intervals)
    difference = totals["effective_kwh"] - baseline
    values = [*totals.values(), baseline, difference]
    if not all(math.isfinite(value) for value in values):
        return result | {"reason": "nonfinite_result"}
    return result | {
        "status": "available",
        "reason": None,
        "factor": snapshot.factor,
        "morning_factor": snapshot.morning_factor,
        "totals": totals
        | {
            "raw_model_kwh": baseline,
            "effective_minus_raw_kwh": difference,
            "effective_minus_raw_percent": (
                difference / baseline * 100
                if baseline > 0 and math.isfinite(difference / baseline * 100)
                else None
            ),
        },
        "intervals": intervals,
        "raw_intervals": raw_intervals,
    }
