"""Verlustfreier Rohmodellcache mit strikter, HA-unabhängiger Validierung."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from math import fsum, isclose
from typing import Any
from zoneinfo import ZoneInfo

from .calculations import aggregate_energy_for_day, apply_inverter_limits
from .forecast_intervals import _utc, _valid_number, window_energy
from .history import MODEL_VERSION
from .models import (
    AcInverterGroup,
    DailyYield,
    ForecastResult,
    PvRoof,
    RoofForecast,
    RoofForecastInterval,
    TotalForecastInterval,
)

CACHE_VERSION = 1
MAX_CACHE_BYTES = 8 * 1024 * 1024
CONF_FORECAST_CACHE = "forecast_cache_enabled"


@dataclass(frozen=True, slots=True)
class CachedForecast:
    """Ursprüngliche Rohprognose und tatsächlicher erfolgreicher Abrufzeitpunkt."""

    forecast: ForecastResult
    fetched_at: datetime


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Ungültiger Cachezeitpunkt")
    return _utc(datetime.fromisoformat(value))


def _number(value: object) -> float:
    if not _valid_number(value):
        raise ValueError("Ungültige Cachezahl")
    return float(value)


def _same(left: float, right: float) -> None:
    if not isclose(left, right, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError("Die Rohmodellbilanz des Caches stimmt nicht überein")


def encode_forecast(
    forecast: ForecastResult,
    fetched_at: datetime,
    entry_id: str,
    configuration_id: str,
    timezone_name: str,
    limit: float | None,
) -> dict[str, Any]:
    """Nur explizite Rohbeiträge speichern, ohne Namen oder Wetterantworten."""
    data = {
        "schema_version": CACHE_VERSION,
        "model_version": MODEL_VERSION,
        "entry_id": entry_id,
        "configuration_id": configuration_id,
        "timezone": timezone_name,
        "forecast_days": forecast.forecast_days,
        "local_date": forecast.local_date.isoformat(),
        "fetched_at": _utc(fetched_at).isoformat(),
        "inverter_max_power_kw": limit,
        "groups": [
            {"id": g.id, "max_power_kw": g.max_power_kw, "roof_ids": list(g.roof_ids)}
            for g in forecast.inverter_groups
        ],
        "horizon_shading": forecast.horizon_shading,
        "roofs": {
            key: [
                {
                    "start": _utc(i.start).isoformat(),
                    "end": _utc(i.end).isoformat(),
                    "dc_power_kw": i.dc_power_kw,
                    "ac_power_kw": i.ac_power_kw,
                    "energy_kwh": i.energy_kwh,
                }
                for i in roof.intervals
            ]
            for key, roof in forecast.roofs.items()
        },
        "intervals": [
            {
                "start": _utc(i.start).isoformat(),
                "end": _utc(i.end).isoformat(),
                "energy_kwh": i.energy_kwh,
                "ac_power_kw": i.ac_power_kw,
                "quality_flags": list(i.quality_flags),
                "is_complete": i.is_complete,
            }
            for i in forecast.total_intervals
        ],
    }
    if (
        len(json.dumps(data, allow_nan=False, indent=2).encode())
        > MAX_CACHE_BYTES - 1024
    ):
        raise OverflowError("Der Prognosecache überschreitet seine Größenbegrenzung")
    return data


def decode_forecast(
    data: dict[str, Any],
    *,
    entry_id: str,
    configuration_id: str,
    timezone_name: str,
    forecast_days: int,
    roofs: tuple[PvRoof, ...],
    groups: tuple[AcInverterGroup, ...],
    limit: float | None,
    now: datetime,
) -> CachedForecast:
    """Zuordnung, UTC-Abdeckung und Roh-/AC-Bilanzen vor Wiederherstellung prüfen."""
    now = _utc(now)
    if (
        len(json.dumps(data, allow_nan=False, indent=2).encode())
        > MAX_CACHE_BYTES - 1024
    ):
        raise ValueError("Der Prognosecache ist zu groß")
    expected_groups = [
        {"id": g.id, "max_power_kw": g.max_power_kw, "roof_ids": list(g.roof_ids)}
        for g in groups
    ]
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != CACHE_VERSION
        or data["model_version"] != MODEL_VERSION
        or data["entry_id"] != entry_id
        or data["configuration_id"] != configuration_id
        or data["timezone"] != timezone_name
        or type(data["forecast_days"]) is not int
        or data["forecast_days"] != forecast_days
        or data["groups"] != expected_groups
        or data["inverter_max_power_kw"] != limit
        or type(data["horizon_shading"]) is not bool
        or data["horizon_shading"] != any(any(r.horizon_profile) for r in roofs)
    ):
        raise ValueError(
            "Der Prognosecache gehört nicht zur aktuellen Modellkonfiguration"
        )
    if data["inverter_max_power_kw"] is not None:
        _number(data["inverter_max_power_kw"])
    for group in data["groups"]:
        _number(group["max_power_kw"])
    fetched = _timestamp(data["fetched_at"])
    day = date.fromisoformat(data["local_date"])
    zone = ZoneInfo(timezone_name)
    start = _utc(datetime.combine(day, time.min, zone))
    end = _utc(datetime.combine(day + timedelta(days=forecast_days), time.min, zone))
    current = _utc(datetime.combine(now.astimezone(zone).date(), time.min, zone))
    if fetched > now or fetched.astimezone(zone).date() != day or end <= current:
        raise ValueError("Der Cachezeitraum ist nicht verwendbar")
    if not isinstance(data["roofs"], dict) or set(data["roofs"]) != {
        r.id for r in roofs
    }:
        raise ValueError("Unvollständige Dachdaten im Prognosecache")
    results = {}
    for roof in roofs:
        rows = data["roofs"][roof.id]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 170:
            raise ValueError("Ungültige Anzahl Dachintervalle")
        items = tuple(
            RoofForecastInterval(
                _timestamp(i["start"]),
                _timestamp(i["end"]),
                _number(i["dc_power_kw"]),
                _number(i["ac_power_kw"]),
                _number(i["energy_kwh"]),
            )
            for i in rows
        )
        if (
            items[0].start > start
            or items[-1].end < end
            or any(a.end != b.start for a, b in pairwise(items))
            or any(
                not timedelta(0) < i.end - i.start <= timedelta(hours=1)
                or i.end <= start
                or i.start >= end
                for i in items
            )
        ):
            raise ValueError("Die Dachintervalle sind nicht vollständig und eindeutig")
        for item in items:
            _same(
                item.energy_kwh,
                item.ac_power_kw * (item.end - item.start).total_seconds() / 3600,
            )
        results[roof.id] = RoofForecast(
            roof,
            items,
            DailyYield(
                aggregate_energy_for_day(items, day, zone),
                aggregate_energy_for_day(items, day + timedelta(days=1), zone),
            ),
        )
    rows = data["intervals"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 170:
        raise ValueError("Ungültige Anzahl Gesamtintervalle")
    intervals = []
    for row in rows:
        flags = row["quality_flags"]
        if (
            row["is_complete"] is not True
            or not isinstance(flags, list)
            or any(
                flag
                not in (
                    "gti_fallback",
                    "temperature_fallback",
                    "horizon_input_fallback",
                )
                for flag in flags
            )
        ):
            raise ValueError("Unvollständige oder unbekannte Eingabequalität")
        item = TotalForecastInterval(
            _timestamp(row["start"]),
            _timestamp(row["end"]),
            _number(row["energy_kwh"]),
            _number(row["ac_power_kw"]),
            tuple(flags),
        )
        if not start <= item.start < item.end <= end:
            raise ValueError("Ungültige Gesamtgrenzen")
        leaves = {
            key: [
                i for i in roof.intervals if i.start <= item.start and i.end >= item.end
            ]
            for key, roof in results.items()
        }
        if any(len(value) != 1 for value in leaves.values()):
            raise ValueError("Nicht eindeutige Dachbeiträge")
        clipped = apply_inverter_limits(
            {key: value[0].dc_power_kw for key, value in leaves.items()}, limit, groups
        )
        for key, value in leaves.items():
            _same(value[0].ac_power_kw, clipped[key])
        _same(item.ac_power_kw, fsum(value[0].ac_power_kw for value in leaves.values()))
        _same(
            item.energy_kwh,
            item.ac_power_kw * (item.end - item.start).total_seconds() / 3600,
        )
        intervals.append(item)
    if (
        any(a.end != b.start for a, b in pairwise(intervals))
        or window_energy(intervals, start, end) is None
    ):
        raise ValueError("Unvollständige Gesamtprognose")
    result = ForecastResult(
        day,
        results,
        DailyYield(
            aggregate_energy_for_day(intervals, day, zone),
            aggregate_energy_for_day(intervals, day + timedelta(days=1), zone),
        ),
        tuple(intervals),
        groups,
        forecast_days,
        data["horizon_shading"],
    )
    return CachedForecast(result, fetched)
