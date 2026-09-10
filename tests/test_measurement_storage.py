"""Messsnapshots bleiben entkoppelt und übersetzen nur neue eingefrorene Werte."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from custom_components.pv_forecast.measurement_storage import MeasurementSnapshotCache
from custom_components.pv_forecast.measurements import (
    EnergyDelta,
    Reading,
    SourceConfig,
    SourceHistory,
)

START = datetime(2026, 9, 3, tzinfo=UTC)


def _history():
    history = SourceHistory(
        SourceConfig("s1", "sensor.pv", "total", "Gesamt", registry_id="r1"),
        "Europe/Berlin",
        50,
    )
    history.bind_location("original", "Europe/Berlin", START)
    for second in range(0, 300, 30):
        history.add_reading(START + timedelta(seconds=second), second / 1000, "kWh")
    return history


def test_snapshot_preserves_storage_contract_and_detaches_mutable_metadata():
    history = _history()
    cache = MeasurementSnapshotCache()
    snapshot = cache.serialize({"s1": history})
    expected = deepcopy({"sources": {"s1": history.to_dict()}})
    assert snapshot == expected
    history.mark_gap("upstream_gap")
    history.replace_source(
        replace(history.source, entity_id="sensor.new", registry_id="r2")
    )
    history.bind_location("moved", "UTC", START + timedelta(days=1))
    history.add_reading(START + timedelta(days=1), 5, "kWh")
    assert cache.serialize({"s1": history}) == {"sources": {"s1": history.to_dict()}}
    assert snapshot == expected


def test_snapshot_reuses_only_retained_immutable_items():
    history = _history()
    cache = MeasurementSnapshotCache()
    cache.prime(tuple(history.readings + history.deltas))
    with (
        patch.object(
            Reading, "to_dict", autospec=True, side_effect=Reading.to_dict
        ) as readings,
        patch.object(EnergyDelta, "to_dict", autospec=True) as deltas,
    ):
        cache.serialize({"s1": history})
        # Nur die kleine aktuelle Basiskopie wird noch durch to_dict übersetzt.
        assert readings.call_count == 1
        assert deltas.call_count == 0
    history.prune(START + timedelta(minutes=3), 20_000)
    cache.serialize({"s1": history})
    assert len(cache._items) == len(history.readings) + len(history.deltas)
    cache.serialize({})
    assert cache._items == {}
