"""Messkurven verwenden echte Differenzen und bleiben bei vielen Fenstern begrenzt."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.pv_forecast.measurement_windows import async_interval_windows
from custom_components.pv_forecast.measurements import (
    EnergyDelta,
    Reading,
    SourceConfig,
    SourceHistory,
    aggregate_energy,
)

START = datetime(2026, 9, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _history(source_id="a", **changes):
    """Eine ausdrücklich bestätigte, unabhängige AC-Messquelle anlegen."""

    source = SourceConfig(source_id, f"sensor.{source_id}", "total", "AC-PV")
    return SourceHistory(replace(source, **changes), "Europe/Berlin", 20)


async def _assert_matches(histories, windows, now):
    """Die bestehende Einzelfensterauswertung bleibt der fachliche Vergleich."""

    result = await async_interval_windows(histories, windows, now)
    for actual, (start, end) in zip(result, windows, strict=True):
        expected = aggregate_energy(histories, start, end, now)
        complete = expected["energy_complete"] and end <= now
        assert actual["energy_complete"] is complete
        assert actual["energy_kwh"] == (expected["energy_kwh"] if complete else None)
        assert actual["source_count"] == expected["source_count"]
        assert set(actual["quality_flags"]) == set(expected["quality_flags"]) | (
            {"future_window"} if end > now else set()
        )
    return result


async def test_multiple_counters_zero_gap_boundary_and_future():
    """Nur ganz belegte Energiemengen erscheinen als Messbalken, auch bei null."""

    first, second = _history(), _history("b")
    for history in (first, second):
        history.add_reading(START, 0, "kWh")
        history.add_reading(START + HOUR, 0, "kWh")
        history.add_reading(START + HOUR * 2, "unknown", "kWh")
        history.add_reading(START + HOUR * 3, 4, "kWh")
    windows = [(START + index * HOUR, START + (index + 1) * HOUR) for index in range(4)]
    result = await _assert_matches((first, second), windows, START + HOUR * 3)
    assert [item["energy_kwh"] for item in result] == [0, None, None, None]
    assert result[0]["ac_power_kw"] == 0
    assert result[0]["source_count"] == 2
    coarse = await _assert_matches(
        (first, second), [(START + HOUR, START + HOUR * 3)], START + HOUR * 3
    )
    assert coarse[0]["energy_kwh"] == 8
    assert coarse[0]["ac_power_kw"] == 4


@pytest.mark.parametrize("kind", ["total", "daily"])
async def test_reset_correction_and_source_change_match_shared_rules(kind):
    """Resets, Tageskorrekturen und Identitätssegmente behalten ihre Bedeutung."""

    history = _history(kind=kind)
    for offset, value in [(0, 2), (1, 3), (2, 1), (3, 2)]:
        history.add_reading(START + offset * HOUR, value, "kWh")
    history.replace_source(replace(history.source, registry_id="replacement"))
    history.add_reading(START + HOUR * 4, 100, "kWh")
    history.add_reading(START + HOUR * 5, 102, "kWh")
    windows = [(START + index * HOUR, START + (index + 1) * HOUR) for index in range(5)]
    await _assert_matches((history,), windows, START + HOUR * 7)
    await _assert_matches((history,), [(START, START + HOUR * 5)], START + HOUR * 7)


async def test_power_change_preserves_historical_energy_and_missing_bounds():
    """Die aktuelle Leistungsart entfernt keine frühere Energiequelle aus der Summe."""

    history = _history()
    history.add_reading(START, "unknown", "kWh")
    history.add_reading(START + HOUR * 3, "unknown", "kWh")
    history.add_reading(START + HOUR * 4, 1, "kWh")
    history.add_reading(START + HOUR * 5, 2, "kWh")
    history.replace_source(replace(history.source, kind="power"))
    history.add_reading(START + HOUR * 6, 4, "kW")
    windows = [(START + index * HOUR, START + (index + 1) * HOUR) for index in range(7)]
    result = await _assert_matches((history,), windows, START + HOUR * 7)
    assert result[1]["source_count"] == 1
    assert result[4]["energy_kwh"] == 1
    assert result[6]["source_count"] == 0


async def test_late_readings_and_disordered_windows_keep_absolute_intervals():
    """Verspätung und Fensterreihenfolge ändern keine UTC-Zuordnung."""

    history = _history()
    history.add_reading(START, 0, "kWh")
    history.add_reading(START + HOUR, 2, "kWh")
    history.add_reading(START + HOUR * 2, 4, "kWh")
    history.add_reading(START + HOUR / 2, 1, "kWh")
    windows = [(START + HOUR, START + HOUR * 2), (START, START + HOUR / 2)]
    await _assert_matches((history,), windows, START + HOUR * 2)


async def test_many_windows_do_not_scan_all_readings_repeatedly_and_yield():
    """50 Fenster über 20.000 Punkte scannen nur ihre kleinen indizierten Sichten."""

    history = _history()
    step = timedelta(seconds=8)
    count = 20_000
    history.readings = [
        Reading(START + index * step, index / 1000, segment_id=history.segment_id)
        for index in range(count)
    ]
    history.deltas = [
        EnergyDelta(
            START + index * step,
            START + (index + 1) * step,
            0.001,
            frozenset(),
            history.segment_id,
        )
        for index in range(count - 1)
    ]
    windows = [
        (START + index * HOUR, START + (index + 1) * HOUR) for index in range(48)
    ]
    scans = []
    original = SourceHistory.snapshot

    def snapshot(view, start, end, now):
        scans.append(len(view.readings) + len(view.deltas))
        return original(view, start, end, now)

    with patch.object(SourceHistory, "snapshot", new=snapshot):
        task = asyncio.create_task(
            async_interval_windows((history,), windows, START + 48 * HOUR)
        )
        await asyncio.sleep(0)
        assert not task.done()
        await task
    assert len(scans) == 48
    assert sum(scans) < count * 3
    assert len(history.readings) == count
    assert len(history.deltas) == count - 1


async def test_no_sources_is_missing_instead_of_zero():
    """Ohne Energiemessquelle bleibt auch ein vollständig vergangenes Fenster leer."""

    result = await _assert_matches((), [(START, START + HOUR)], START + HOUR)
    assert result[0]["energy_kwh"] is None
    assert result[0]["ac_power_kw"] is None


async def test_finite_energy_does_not_emit_infinite_aggregate_power():
    """Zwei endliche Einzelleistungen dürfen keine unzulässige JSON-Infinity ergeben."""

    histories = [_history("a"), _history("b")]
    for history in histories:
        history.max_power_kw = 1e308
        history.add_reading(START, 0, "kWh")
        history.add_reading(START + HOUR / 2, 4.5e307, "kWh")
    result = await async_interval_windows(
        histories, [(START, START + HOUR / 2)], START + HOUR
    )
    assert result[0]["energy_kwh"] == 9e307
    assert result[0]["ac_power_kw"] is None
    assert "arithmetic_overflow" in result[0]["quality_flags"]
    json.dumps(result, allow_nan=False)
