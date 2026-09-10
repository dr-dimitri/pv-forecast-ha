"""Viele Messfenster indexiert mit denselben quelleneigenen Regeln auswerten."""

import asyncio
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from copy import copy
from datetime import datetime
from math import isfinite
from operator import attrgetter
from typing import Any

from .measurements import Reading, SourceHistory, aggregate_energy

type MeasurementWindow = tuple[datetime, datetime]


def _window_histories(
    history: SourceHistory, windows: Sequence[MeasurementWindow]
) -> list[SourceHistory]:
    """Kleine Lesesichten bilden; Metadaten und Tageskorrekturen bleiben erhalten."""

    readings = sorted(history.readings, key=attrgetter("timestamp"))
    deltas = sorted(history.deltas, key=attrgetter("start"))
    # Aktueller Status gilt auch für vergangene Fenster. Bei einem Wechsel von
    # Energie zu Leistung erhalten zusätzliche Randpunkte historische Energie-
    # segmente für die zentrale Auswahl in aggregate_energy.
    sentinels = [history.latest_reading, history.last_valid_reading]
    if history.source.kind == "power":
        energy_segments = {
            key
            for key, source in history.segment_sources.items()
            if source.kind != "power"
        }
        bounds: dict[str, tuple[Reading, Reading]] = {}
        for reading in readings:
            if (
                reading.segment_id in energy_segments
                and "late_reading" not in reading.quality_flags
            ):
                first, _ = bounds.get(reading.segment_id, (reading, reading))
                bounds[reading.segment_id] = first, reading
        sentinels.extend(reading for pair in bounds.values() for reading in pair)

    result = []
    for start, end in windows:
        view = copy(history)
        first = bisect_left(readings, start, key=attrgetter("timestamp"))
        last = bisect_right(readings, end, key=attrgetter("timestamp"))
        selected = readings[first:last]
        selected_ids = {id(reading) for reading in selected}
        selected.extend(
            reading
            for reading in sentinels
            if reading is not None and id(reading) not in selected_ids
        )
        view.readings = selected
        # Gültige Differenzen sind chronologisch und überschneiden sich nicht.
        first_delta = bisect_right(deltas, start, key=attrgetter("end"))
        last_delta = bisect_left(deltas, end, key=attrgetter("start"))
        view.deltas = deltas[first_delta:last_delta]
        result.append(view)
    return result


async def async_interval_windows(
    histories: Sequence[SourceHistory],
    windows: Sequence[MeasurementWindow],
    now: datetime,
) -> list[dict[str, Any]]:
    """Je Quelle einmal indexieren und zwischen Quellen den Eventloop freigeben."""

    views: list[list[SourceHistory]] = [[] for _ in windows]
    snapshots: list[dict[str, dict[str, Any]]] = [{} for _ in windows]
    for history in histories:
        await asyncio.sleep(0)
        for index, (view, (start, end)) in enumerate(
            zip(_window_histories(history, windows), windows, strict=True)
        ):
            views[index].append(view)
            snapshots[index][view.source.source_id] = view.snapshot(start, end, now)

    result = []
    for index, (start, end) in enumerate(windows):
        total = aggregate_energy(
            views[index], start, end, now, cached_snapshots=snapshots[index]
        )
        complete = total["energy_complete"] and end <= now
        energy = total["energy_kwh"] if complete else None
        flags = set(total["quality_flags"])
        if end > now:
            flags.add("future_window")
        power = (
            energy / ((end - start).total_seconds() / 3600)
            if energy is not None
            else None
        )
        if power is not None and not isfinite(power):
            power = None
            flags.add("arithmetic_overflow")
        result.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "energy_kwh": energy,
                # Belegte Teilsummen bleiben sichtbar, ohne Randdifferenzen
                # aufzuteilen oder eine vollständige Stunde zu behaupten.
                "observed_energy_kwh": total["energy_kwh"] if end <= now else None,
                "ac_power_kw": power,
                "energy_complete": complete,
                "quality_flags": sorted(flags),
                "source_count": total["source_count"],
            }
        )
    return result
