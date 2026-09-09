"""Reine Messwertauswertung ohne Gerätezugriff oder Stundeninterpolation."""

from collections.abc import Iterable, Mapping
from copy import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import fsum, isfinite
from typing import Any, Literal, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

type MeasurementKind = Literal["total", "daily", "power"]


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


def _utc(value: datetime) -> datetime:
    """Naive Zeitpunkte sind keine eindeutigen Messzeitpunkte."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Ein Messzeitpunkt muss eine Zeitzone enthalten")
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _stored_flags(value: object) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Ungültige gespeicherte Qualitätsmarkierungen")
    return frozenset(value)


def _stored_segment(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Ungültige gespeicherte Segment-ID")
    return value


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Bestätigte AC-Quelle mit namensunabhängiger Zuordnungs-ID."""

    source_id: str
    entity_id: str
    kind: MeasurementKind
    scope: str
    registry_id: str | None = None
    derived_energy: bool = False
    max_interval_minutes: float = 60
    confirmed_pv: bool = True
    confirmed_disjoint: bool = True
    upstream_entity_id: str | None = None
    upstream_registry_id: str | None = None
    helper_entry_id: str | None = None

    def __post_init__(self) -> None:
        upstream = (
            self.upstream_entity_id,
            self.upstream_registry_id,
            self.helper_entry_id,
        )
        if any(value is not None for value in upstream) and (
            not all(isinstance(value, str) and value for value in upstream)
            or not str(self.upstream_entity_id).startswith("sensor.")
            or self.kind != "total"
            or not self.derived_energy
        ):
            raise ValueError("Die Herkunft des Energiehelfers ist unvollständig")
        if (
            not isinstance(self.source_id, str)
            or not self.source_id.strip()
            or not isinstance(self.entity_id, str)
            or not self.entity_id.startswith("sensor.")
            or len(self.entity_id) == 7
            or self.kind not in ("total", "daily", "power")
            or not isinstance(self.scope, str)
            or not self.scope.strip()
            or (
                self.registry_id is not None
                and (
                    not isinstance(self.registry_id, str)
                    or not self.registry_id.strip()
                )
            )
            or not isinstance(self.derived_energy, bool)
            or self.confirmed_pv is not True
            or self.confirmed_disjoint is not True
            or not _finite_number(self.max_interval_minutes)
            or self.max_interval_minutes <= 0
        ):
            raise ValueError("Die Messquelle ist nicht vollständig bestätigt")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Optionen ohne stillschweigende Einheiten- oder Typumdeutung lesen."""
        try:
            return cls(
                source_id=data["source_id"],
                entity_id=data["entity_id"],
                kind=data["kind"],
                scope=data["scope"],
                registry_id=data.get("registry_id"),
                derived_energy=data.get("derived_energy", False),
                max_interval_minutes=data.get("max_interval_minutes", 60),
                confirmed_pv=data.get("confirmed_pv", False),
                confirmed_disjoint=data.get("confirmed_disjoint", False),
                upstream_entity_id=data.get("upstream_entity_id"),
                upstream_registry_id=data.get("upstream_registry_id"),
                helper_entry_id=data.get("helper_entry_id"),
            )
        except (KeyError, TypeError) as err:
            raise ValueError("Die Messquelle ist unvollständig") from err

    def to_dict(self) -> dict[str, Any]:
        """Die separate Messkonfiguration verlustfrei serialisieren."""
        return {
            "source_id": self.source_id,
            "entity_id": self.entity_id,
            "registry_id": self.registry_id,
            "kind": self.kind,
            "scope": self.scope,
            "derived_energy": self.derived_energy,
            "max_interval_minutes": self.max_interval_minutes,
            "confirmed_pv": self.confirmed_pv,
            "confirmed_disjoint": self.confirmed_disjoint,
            **(
                {
                    "upstream_entity_id": self.upstream_entity_id,
                    "upstream_registry_id": self.upstream_registry_id,
                    "helper_entry_id": self.helper_entry_id,
                }
                if self.upstream_registry_id is not None
                else {}
            ),
        }

    @property
    def measurement_identity(self) -> tuple[str, str, str, bool]:
        """Eine registrierte Entity behält beim Umbenennen ihre Identität."""
        identity = (
            f"registry:{self.registry_id}"
            if self.registry_id is not None
            else f"entity:{self.entity_id}"
        )
        if self.upstream_registry_id is not None:
            identity += (
                f"/power:{self.upstream_registry_id}/helper:{self.helper_entry_id}"
            )
        return identity, self.kind, self.scope, self.derived_energy


def normalize_reading_value(
    raw_value: object, unit: str | None, kind: MeasurementKind
) -> tuple[float | None, frozenset[str]]:
    """Nur ausdrücklich passende Einheiten in kWh beziehungsweise kW umrechnen."""
    if raw_value is None or raw_value == "unknown":
        return None, frozenset({"unknown"})
    if raw_value == "unavailable":
        return None, frozenset({"unavailable"})
    units = {"W": 0.001, "kW": 1} if kind == "power" else {"Wh": 0.001, "kWh": 1}
    if not isinstance(unit, str) or unit not in units:
        return None, frozenset({"unsupported_unit"})
    if isinstance(raw_value, bool) or not isinstance(raw_value, str | int | float):
        return None, frozenset({"invalid_value"})
    try:
        value = float(raw_value) * units[unit]
    except (ValueError, OverflowError):
        return None, frozenset({"invalid_value"})
    if not isfinite(value) or value < 0:
        return None, frozenset({"invalid_value"})
    return value, frozenset()


@dataclass(frozen=True, slots=True)
class Reading:
    """Normalisierter Messpunkt; null ist ausdrücklich kein Nullertrag."""

    timestamp: datetime
    value: float | None
    quality_flags: frozenset[str] = frozenset()
    last_reset: datetime | None = None
    segment_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "value": self.value,
            "quality_flags": sorted(self.quality_flags),
            "last_reset": self.last_reset.isoformat() if self.last_reset else None,
            "segment_id": self.segment_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        value = data["value"]
        if value is not None and (not _finite_number(value) or value < 0):
            raise ValueError("Ungültiger gespeicherter Messwert")
        return cls(
            _parse_time(data["timestamp"]),
            value,
            _stored_flags(data["quality_flags"]),
            _parse_time(data["last_reset"]) if data.get("last_reset") else None,
            _stored_segment(data["segment_id"]),
        )


@dataclass(frozen=True, slots=True)
class EnergyDelta:
    """Eine belegte Zählerdifferenz darf nicht auf Unterstunden verteilt werden."""

    start: datetime
    end: datetime
    energy_kwh: float
    quality_flags: frozenset[str]
    segment_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "energy_kwh": self.energy_kwh,
            "quality_flags": sorted(self.quality_flags),
            "segment_id": self.segment_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        start, end = _parse_time(data["start"]), _parse_time(data["end"])
        value = data["energy_kwh"]
        if end <= start or not _finite_number(value) or value < 0:
            raise ValueError("Ungültige gespeicherte Zählerdifferenz")
        return cls(
            start,
            end,
            value,
            _stored_flags(data["quality_flags"]),
            _stored_segment(data["segment_id"]),
        )


class SourceHistory:
    """Eine begrenzbare Erfassung mit getrennten Identitätssegmenten."""

    def __init__(
        self,
        source: SourceConfig,
        timezone: str,
        max_power_kw: float,
        segment_id: str | None = None,
    ) -> None:
        if not _finite_number(max_power_kw) or max_power_kw <= 0:
            raise ValueError("Die Sprungprüfung benötigt eine endliche Leistung")
        self.source = source
        self.timezone = ZoneInfo(timezone)
        self.max_power_kw = max_power_kw
        self.segment_id = segment_id or uuid4().hex
        self.readings: list[Reading] = []
        self.deltas: list[EnergyDelta] = []
        self._segments = {self.segment_id: source.to_dict()}
        self._segment_contexts: dict[str, dict[str, Any]] = {
            self.segment_id: {
                "location_id": None,
                "timezone": timezone,
                "started_at": None,
            }
        }
        self._baseline: Reading | None = None
        self._pending_gap: set[str] = set()
        self._invalid_days: set[tuple[str, str]] = set()

    @property
    def latest_reading(self) -> Reading | None:
        """Verspätete Zustellungen überschreiben nicht den jüngsten Zustand."""
        return max(
            (r for r in self.readings if r.segment_id == self.segment_id),
            key=lambda reading: reading.timestamp,
            default=None,
        )

    @property
    def segment_sources(self) -> dict[str, SourceConfig]:
        """Historische Messgrenzen auch für die Leserechte zugänglich machen."""
        return {
            key: SourceConfig.from_dict(value) for key, value in self._segments.items()
        }

    @property
    def last_valid_reading(self) -> Reading | None:
        return max(
            (
                reading
                for reading in self.readings
                if reading.value is not None
                and not reading.quality_flags.intersection(
                    {
                        "late_reading",
                        "implausible_jump",
                        "arithmetic_overflow",
                        "invalid_last_reset",
                        "counter_decrease",
                        "daily_correction",
                        "invalid_metadata",
                        "restored_state",
                    }
                )
                and reading.segment_id == self.segment_id
            ),
            key=lambda reading: reading.timestamp,
            default=None,
        )

    def mark_gap(self, flag: str = "restart") -> None:
        """Ein unbeobachteter Zeitraum bleibt auch nach Neustart erkennbar."""
        self._pending_gap.add(flag)

    def bind_location(
        self, location_id: str, timezone: str, started_at: datetime
    ) -> None:
        """Eine neue physische Lage beginnt ohne Zählerbasis des früheren Standorts."""

        current = self._segment_contexts[self.segment_id]
        if current["location_id"] is None:
            for context in self._segment_contexts.values():
                context["location_id"] = location_id
        elif current["location_id"] != location_id:
            self._start_segment("location_changed")
            self._segment_contexts[self.segment_id] = {
                "location_id": location_id,
                "timezone": timezone,
                "started_at": _utc(started_at).isoformat(),
            }
        self.timezone = ZoneInfo(timezone)

    def accepts_timestamp(self, timestamp: datetime) -> bool:
        """Ein Zustand vor dem Standortwechsel ist keine neue Messbasis."""

        start = self._segment_contexts[self.segment_id]["started_at"]
        return start is None or _utc(timestamp) >= _parse_time(start)

    def current_location_view(self) -> Self:
        """Für aktuelle Planungsanzeigen nur Messungen dieses Standorts lesen."""

        active_context = self._segment_contexts[self.segment_id]
        segments = {
            key
            for key, context in self._segment_contexts.items()
            if context == active_context
        }
        view = copy(self)
        view.readings = [
            reading for reading in self.readings if reading.segment_id in segments
        ]
        view.deltas = [delta for delta in self.deltas if delta.segment_id in segments]
        return view

    def _start_segment(self, reason: str) -> None:
        context = self._segment_contexts[self.segment_id].copy()
        self.segment_id = uuid4().hex
        self._segment_contexts[self.segment_id] = context
        self._segments[self.segment_id] = self.source.to_dict()
        self._baseline = None
        self._pending_gap = {reason}

    def replace_source(self, source: SourceConfig) -> None:
        """Eine andere Messgrenze oder Sensoridentität beginnt ein neues Segment."""
        if source.source_id != self.source.source_id:
            raise ValueError("Eine Historie gehört genau einer Zuordnungs-ID")
        if source.measurement_identity != self.source.measurement_identity:
            self._start_segment("source_changed")
        self.source = source
        self._segments[self.segment_id] = source.to_dict()

    def _local_day(self, timestamp: datetime, segment_id: str | None = None) -> str:
        context = self._segment_contexts[segment_id or self.segment_id]
        return timestamp.astimezone(ZoneInfo(context["timezone"])).date().isoformat()

    def _append_delta(
        self, start: datetime, end: datetime, energy: float, flags: set[str]
    ) -> bool:
        duration = (end - start).total_seconds()
        if duration <= 0:
            return energy == 0
        # Die Division vermeidet Überläufe durch Leistung mal große Zeitspannen.
        mean_power = energy / (duration / 3600)
        if not isfinite(energy) or not isfinite(mean_power):
            flags.add("arithmetic_overflow")
            return False
        if mean_power > self.max_power_kw:
            flags.add("implausible_jump")
            return False
        if duration / 60 > self.source.max_interval_minutes:
            flags.update({"gap", "stale_gap"})
        if self._pending_gap:
            flags.update(self._pending_gap)
            flags.add("gap")
        self.deltas.append(
            EnergyDelta(start, end, energy, frozenset(flags), self.segment_id)
        )
        return True

    def add_reading(
        self,
        timestamp: datetime,
        raw_value: object,
        unit: str | None,
        last_reset: datetime | None = None,
        *,
        quality_flags: Iterable[str] = (),
    ) -> Reading:
        """Erst je Quelle differenzieren; niemals einen Summenzähler vortäuschen."""
        timestamp = _utc(timestamp)
        value, normalization_flags = normalize_reading_value(
            raw_value, unit, self.source.kind
        )
        flags = set(normalization_flags)
        flags.update(quality_flags)
        if self.source.derived_energy:
            flags.add("derived_energy")
        if last_reset is not None:
            last_reset = _utc(last_reset)
            if last_reset > timestamp:
                flags.add("invalid_last_reset")
                last_reset = None
        reading = Reading(
            timestamp, value, frozenset(flags), last_reset, self.segment_id
        )
        if not self.accepts_timestamp(timestamp):
            return replace(
                reading,
                quality_flags=reading.quality_flags | {"before_location_change"},
            )
        latest = max(self.readings, key=lambda item: item.timestamp, default=None)
        if latest is not None and timestamp <= latest.timestamp:
            if (
                timestamp == latest.timestamp
                and value == latest.value
                and last_reset == latest.last_reset
                and latest.segment_id == self.segment_id
            ):
                return latest
            if timestamp < latest.timestamp or latest.segment_id == self.segment_id:
                reading = replace(
                    reading, quality_flags=reading.quality_flags | {"late_reading"}
                )
                self.readings.append(reading)
                return reading
        if value is None:
            self._pending_gap.update(flags)
            self.readings.append(reading)
            return reading
        previous = self._baseline
        if previous is None:
            flags.update(self._pending_gap)
        if self.source.kind == "power":
            if self._pending_gap:
                flags.update(self._pending_gap)
                flags.add("gap")
            if value > self.max_power_kw:
                flags.add("implausible_jump")
        elif previous is not None:
            assert previous.value is not None
            day_changed = self._local_day(previous.timestamp) != self._local_day(
                timestamp
            )
            confirmed_reset = (
                last_reset is not None
                and last_reset != previous.last_reset
                and previous.timestamp <= last_reset <= timestamp
            )
            if confirmed_reset:
                flags.add("counter_reset")
                if self.source.kind == "daily":
                    flags.add("daily_reset")
                self._append_delta(last_reset, timestamp, value, flags)
            elif self.source.kind == "daily" and day_changed:
                # Ein neuer Kalendertag belegt den fehlenden Vortagsabschluss nicht.
                local = timestamp.astimezone(self.timezone)
                if (
                    local.hour == local.minute == local.second == local.microsecond == 0
                    and value == 0
                ):
                    flags.add("daily_reset")
                else:
                    flags.add("daily_reset_unconfirmed")
            elif value < previous.value:
                flags.add("counter_decrease")
                if self.source.kind == "daily":
                    flags.add("daily_correction")
                    self._invalid_days.add(
                        (self.segment_id, self._local_day(timestamp))
                    )
                else:
                    # Ohne belegten Reset könnte ein Zählerwechsel vorliegen.
                    self._start_segment("counter_decrease")
            else:
                invalid_day = (
                    self.segment_id,
                    self._local_day(timestamp),
                ) in self._invalid_days
                if invalid_day:
                    flags.add("daily_correction")
                else:
                    self._append_delta(
                        previous.timestamp, timestamp, value - previous.value, flags
                    )
        reading = replace(
            reading, quality_flags=frozenset(flags), segment_id=self.segment_id
        )
        self.readings.append(reading)
        self._baseline = reading
        self._pending_gap.clear()
        return reading

    def prune(self, cutoff: datetime, max_readings: int = 20000) -> None:
        """Werte und Deltas gemeinsam begrenzen; alte Tageskorrekturen verwerfen."""
        cutoff = _utc(cutoff)
        if max_readings < 1:
            raise ValueError("Mindestens ein Messwert muss speicherbar bleiben")
        self.readings = sorted(
            (r for r in self.readings if r.timestamp >= cutoff),
            key=lambda reading: reading.timestamp,
        )[-max_readings:]
        if self.readings:
            cutoff = max(cutoff, min(r.timestamp for r in self.readings))
        self.deltas = [d for d in self.deltas if d.start >= cutoff][-max_readings:]
        if self._baseline is not None and self._baseline not in self.readings:
            self._baseline = None
            self.mark_gap("retention_gap")
        self._invalid_days = {
            item
            for item in self._invalid_days
            if item[1] >= self._local_day(cutoff, item[0])
        }
        used = {r.segment_id for r in self.readings} | {
            d.segment_id for d in self.deltas
        }
        used.add(self.segment_id)
        self._invalid_days = {item for item in self._invalid_days if item[0] in used}
        self._segments = {
            key: value for key, value in self._segments.items() if key in used
        }
        self._segment_contexts = {
            key: value for key, value in self._segment_contexts.items() if key in used
        }

    def snapshot(self, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
        """Belegte Differenzen und exakte Nullanteile ohne Energieschätzung liefern."""
        start, end, now = _utc(start), _utc(end), _utc(now)
        if end <= start:
            raise ValueError("Das Messfenster muss eine positive Dauer haben")
        flags: set[str] = set()
        overlapping = [d for d in self.deltas if d.start < end and d.end > start]
        relevant_segments = {delta.segment_id for delta in overlapping}
        if not relevant_segments:
            relevant_segments = {
                reading.segment_id
                for reading in self.readings
                if start <= reading.timestamp < end
            }
        invalid_days = {
            item
            for item in self._invalid_days
            if item[0] in relevant_segments
            and item[1] >= self._local_day(start, item[0])
            and item[1] <= self._local_day(end - timedelta(microseconds=1), item[0])
        }
        if invalid_days:
            flags.add("daily_correction")
        if any(
            {"derived_energy", "gap"} <= delta.quality_flags for delta in overlapping
        ):
            # Ein Leistungsintegral belegt die über eine Lücke angenäherte
            # Energie nicht; ein echter fortlaufender Zähler kann dies hingegen.
            flags.add("derived_measurement_gap")
        selected = []
        for delta in overlapping:
            left, right = max(start, delta.start), min(end, delta.end)
            if {"derived_energy", "gap"} <= delta.quality_flags or (
                delta.segment_id,
                self._local_day(left, delta.segment_id),
            ) in invalid_days:
                continue
            if left != delta.start or right != delta.end:
                # Ein gesundes unverändertes Zählerintervall belegt auch in
                # jedem Teilfenster exakt null. Positive Energie bleibt ungeteilt.
                if delta.energy_kwh != 0 or delta.quality_flags - {"derived_energy"}:
                    flags.add("boundary_gap")
                    continue
                delta = replace(delta, start=left, end=right)
            selected.append(delta)
        for reading in self.readings:
            if (
                start <= reading.timestamp <= end
                and reading.segment_id in relevant_segments
            ):
                flags.update(reading.quality_flags)
        for delta in selected:
            flags.update(delta.quality_flags)
        latest_valid = self.last_valid_reading
        if latest_valid is None:
            flags.add("no_valid_reading")
        elif (
            now - latest_valid.timestamp
        ).total_seconds() / 60 > self.source.max_interval_minutes:
            flags.add("stale")
        duration = (end - start).total_seconds()
        exact_seconds = _covered_seconds(selected)
        covered_seconds = _covered_seconds(
            d for d in selected if "gap" not in d.quality_flags
        )
        try:
            energy = fsum(d.energy_kwh for d in selected) if selected else None
        except OverflowError:
            energy = None
            flags.add("arithmetic_overflow")
        if energy is not None and not isfinite(energy):
            energy = None
            flags.add("arithmetic_overflow")
        segments = {d.segment_id for d in selected}
        if len(segments) > 1:
            flags.add("multiple_segments")
        energy_complete = (
            energy is not None
            and exact_seconds == duration
            and not invalid_days
            and len(segments) == 1
        )
        complete = energy_complete and covered_seconds == duration
        if not complete and self.source.kind != "power":
            flags.add("incomplete")
        latest = self.latest_reading
        return {
            "source_id": self.source.source_id,
            "entity_id": self.source.entity_id,
            "registry_id": self.source.registry_id,
            "kind": self.source.kind,
            "scope": self.source.scope,
            "derived_energy": self.source.derived_energy,
            "segment_id": self.segment_id,
            "segments": {key: value.copy() for key, value in self._segments.items()},
            "segment_contexts": {
                key: value.copy() for key, value in self._segment_contexts.items()
            },
            "latest_reading": latest.to_dict() if latest else None,
            "last_valid_reading": latest_valid.to_dict() if latest_valid else None,
            "energy_kwh": energy,
            "energy_complete": energy_complete,
            "complete": complete,
            "coverage_seconds": covered_seconds,
            "quality_flags": sorted(flags),
            "deltas": [delta.to_dict() for delta in selected],
            "readings": [
                reading.to_dict()
                for reading in self.readings
                if start <= reading.timestamp < end
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        """Interne Historie unabhängig vom Config-Entry-Schema speichern."""
        return {
            "source": self.source.to_dict(),
            "segment_id": self.segment_id,
            "segments": {key: value.copy() for key, value in self._segments.items()},
            "segment_contexts": {
                key: value.copy() for key, value in self._segment_contexts.items()
            },
            "readings": [reading.to_dict() for reading in self.readings],
            "deltas": [delta.to_dict() for delta in self.deltas],
            "baseline": self._baseline.to_dict() if self._baseline else None,
            "pending_gap": sorted(self._pending_gap),
            "invalid_days": [list(item) for item in sorted(self._invalid_days)],
        }

    @classmethod
    def from_dict(
        cls,
        source: SourceConfig,
        data: Mapping[str, Any],
        timezone: str,
        max_power_kw: float,
    ) -> Self:
        """Nach Neustart keine Messidentität oder Einheit still umdeuten."""
        try:
            previous_source = SourceConfig.from_dict(data["source"])
            segment = _stored_segment(data["segment_id"])
            history = cls(previous_source, timezone, max_power_kw, segment)
            segments = {
                _stored_segment(key): SourceConfig.from_dict(value)
                for key, value in data["segments"].items()
            }
            if (
                segment not in segments
                or segments[segment] != previous_source
                or any(item.source_id != source.source_id for item in segments.values())
                or not isinstance(data["readings"], list)
                or not isinstance(data["deltas"], list)
            ):
                raise ValueError("Inkonsistente gespeicherte Messsegmente")
            history._segments = {key: item.to_dict() for key, item in segments.items()}
            contexts = data.get("segment_contexts")
            if contexts is None:
                contexts = {
                    key: {"location_id": None, "timezone": timezone, "started_at": None}
                    for key in segments
                }
            if not isinstance(contexts, dict) or set(contexts) != set(segments):
                raise ValueError("Die Standortkontexte der Messsegmente fehlen")
            for key, context in contexts.items():
                if not isinstance(context, dict) or (
                    context.get("location_id") is not None
                    and (
                        not isinstance(context["location_id"], str)
                        or not context["location_id"]
                    )
                ):
                    raise ValueError("Ein Messsegment hat keinen gültigen Standort")
                ZoneInfo(context["timezone"])
                if context["started_at"] is not None:
                    _parse_time(context["started_at"])
                history._segment_contexts[key] = dict(context)
            history.timezone = ZoneInfo(history._segment_contexts[segment]["timezone"])
            history.readings = [Reading.from_dict(item) for item in data["readings"]]
            history.deltas = [EnergyDelta.from_dict(item) for item in data["deltas"]]
            last_accepted: datetime | None = None
            seen: set[tuple[str, datetime]] = set()
            for reading in history.readings:
                beginning = history._segment_contexts[reading.segment_id]["started_at"]
                if beginning is not None and reading.timestamp < _parse_time(beginning):
                    raise ValueError("Ein Messpunkt liegt vor seinem Standortsegment")
                if reading.segment_id not in segments or (
                    reading.last_reset is not None
                    and reading.last_reset > reading.timestamp
                ):
                    raise ValueError("Messpunkt verweist auf ungültiges Segment")
                if "late_reading" not in reading.quality_flags:
                    key = reading.segment_id, reading.timestamp
                    if key in seen or (
                        last_accepted is not None and reading.timestamp < last_accepted
                    ):
                        raise ValueError(
                            "Gespeicherte Messpunkte sind nicht chronologisch"
                        )
                    seen.add(key)
                    last_accepted = reading.timestamp
            previous_end: datetime | None = None
            for delta in history.deltas:
                if delta.segment_id not in segments or (
                    previous_end is not None and delta.start < previous_end
                ):
                    raise ValueError("Gespeicherte Zählerdifferenzen überlappen")
                if segments[delta.segment_id].kind == "power":
                    raise ValueError(
                        "Leistungsquellen enthalten keine Energiedifferenzen"
                    )
                previous_end = delta.end
            history._baseline = (
                Reading.from_dict(data["baseline"]) if data.get("baseline") else None
            )
            if history._baseline is not None and (
                history._baseline not in history.readings
                or history._baseline.segment_id != segment
                or history._baseline.value is None
                or "late_reading" in history._baseline.quality_flags
            ):
                raise ValueError("Die gespeicherte Zählerbasis ist inkonsistent")
            expected_baseline = max(
                (
                    reading
                    for reading in history.readings
                    if reading.segment_id == segment
                    and reading.value is not None
                    and "late_reading" not in reading.quality_flags
                ),
                key=lambda reading: reading.timestamp,
                default=None,
            )
            if history._baseline != expected_baseline:
                raise ValueError("Die gespeicherte Zählerbasis ist nicht aktuell")
            history._pending_gap = set(_stored_flags(data.get("pending_gap", [])))
            for item in data.get("invalid_days", []):
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or item[0] not in segments
                ):
                    raise ValueError("Ungültige gespeicherte Tageskorrektur")
                if datetime.fromisoformat(item[1]).date().isoformat() != item[1]:
                    raise ValueError("Ungültiges gespeichertes Korrekturdatum")
                history._invalid_days.add(tuple(item))
            history.replace_source(source)
            return history
        except (KeyError, TypeError, AttributeError, OverflowError) as err:
            raise ValueError("Die gespeicherte Messhistorie ist beschädigt") from err


def _covered_seconds(deltas: Iterable[EnergyDelta]) -> float:
    """Überlappende Bereiche für die Abdeckung nur einmal zählen."""
    intervals = sorted((delta.start, delta.end) for delta in deltas)
    if not intervals:
        return 0
    start, end = intervals[0]
    seconds = 0.0
    for next_start, next_end in intervals[1:]:
        if next_start > end:
            seconds += (end - start).total_seconds()
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return seconds + (end - start).total_seconds()


def aggregate_energy(
    histories: Iterable[SourceHistory],
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    cached_snapshots: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Disjunkte Erzeuger erst nach quelleneigener Resetprüfung addieren."""
    start, end, now = _utc(start), _utc(end), _utc(now)
    if end <= start:
        raise ValueError("Das Messfenster muss eine positive Dauer haben")
    energy_sources = [
        history for history in histories if _has_energy_in_window(history, start, end)
    ]
    if len({h.source.source_id for h in energy_sources}) != len(energy_sources):
        raise ValueError("Eine Messquelle darf nicht doppelt addiert werden")
    identities = [h.source.registry_id or h.source.entity_id for h in energy_sources]
    entities = [h.source.entity_id for h in energy_sources]
    if len(set(identities)) != len(identities) or len(set(entities)) != len(entities):
        raise ValueError("Eine Sensoridentität darf nicht doppelt addiert werden")
    snapshots = [
        (
            cached_snapshots[history.source.source_id]
            if cached_snapshots is not None
            else history.snapshot(start, end, now)
        )
        for history in energy_sources
    ]
    flags = {flag for snapshot in snapshots for flag in snapshot["quality_flags"]}
    values = [
        snapshot["energy_kwh"]
        for snapshot in snapshots
        if snapshot["energy_kwh"] is not None
    ]
    try:
        energy = fsum(values) if values else None
    except OverflowError:
        energy = None
        flags.add("arithmetic_overflow")
    return {
        "energy_kwh": energy,
        "energy_complete": bool(snapshots)
        and energy is not None
        and all(s["energy_complete"] for s in snapshots),
        "complete": bool(snapshots)
        and energy is not None
        and all(s["complete"] for s in snapshots),
        "quality_flags": sorted(flags),
        "source_count": len(snapshots),
    }


def _has_energy_in_window(
    history: SourceHistory, start: datetime, end: datetime
) -> bool:
    """Messarten gelten je Segment; aktuelle Energiequellen können noch leer sein."""
    if history.source.kind != "power":
        return True
    energy_segments = {
        segment_id
        for segment_id, config in history.segment_sources.items()
        if config.kind != "power"
    }
    if any(
        delta.segment_id in energy_segments and delta.start < end and delta.end > start
        for delta in history.deltas
    ):
        return True
    # Auch unbekannte historische Energiezustände bleiben als Messlücke sichtbar.
    bounds: dict[str, tuple[datetime, datetime]] = {}
    for reading in history.readings:
        if (
            reading.segment_id in energy_segments
            and "late_reading" not in reading.quality_flags
        ):
            first, last = bounds.get(
                reading.segment_id, (reading.timestamp, reading.timestamp)
            )
            bounds[reading.segment_id] = min(first, reading.timestamp), max(
                last, reading.timestamp
            )
    return any(first < end and last > start for first, last in bounds.values())
