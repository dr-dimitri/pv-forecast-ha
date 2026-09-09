"""Bewusst aufgerufener EMHASS-Datenadapter; liest JSON, schreibt JSON, kein HTTP."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from math import fsum, isfinite
from typing import Any


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Zeitpunkte benötigen einen UTC-Offset")
    return parsed.astimezone(UTC)


def convert(
    response: dict[str, Any],
    start: datetime,
    end: datetime,
    step_minutes: int,
    now: datetime,
) -> dict[str, Any]:
    """Intervallenergie energieerhaltend in explizite mittlere Watt umrechnen."""
    if any(value.utcoffset() is None for value in (start, end, now)):
        raise ValueError("Zeitpunkte benötigen einen UTC-Offset")
    start, end, now = (value.astimezone(UTC) for value in (start, end, now))
    if (
        isinstance(step_minutes, bool)
        or not isinstance(step_minutes, int)
        or not 1 <= step_minutes <= 60
        or end <= start
        or end - start > timedelta(hours=49)
    ):
        raise ValueError("Das gewünschte Planungsraster ist ungültig")
    step = timedelta(minutes=step_minutes)
    if (end - start) % step:
        raise ValueError("Der Zeitraum muss eine ganze Anzahl Schritte enthalten")
    if not isinstance(response, dict):
        raise ValueError("Die Prognoseantwort muss ein JSON-Objekt sein")
    if (
        type(response.get("schema_version")) is not int
        or response["schema_version"] != 1
        or response.get("last_update_success") is not True
        or not response.get("fetched_at")
        or not timedelta(0)
        <= now - timestamp(response["fetched_at"])
        <= timedelta(minutes=60)
    ):
        raise ValueError("Die Prognose ist veraltet oder ihr Vertrag unbekannt")
    raw_intervals = response.get("intervals")
    if not isinstance(raw_intervals, list):
        raise ValueError("Die Prognoseintervalle müssen eine JSON-Liste sein")
    intervals = []
    for item in raw_intervals:
        if not isinstance(item, dict):
            raise ValueError("Ein Prognoseintervall muss ein JSON-Objekt sein")
        left, right = timestamp(item["start"]), timestamp(item["end"])
        if right <= start or left >= end:
            continue
        energy = item["energy_kwh"]
        if (
            right <= left
            or item.get("is_complete") is not True
            or item.get("quality_flags")
            or isinstance(energy, bool)
            or not isinstance(energy, int | float)
            or not isfinite(energy)
            or energy < 0
        ):
            raise ValueError("Die Prognose enthält unbrauchbare Intervalle")
        intervals.append((left, right, energy))
    intervals.sort()
    values = {}
    cursor = start
    while cursor < end:
        limit, covered, energies = cursor + step, cursor, []
        for left, right, energy in intervals:
            a, b = max(cursor, left), min(limit, right)
            if b <= a:
                continue
            if a != covered:
                raise ValueError("Fehlende oder überlappende Prognoseabdeckung")
            energies.append(energy * ((b - a) / (right - left)))
            covered = b
        if covered != limit:
            raise ValueError("Das Planungsfenster ist nicht vollständig abgedeckt")
        power = fsum(energies) * (60_000 / step_minutes)
        if not isfinite(power):
            raise ValueError("Die mittlere Leistung ist nicht darstellbar")
        values[cursor.isoformat(sep=" ")] = power
        cursor = limit
    return {"pv_power_forecast": values, "prediction_horizon": len(values)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=timestamp)
    parser.add_argument("--end", required=True, type=timestamp)
    parser.add_argument("--step-minutes", type=int, default=30)
    args = parser.parse_args()
    try:
        result = convert(
            json.load(sys.stdin),
            args.start,
            args.end,
            args.step_minutes,
            datetime.now(UTC),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as err:
        parser.error(str(err))
    json.dump(result, sys.stdout, allow_nan=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
