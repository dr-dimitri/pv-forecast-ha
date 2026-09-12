"""Begrenzte historische Tagesansichten aus unveränderten Archivbelegen."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .history import ArchiveRecord


def _bounds(day: Date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(day, time.min, zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def _assessment(
    record: ArchiveRecord, left: datetime, right: datetime, now: datetime
) -> dict[str, Any]:
    current = record.assessment
    valid = bool(
        current
        and current.valid
        and not record.deleted_sources
        and current.assessed_at <= now
    )
    whole = current.actual_energy_kwh if valid else None
    clipped = left != record.start or right != record.end
    exact = valid and (not clipped or whole == 0)
    return {
        "energy_kwh": whole if exact else None,
        "complete": exact,
        "whole_interval_energy_kwh": whole,
        "assessed_at": current.assessed_at.isoformat() if current else None,
        "revised": bool(record.assessment_revisions),
        "previous_revision_count": len(record.assessment_revisions),
        "manual_correction": bool(current and current.manual),
        "measured_energy_kwh": (
            record.measured_assessment.actual_energy_kwh
            if current and current.manual and record.measured_assessment
            else None
        ),
        "reasons": (
            ["deleted_sources"]
            if record.deleted_sources
            else (
                ["measurement_boundary"]
                if valid and not exact
                else list(current.reasons) if current else ["missing_assessment"]
            )
        ),
    }


def _record_view(
    record: ArchiveRecord, left: datetime, right: datetime, now: datetime
) -> dict[str, Any]:
    fraction = (right - left) / (record.end - record.start)
    return {
        "start": left.isoformat(),
        "end": right.isoformat(),
        "source_start": record.start.isoformat(),
        "source_end": record.end.isoformat(),
        "raw_energy_kwh": record.raw_energy_kwh * fraction,
        "energy_kwh": record.effective_energy_kwh * fraction,
        "calibrated_energy_kwh": (
            record.calibrated_energy_kwh * fraction
            if record.calibrated_energy_kwh is not None
            else None
        ),
        "applied_factor": record.applied_factor,
        "cutoff": record.cutoff.isoformat(),
        "fetched_at": record.fetched_at.isoformat(),
        "observed_at": record.observed_at.isoformat(),
        "quality_flags": list(record.quality_flags),
        "measurement": _assessment(record, left, right, now),
    }


def build_archive_day_view(
    records: Sequence[ArchiveRecord],
    now: datetime,
    active_configuration_id: str,
    active_timezone: str,
    *,
    date: str,
    configuration_id: str | None = None,
    horizon: str = "hourly_1h",
    retention_truncated: bool = False,
) -> dict[str, Any]:
    """Explizit ausgewählte Kontexte ohne Berichte, Bandberechnung oder I/O lesen."""
    if (
        not isinstance(date, str)
        or len(date) != 10
        or Date.fromisoformat(date).isoformat() != date
    ):
        raise ValueError("Das Archivdatum benötigt YYYY-MM-DD")
    if horizon not in ("hourly_1h", "hourly_3h"):
        raise ValueError("Ungültiger archivierter Stundenhorizont")
    selected_date = Date.fromisoformat(date)
    context = (
        configuration_id if configuration_id is not None else active_configuration_id
    )
    if not isinstance(context, str) or not context:
        raise ValueError("Eine gültige Archivkonfiguration ist erforderlich")
    groups = {(record.configuration_id, record.timezone) for record in records}
    groups.add((active_configuration_id, active_timezone))
    zones = {zone for config, zone in groups if config == context}
    if len(zones) != 1:
        raise ValueError("Der Archivkontext ist unbekannt oder nicht eindeutig")
    timezone = next(iter(zones))
    today = now.astimezone(ZoneInfo(timezone)).date()
    if not today - timedelta(days=90) <= selected_date < today:
        raise ValueError(
            "Der Archivtag muss innerhalb der letzten 90 abgeschlossenen Tage liegen"
        )
    start, end = _bounds(selected_date, timezone)
    contexts = []
    for config, zone in sorted(groups):
        local_today = now.astimezone(ZoneInfo(zone)).date()
        contexts.append(
            {
                "configuration_id": config,
                "timezone": zone,
                "active": (config, zone) == (active_configuration_id, active_timezone),
                "min_date": (local_today - timedelta(days=90)).isoformat(),
                "max_date": (local_today - timedelta(days=1)).isoformat(),
            }
        )
    result = {
        "schema_version": 1,
        "scope": "total",
        "date": date,
        "timezone": timezone,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": now.astimezone(UTC).isoformat(),
        "configuration_id": context,
        "horizon": horizon,
        "label": (
            "Jeweils 1 Stunde vorher"
            if horizon == "hourly_1h"
            else "Jeweils 3 Stunden vorher"
        ),
        "contexts": contexts,
        "retention_truncated": retention_truncated,
        "status": "empty",
        "reason": "no_records",
        "intervals": [],
        "daily_forecasts": {},
        "daily_measurement": {"energy_kwh": None, "complete": False},
        "coverage": {"forecast_fraction": 0.0, "measurement_fraction": 0.0},
    }
    selected = [
        r
        for r in records
        if r.configuration_id == context
        and r.timezone == timezone
        and r.start < end
        and r.end > start
        and r.horizon in (horizon, "daily_previous_18", "daily_same_06")
    ]
    identities = {
        (
            r.model_version,
            tuple(
                sorted(
                    (s.source_id, s.measurement_identity) for s in r.measurement_sources
                )
            ),
        )
        for r in selected
    }
    if len(identities) > 1:
        return result | {"status": "unavailable", "reason": "ambiguous_context"}
    hours = sorted((r for r in selected if r.horizon == horizon), key=lambda r: r.start)
    if len(hours) > 50 or any(a.end > b.start for a, b in pairwise(hours)):
        return result | {"status": "unavailable", "reason": "ambiguous_intervals"}
    intervals = [
        _record_view(r, max(start, r.start), min(end, r.end), now) for r in hours
    ]
    daily = {}
    daily_records = []
    for kind in ("daily_previous_18", "daily_same_06"):
        matches = [
            r
            for r in selected
            if r.horizon == kind
            and r.start == start
            and r.end == end
            and r.target_date == selected_date
        ]
        if len(matches) > 1:
            return result | {"status": "unavailable", "reason": "ambiguous_context"}
        daily[kind] = _record_view(matches[0], start, end, now) if matches else None
        daily_records.extend(matches)
    measured = [r for r in daily_records if r.assessment is not None]
    if measured:
        latest = max(r.assessment.assessed_at for r in measured)
        latest_views = [
            _assessment(r, start, end, now)
            for r in measured
            if r.assessment.assessed_at == latest
        ]
        if all(item == latest_views[0] for item in latest_views):
            result["daily_measurement"] = latest_views[0]
        elif all(
            item["manual_correction"]
            and item["complete"]
            and item["energy_kwh"] == latest_views[0]["energy_kwh"]
            for item in latest_views
        ):
            result["daily_measurement"] = latest_views[0] | {
                "measured_energy_kwh": None
            }
    seconds = (end - start).total_seconds()
    covered = sum(
        (min(r.end, end) - max(r.start, start)).total_seconds() for r in hours
    )
    measured_seconds = sum(
        (
            datetime.fromisoformat(i["end"]) - datetime.fromisoformat(i["start"])
        ).total_seconds()
        for i in intervals
        if i["measurement"]["complete"]
    )
    return result | {
        "status": (
            "complete"
            if covered == seconds
            else "partial" if intervals or daily_records else "empty"
        ),
        "reason": None if intervals or daily_records else "no_records",
        "model_version": next(iter(identities))[0] if identities else None,
        "intervals": intervals,
        "daily_forecasts": daily,
        "coverage": {
            "forecast_fraction": covered / seconds,
            "measurement_fraction": measured_seconds / seconds,
        },
    }
