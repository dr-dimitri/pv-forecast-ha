"""Konfiguration und gleiche Messpaare für einen separaten Temperaturvergleich."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from math import fsum, isfinite
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .history import ArchiveRecord
    from .models import PvRoof

MODEL = "ross_comparison_v1"
# Repräsentative Referenzwerte aus pvlib.temperature.ross; keine Anlagenmessung.
COEFFICIENTS = {
    "pitched_ventilated": 0.02,
    "free_standing": 0.0208,
    "flat_ventilated": 0.026,
    "pitched_poorly_ventilated": 0.0342,
}


def mountings_from_options(
    options: Mapping[str, Any], roofs: Sequence[PvRoof]
) -> dict[str, str] | None:
    """Alle aktiven Dächer benötigen bewusst gewählte Vergleichsannahmen."""

    configured = options.get("temperature_mountings", {})
    if not isinstance(configured, Mapping) or not roofs:
        return None
    selected = {roof.id: configured.get(roof.id) for roof in roofs}
    if any(
        not isinstance(value, str) or value not in COEFFICIENTS
        for value in selected.values()
    ):
        return None
    return selected


def parameter_id(mountings: Mapping[str, str]) -> str:
    """Parameterwahl unabhängig von Anzeigenamen und Dictionary-Reihenfolge trennen."""

    return hashlib.sha256(
        json.dumps([MODEL, sorted(mountings.items())]).encode()
    ).hexdigest()


def validate_comparison(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        mountings = value["mountings"]
        valid = (
            type(value["schema_version"]) is int
            and value["schema_version"] == 1
            and value["model"] == MODEL
            and isinstance(mountings, dict)
            and bool(mountings)
            and all(
                isinstance(key, str)
                and key
                and isinstance(mount, str)
                and mount in COEFFICIENTS
                for key, mount in mountings.items()
            )
            and value["parameter_id"] == parameter_id(mountings)
            and type(value["energy_kwh"]) in (int, float)
            and isfinite(value["energy_kwh"])
            and value["energy_kwh"] >= 0
        )
    except (TypeError, KeyError, OverflowError):
        valid = False
    if not valid:
        raise ValueError("Ungültiger gespeicherter Temperaturvergleich")
    return value


def comparison_report(
    records: Sequence[ArchiveRecord],
    now: datetime,
    configuration_id: str,
    mountings: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Rohmodelle ohne übertragene Kalibrierung auf identischen Messungen bewerten."""

    key = parameter_id(mountings) if mountings else None
    result = {
        "schema_version": 1,
        "model": MODEL,
        "parameter_id": key,
        "mountings": dict(mountings) if mountings else {},
        "window_days": 90,
        "applied": False,
        "horizons": {},
    }
    for horizon in ("daily_previous_18", "daily_same_06", "hourly_1h", "hourly_3h"):
        selected = [
            record
            for record in records
            if record.horizon == horizon
            and record.configuration_id == configuration_id
            and now.astimezone(ZoneInfo(record.timezone)).date() - timedelta(days=90)
            <= record.target_date
            < now.astimezone(ZoneInfo(record.timezone)).date()
        ]
        valid = [
            record
            for record in selected
            if record.temperature_comparison is not None
            and key is not None
            and record.temperature_comparison["parameter_id"] == key
            and not record.quality_flags
            and not record.deleted_sources
            and record.assessment is not None
            and record.assessment.valid
            and record.assessment.assessed_at <= now
        ]
        count = len(valid)
        baseline = [
            record.raw_energy_kwh - record.assessment.actual_energy_kwh
            for record in valid
        ]
        alternative = [
            record.temperature_comparison["energy_kwh"]
            - record.assessment.actual_energy_kwh
            for record in valid
        ]

        def mean(values, count=count):
            try:
                result = fsum(value / count for value in values) if count else None
                return result if result is None or isfinite(result) else None
            except OverflowError:
                return None

        result["horizons"][horizon] = {
            "count": count,
            "days": len({record.target_date for record in valid}),
            "count_forecasts": len(selected),
            "coverage_fraction": count / len(selected) if selected else None,
            "raw_mae_kwh": mean([abs(value) for value in baseline]),
            "alternative_mae_kwh": mean([abs(value) for value in alternative]),
            "raw_bias_kwh": mean(baseline),
            "alternative_bias_kwh": mean(alternative),
            "excluded_count": len(selected) - count,
        }
    return result
