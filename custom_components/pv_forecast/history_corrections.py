"""Bestätigte Tageswahrheit mit erhaltenem automatischem Messbeleg, ohne HA/I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .history import ArchiveRecord, HistoryArchive

DAILY_HORIZONS = ("daily_previous_18", "daily_same_06")


def _context(record: ArchiveRecord) -> tuple:
    return (
        record.start,
        record.end,
        record.timezone,
        record.configuration_id,
        tuple(
            sorted(
                (s.source_id, s.measurement_identity)
                for s in record.measurement_sources
            )
        ),
    )


def correction_groups(
    archive: HistoryArchive, now: datetime
) -> list[tuple[ArchiveRecord, ...]]:
    """Beide Tagesstichtage nur bei identischer physischer Messgrenze verbinden."""
    groups: dict[tuple, list[ArchiveRecord]] = {}
    for record in archive.records.values():
        today = now.astimezone(ZoneInfo(record.timezone)).date()
        if (
            record.horizon not in DAILY_HORIZONS
            or not today - timedelta(days=365) <= record.target_date < today
            or record.deleted_sources
            or not record.measurement_sources
            or not archive._configuration_valid(record)
        ):
            continue
        groups.setdefault(_context(record), []).append(record)
    return [
        tuple(sorted(records, key=lambda r: r.horizon)) for records in groups.values()
    ]


def correction_group(
    archive: HistoryArchive, record_id: str, now: datetime
) -> tuple[ArchiveRecord, ...]:
    for group in correction_groups(archive, now):
        if any(r.record_id == record_id for r in group):
            return group
    raise ValueError("daily_correction_unavailable")


def correction_revision(records: tuple[ArchiveRecord, ...]) -> str:
    """Auch erneutes Eingeben desselben Werts macht alte Dialoge ungültig."""
    return hashlib.sha256(
        json.dumps(
            [r.to_dict() for r in records], sort_keys=True, allow_nan=False
        ).encode()
    ).hexdigest()


def correct_day(
    archive: HistoryArchive,
    record_id: str,
    energy_kwh: float | None,
    now: datetime,
    *,
    expected_revision: str,
) -> bool:
    """None nimmt zurück; Null bestätigt einen Nulltag, ohne Stunden zu erfinden."""
    from .history import MAX_REVISIONS, Assessment

    if now.utcoffset() is None:
        raise ValueError("daily_correction_invalid")
    now = now.astimezone(UTC)
    records = correction_group(archive, record_id, now)
    if correction_revision(records) != expected_revision:
        raise ValueError("daily_correction_changed")
    if energy_kwh is not None and (
        isinstance(energy_kwh, bool)
        or not isinstance(energy_kwh, (int, float))
        or not isfinite(energy_kwh)
        or energy_kwh < 0
    ):
        raise ValueError("daily_correction_invalid")
    if any(r.assessment and r.assessment.assessed_at > now for r in records):
        raise ValueError("daily_correction_changed")
    changed = False
    for record in records:
        previous = record.assessment
        is_manual = bool(previous and previous.manual)
        if energy_kwh is None:
            if not is_manual:
                continue
            original = record.measured_assessment
            assessment = (
                replace(original, assessed_at=now)
                if original is not None
                else Assessment(now, None, False, ("measurements_missing",), ())
            )
            if assessment.valid and assessment.has_derived_gap:
                assessment = replace(
                    assessment,
                    actual_energy_kwh=None,
                    valid=False,
                    reasons=("derived_measurement_gap",),
                )
            measured = None
        else:
            if is_manual and previous.actual_energy_kwh == energy_kwh:
                continue
            measured = record.measured_assessment if is_manual else previous
            assessment = Assessment(now, float(energy_kwh), True, (), (), manual=True)
        revisions = record.assessment_revisions
        if previous is not None:
            revisions = (*revisions, previous)[-MAX_REVISIONS:]
        archive.records[record.record_id] = replace(
            record,
            assessment=assessment,
            measured_assessment=measured,
            assessment_revisions=revisions,
        )
        changed = True
    return changed
