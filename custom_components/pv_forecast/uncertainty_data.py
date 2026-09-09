"""Gemeinsame lesende Tagesbandansicht für Archivaktion und freiwillige Karte."""

from datetime import UTC, datetime, timedelta
from typing import Any

from .history import HistoryArchive
from .uncertainty import evaluate_experience_band, unavailable_band


def current_experience_bands(
    archive: HistoryArchive, now: datetime, configuration_id: str
) -> dict[str, Any]:
    """Nur passende, bereits eingefrorene Tagesstände als Bandbasis ausgeben."""

    if now.utcoffset() is None:
        raise ValueError("Die Bandansicht benötigt einen eindeutigen Zeitpunkt")
    now = now.astimezone(UTC)
    today = now.astimezone(archive.timezone).date()
    records = tuple(archive.records.values())
    days = {}
    for name, offset in (("today", 0), ("tomorrow", 1)):
        target_date = today + timedelta(days=offset)
        targets = [
            record
            for record in records
            if record.target_date == target_date
            and record.horizon in ("daily_previous_18", "daily_same_06")
            and record.cutoff <= now
            and record.configuration_id == configuration_id
        ]
        if not targets:
            days[name] = {
                **unavailable_band("no_frozen_forecast"),
                "target_date": target_date.isoformat(),
            }
            continue
        target = max(targets, key=lambda record: record.cutoff)
        days[name] = evaluate_experience_band(records, target, as_of=now)
    return {
        "schema_version": 1,
        "label": "Erfahrungsband",
        "as_of": now.isoformat(),
        "timezone": archive.timezone.key,
        "basis": "frozen_daily_forecast",
        "retention_truncated": archive.retention_truncated,
        "days": days,
        "remaining_today": unavailable_band("unsupported_horizon"),
        "next_60_minutes": unavailable_band("unsupported_horizon"),
    }
