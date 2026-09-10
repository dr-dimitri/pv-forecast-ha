"""Entkoppelte Messsnapshots mit begrenzter Wiederverwendung eingefrorener Werte."""

from collections.abc import Mapping
from copy import copy
from typing import Any

from .measurements import EnergyDelta, Reading, SourceHistory


class MeasurementSnapshotCache:
    """Unveränderte Messpunkte genau einmal in ihre Speicherform übersetzen."""

    def __init__(self) -> None:
        self._items: dict[int, tuple[Reading | EnergyDelta, dict[str, Any]]] = {}

    def prime(self, items: tuple[Reading | EnergyDelta, ...]) -> None:
        """Vor Listenerstart ausschließlich eingefrorene Werte im Executor lesen."""
        self._items = {id(item): (item, item.to_dict()) for item in items}

    def serialize(self, histories: Mapping[str, SourceHistory]) -> dict[str, Any]:
        """Listen und Metadaten kopieren; gecachte Blattwerte nie mehr verändern."""
        previous = self._items
        retained: dict[int, tuple[Reading | EnergyDelta, dict[str, Any]]] = {}

        def encode(item: Reading | EnergyDelta) -> dict[str, Any]:
            key = id(item)
            cached = previous.get(key)
            if cached is None or cached[0] is not item:
                cached = (item, item.to_dict())
            retained[key] = cached
            return cached[1]

        sources: dict[str, Any] = {}
        for source_id, history in histories.items():
            # to_dict bleibt die einzige Definition des Speichervertrags. Die
            # kleinen veränderlichen Metadaten werden dort vollständig kopiert.
            metadata = copy(history)
            metadata.readings = []
            metadata.deltas = []
            data = metadata.to_dict()
            data["readings"] = [encode(item) for item in history.readings]
            data["deltas"] = [encode(item) for item in history.deltas]
            sources[source_id] = data
        # Ausgeschiedene Werte nicht über die nächste Schreibung hinaus halten.
        self._items = retained
        return {"sources": sources}
