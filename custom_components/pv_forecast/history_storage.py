"""Exakte Größe der nativen HA-Archivdatei aus unveränderten Recordbausteinen."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.helpers.json import prepare_save_json

from .history import HistoryArchive


class ArchiveStorage:
    """Die tatsächliche Einrückung und vollständige Storehülle mit begrenzen."""

    def __init__(self, key: str, version: int, minor_version: int) -> None:
        self._envelope = {
            "version": version,
            "minor_version": minor_version,
            "key": key,
        }
        self._record_sizes: dict[str, tuple[dict[str, Any], int]] = {}

    def _record_size(self, record: dict[str, Any]) -> int:
        previous = self._record_sizes.get(record["record_id"])
        if previous is not None and previous[0] is record:
            return previous[1]
        _, encoded = prepare_save_json(record)
        if isinstance(encoded, str):
            encoded = encoded.encode()
        # data.archive.records: Der Record beginnt auf Einrückungsebene vier.
        size = len(encoded) + 8 * (encoded.count(b"\n") + 1)
        self._record_sizes[record["record_id"]] = (record, size)
        return size

    def file_size(self, data: dict[str, Any]) -> int:
        """Native UTF-8-Bytes einschließlich Trennzeichen exakt addieren."""
        records = data["archive"]["records"]
        envelope = {
            **self._envelope,
            "data": {**data, "archive": {**data["archive"], "records": []}},
        }
        _, encoded = prepare_save_json(envelope)
        if isinstance(encoded, str):
            encoded = encoded.encode()
        return len(encoded) + (
            8
            + sum(self._record_size(record) for record in records)
            + 2 * (len(records) - 1)
            if records
            else 0
        )

    def snapshot(
        self,
        archive: HistoryArchive,
        last_fetched_at: datetime | None,
        now: datetime,
        *,
        max_records: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Vor jeder Schreibung beide bisherigen Grenzen und die Dateigröße prüfen."""
        archive.prune(now, max_records=max_records, max_bytes=max_bytes - 1024)
        data = {
            "archive": archive.storage_snapshot(),
            "last_fetched_at": last_fetched_at.isoformat() if last_fetched_at else None,
        }
        size = self.file_size(data)
        if size > max_bytes:
            remaining = len(archive.records)
            for record in sorted(
                archive.records.values(), key=lambda item: (item.end, item.horizon)
            ):
                if size <= max_bytes:
                    break
                size -= self._record_sizes[record.record_id][1] + (
                    2 if remaining > 1 else 8
                )
                remaining -= 1
            # Ein einziger weiterer Beschnitt erhält Reihenfolge, Revisionen
            # und Metadatenregeln, auch wenn viele Records weichen müssen.
            archive.prune(now, max_records=remaining, max_bytes=max_bytes - 1024)
            data["archive"] = archive.storage_snapshot()
            if self.file_size(data) > max_bytes:
                raise ValueError(
                    "Die Archivgrenze ist kleiner als die nötigen Metadaten"
                )
        self._record_sizes = {
            key: value
            for key, value in self._record_sizes.items()
            if key in archive.records
        }
        return data
