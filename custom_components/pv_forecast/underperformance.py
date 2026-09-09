"""Experimentelle Gesamthinweise aus einer festen, zuvor geprüften Tagesbasis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import fsum, isfinite
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .history import ArchiveRecord

MAX_BYTES = 64 * 1024


def _timestamp(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("Die Beobachtung benötigt einen eindeutigen Zeitpunkt")
    return instant.astimezone(UTC)


def empty_state(start: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "segment_start": start.isoformat() if start else None,
        "event": None,
    }


def validate_state(value: Any) -> dict[str, Any]:
    """Begrenzte Referenzen prüfen; unbekannte Regeln niemals überschreiben."""

    if value is None:
        return empty_state()
    try:
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("Unbekannte Beobachtungsregel")
        if value["segment_start"] is not None:
            _timestamp(value["segment_start"])
        event = value["event"]
        if event is not None:
            _timestamp(event["created_at"])
            first, last = date.fromisoformat(event["first_day"]), date.fromisoformat(
                event["last_day"]
            )
            if (
                last - first != timedelta(days=6)
                or len(event["case_ids"]) != 7
                or len(set(event["case_ids"])) != 7
                or not isinstance(event["evidence"], dict)
                or len(event["evidence"]) != 97
                or not all(
                    isinstance(k, str) and k and isinstance(v, str) and len(v) == 64
                    for k, v in event["evidence"].items()
                )
                or not set(event["case_ids"]).issubset(event["evidence"])
                or event["target_id"] != event["case_ids"][0]
                or not isinstance(event["configuration_id"], str)
                or not event["configuration_id"]
                or not isinstance(event["id"], str)
                or len(event["id"]) != 64
                or type(event["accepted_factor"]) not in (int, float)
                or not isfinite(event["accepted_factor"])
                or not 0.5 <= event["accepted_factor"] <= 1.5
                or type(event["acknowledged"]) is not bool
                or type(event["notified"]) is not bool
            ):
                raise ValueError("Ungültiger gespeicherter Minderertragshinweis")
        if len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES:
            raise ValueError(
                "Der Minderertragshinweis überschreitet seine Speichergrenze"
            )
    except (KeyError, TypeError, OverflowError, AttributeError) as err:
        raise ValueError("Beschädigter Minderertragshinweis") from err
    return deepcopy(value)


def fingerprint(record: ArchiveRecord) -> str:
    """Auch spätere Messrevisionen dürfen eine alte Referenz nicht still ersetzen."""

    return hashlib.sha256(
        json.dumps(record.to_dict(), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def discard_invalid_references(
    state: dict[str, Any], records: Mapping[str, ArchiveRecord]
) -> bool:
    """Nach Quellenlöschung auch abhängige Hinweisreferenzen gezielt entfernen."""

    event = state["event"]
    if event and any(
        key not in records or records[key].deleted_sources for key in event["evidence"]
    ):
        state["event"] = None
        return True
    return False


def _eligible(record: ArchiveRecord, now: datetime, excluded: set[date]) -> bool:
    actual = record.assessment
    return bool(
        record.target_date not in excluded
        and not record.quality_flags
        and not record.deleted_sources
        and record.raw_energy_kwh >= 0.1
        and isfinite(record.raw_energy_kwh)
        and actual is not None
        and actual.valid
        and record.end <= actual.assessed_at <= now
        and actual.actual_energy_kwh is not None
        and isfinite(actual.actual_energy_kwh)
        and actual.actual_energy_kwh >= 0
        and record.measurement_sources
        and all(
            s.confirmed_pv and s.confirmed_disjoint
            for s in record.measurement_sources
            if s.kind != "power"
        )
        and any(s.kind != "power" for s in record.measurement_sources)
    )


def _identity(record: ArchiveRecord) -> tuple:
    return (
        record.configuration_id,
        record.timezone,
        record.model_version,
        tuple(
            sorted(
                (s.source_id, s.measurement_identity)
                for s in record.measurement_sources
                if s.kind != "power"
            )
        ),
    )


def _comparison(
    records: Sequence[ArchiveRecord], cases: Sequence[ArchiveRecord], now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .uncertainty import _bounded, _maximum, evaluate_experience_band

    target = replace(cases[0], calibrated_energy_kwh=None)
    band = evaluate_experience_band(records, target, as_of=now, include_reference=True)
    if band["status"] != "available":
        return {"status": "basis_unavailable", "reasons": band["reasons"]}, band
    residuals = tuple(band["reference"]["residuals_kwh"])
    lower = []
    for case in cases:
        limit = case.basis.inverter_max_power_kw if case.basis else None
        if (
            case.basis
            and case.basis.group_limits
            and not case.basis.has_ungrouped_roofs
        ):
            group_limit = fsum(value for _, value in case.basis.group_limits)
            limit = min(limit, group_limit) if limit is not None else group_limit
        lower.append(_bounded(case.raw_energy_kwh, residuals, _maximum(case, limit))[0])
    rows = [
        {
            "date": case.target_date.isoformat(),
            "raw_kwh": case.raw_energy_kwh,
            "actual_kwh": case.assessment.actual_energy_kwh,
            "lower_kwh": bound,
            "below": case.assessment.actual_energy_kwh < bound
            and case.raw_energy_kwh - case.assessment.actual_energy_kwh
            > max(0.1, 0.2 * case.raw_energy_kwh),
        }
        for case, bound in zip(cases, lower, strict=True)
    ]
    expected = fsum(case.raw_energy_kwh for case in cases)
    actual = fsum(case.assessment.actual_energy_kwh for case in cases)
    below = sum(row["below"] for row in rows)
    fraction = (expected - actual) / expected
    return {
        "status": "clear",
        "reasons": [],
        "first_day": cases[0].target_date.isoformat(),
        "last_day": cases[-1].target_date.isoformat(),
        "days": 7,
        "below_days": below,
        "raw_kwh": expected,
        "actual_kwh": actual,
        "difference_kwh": expected - actual,
        "shortfall_fraction": fraction,
        "coverage_fraction": 1.0,
        "cases": rows,
        "comparison": {
            key: band[key]
            for key in (
                "training_count",
                "validation_count",
                "training_period",
                "validation_period",
                "evaluation",
                "variant",
            )
        },
        "detected": below >= 5 and fraction > 0.2,
    }, band


def observe(
    records: Sequence[ArchiveRecord],
    state: dict[str, Any],
    now: datetime,
    configuration_id: str,
    timezone: str,
    excluded: set[date],
    *,
    accepted_factor: float = 1.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ein Ereignis festhalten; weder Nachlernen noch eine Erholung behaupten."""

    if now.utcoffset() is None:
        raise ValueError("Die Beobachtung benötigt einen eindeutigen Zeitpunkt")
    state = deepcopy(state)
    base = {"schema_version": 1, "experimental": True, "learning_paused": False}
    indexed = {record.record_id: record for record in records}
    event = state["event"]
    if event and event["configuration_id"] != configuration_id:
        state = empty_state(now)
        event = None
    if event:
        base.update(
            learning_paused=True,
            event_id=event["id"],
            acknowledged=event["acknowledged"],
            accepted_factor=event["accepted_factor"],
            created_at=event["created_at"],
        )
        if any(
            key not in indexed
            or fingerprint(indexed[key]) != expected
            or indexed[key].target_date in excluded
            for key, expected in event["evidence"].items()
        ):
            return state, {
                **base,
                "status": "reference_changed",
                "reasons": ["reference_changed"],
                "first_day": event["first_day"],
                "last_day": event["last_day"],
            }
        cases = [indexed[key] for key in event["case_ids"]]
    else:
        today = now.astimezone(ZoneInfo(timezone)).date()
        start = _timestamp(state["segment_start"]) if state["segment_start"] else None
        candidates = [
            record
            for record in records
            if record.horizon == "daily_previous_18"
            and record.configuration_id == configuration_id
            and record.timezone == timezone
            and today - timedelta(days=7) <= record.target_date < today
            and (start is None or record.start >= start)
        ]
        cases = sorted(candidates, key=lambda item: item.target_date)
        if (
            len(cases) != 7
            or {c.target_date for c in cases}
            != {today - timedelta(days=offset) for offset in range(1, 8)}
            or any(not _eligible(c, now, excluded) for c in cases)
            or len({_identity(c) for c in cases}) != 1
        ):
            return state, {
                **base,
                "status": "insufficient_days",
                "reasons": ["seven_complete_comparable_days_required"],
            }
    try:
        report, band = _comparison(
            [
                record
                for record in records
                if record.target_date not in excluded
                and (event is None or record.record_id in event["evidence"])
            ],
            cases,
            now,
        )
    except (ValueError, OverflowError):
        return state, {
            **base,
            "status": "basis_unavailable",
            "reasons": ["arithmetic_overflow"],
        }
    if event:
        report["status"] = "active" if report.get("detected") else "reference_changed"
    elif report.get("detected"):
        evidence = {
            key: fingerprint(indexed[key])
            for key in (*band["reference"]["record_ids"], *(c.record_id for c in cases))
        }
        event = {
            "id": hashlib.sha256(
                json.dumps(evidence, sort_keys=True).encode()
            ).hexdigest(),
            "configuration_id": configuration_id,
            "created_at": now.isoformat(),
            "first_day": report["first_day"],
            "last_day": report["last_day"],
            "target_id": cases[0].record_id,
            "case_ids": [case.record_id for case in cases],
            "evidence": evidence,
            "accepted_factor": accepted_factor,
            "acknowledged": False,
            "notified": False,
        }
        state["event"] = event
        validate_state(state)
        base.update(
            learning_paused=True,
            event_id=event["id"],
            acknowledged=False,
            accepted_factor=accepted_factor,
            created_at=event["created_at"],
        )
        report["status"] = "active"
    return state, {**base, **report}


def notification_due(
    state: dict[str, Any], now: datetime, timezone: str, *, enabled: bool
) -> bool:
    """Ein zusammengefasster Hinweis außerhalb der festen lokalen Ruhezeit."""

    event = state["event"]
    hour = now.astimezone(ZoneInfo(timezone)).hour
    return bool(
        enabled
        and event
        and not event["acknowledged"]
        and not event["notified"]
        and 8 <= hour < 22
    )
