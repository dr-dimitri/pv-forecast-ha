"""Lokale Erfassung bestätigter PV-Messquellen aus Home-Assistant-Ereignissen."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from math import fsum, isfinite
from sys import float_info
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .configuration import location_fingerprint
from .const import CONF_INSTALLED_POWER_KWP, CONF_ROOFS, CONF_TIME_ZONE, DOMAIN
from .measurement_helpers import helper_matches
from .measurement_windows import MeasurementWindow, async_interval_windows
from .measurements import (
    SourceConfig,
    SourceHistory,
    aggregate_energy,
    normalize_reading_value,
)
from .models import ForecastResult
from .outlook import build_day_outlook
from .storage import ConfirmedStore

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 2
RETENTION = timedelta(days=7)
MAX_READINGS = 20_000
SAVE_DELAY = 60


class _MeasurementStore(ConfirmedStore):
    """Standortkontexte alter Messsegmente ohne Änderung der Messwerte ergänzen."""

    location_data: dict[str, Any]

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise NotImplementedError
        timezone = str(self.location_data[CONF_TIME_ZONE])
        location_id = location_fingerprint(self.location_data)
        sources = {}
        for source_id, data in old_data["sources"].items():
            history = SourceHistory.from_dict(
                SourceConfig.from_dict(data["source"]), data, timezone, float_info.max
            )
            history.bind_location(location_id, timezone, dt_util.utcnow())
            sources[source_id] = dict(data) | {
                "segment_contexts": history.to_dict()["segment_contexts"]
            }
        return dict(old_data) | {"sources": sources}


def _measurement_store(hass: HomeAssistant, entry_id: str) -> ConfirmedStore:
    """Den unabhängig von Config Entries versionierten lokalen Speicher öffnen."""

    store = _MeasurementStore(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.measurements.{entry_id}",
        private=True,
        atomic_writes=True,
    )
    entry = hass.config_entries.async_get_entry(entry_id)
    store.location_data = dict(entry.data) if entry is not None else {}
    return store


async def async_remove_measurement_store(hass: HomeAssistant, entry_id: str) -> None:
    """Alle Messdaten beim Entfernen der Anlage löschen."""

    await _measurement_store(hass, entry_id).async_remove()


async def async_delete_measurement_source_data(
    hass: HomeAssistant, entry: ConfigEntry, source_id: str
) -> None:
    """Bestätigt ausgewählte Quelldaten auch bei entladener Anlage löschen."""

    from .history_runtime import async_delete_history_source_data

    await async_delete_history_source_data(hass, entry, source_id)
    manager = getattr(getattr(entry, "runtime_data", None), "measurements", None)
    if manager is not None and manager.running:
        await manager.async_delete_source_data(source_id)
        return
    store = _measurement_store(hass, entry.entry_id)
    data = await store.async_load()
    if isinstance(data, dict) and isinstance(data.get("sources"), dict):
        data["sources"].pop(source_id, None)
        await store.async_save_checked(data)


class MeasurementManager:
    """Messwerte passiv erfassen, begrenzt speichern und ohne HTTP auswerten."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.timezone = str(entry.data[CONF_TIME_ZONE])
        self._store = _measurement_store(hass, entry.entry_id)
        self._store.async_track_writes(None, SAVE_DELAY)
        self._histories: dict[str, SourceHistory] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._cancel_cleanup: CALLBACK_TYPE | None = None
        self._running = False
        self._storage_error: str | None = None
        self._save_scheduled = False
        # Diese großzügige Grenze ist nur eine Qualitätsheuristik. Sie begründet
        # weder die Herkunft eines Zählers noch die physikalische Anlagengrenze.
        try:
            power = fsum(
                float(roof[CONF_INSTALLED_POWER_KWP])
                for roof in entry.options.get(CONF_ROOFS, [])
            )
            self._max_power_kw = power * 2
            if not isfinite(self._max_power_kw) or self._max_power_kw <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            self._max_power_kw = float_info.max
        configured = entry.options.get("measurement_sources", [])
        if not isinstance(configured, list | tuple):
            configured = []
        for raw in configured:
            try:
                source = SourceConfig.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ungültige Messquellenkonfiguration wird ausgelassen")
                continue
            if source.source_id in self._histories or any(
                old.source.entity_id == source.entity_id
                or (
                    source.registry_id is not None
                    and old.source.registry_id == source.registry_id
                )
                for old in self._histories.values()
            ):
                _LOGGER.warning("Doppelte Messquellen-ID wird ausgelassen")
                continue
            self._histories[source.source_id] = SourceHistory(
                source, self.timezone, self._max_power_kw
            )
            self._histories[source.source_id].bind_location(
                location_fingerprint(entry.data), self.timezone, dt_util.utcnow()
            )

    @property
    def running(self) -> bool:
        """Ob die Erfassung aktiv ist."""

        return self._running

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Alle aktuellen und historischen Quell-Entities für die Leserechte."""

        return tuple(
            dict.fromkeys(
                entity_id
                for history in self._histories.values()
                for source in history.segment_sources.values()
                for entity_id in self._source_entity_ids(source)
            )
        )

    @property
    def identity_unresolved(self) -> tuple[str, ...]:
        """Quellen ohne weiterhin bestätigte Entity-Identität."""

        return tuple(
            source_id
            for source_id, history in self._histories.items()
            if any(
                not self._identity_matches(
                    replace(source, entity_id=self._resolved_entity_id(source))
                )
                for source in history.segment_sources.values()
            )
        )

    @property
    def storage_error(self) -> str | None:
        """Schreibfehler melden, ohne die weiterhin gültige Erfassung zu sperren."""
        return self._storage_error or self._store.write_error

    async def async_start(self, *, fresh_after: datetime | None = None) -> None:
        """Historie laden und ausschließlich lokale Ereignisse abonnieren."""

        if self._running:
            return
        if not self._histories:
            # Nach bestätigtem Entfernen der letzten Quelle kann der vorherige
            # Manager bis zum Reload noch Berichte gespeichert haben.
            if self.entry.options.get("measurement_sources") == []:
                await self._store.async_remove()
            return
        try:
            stored = await self._store.async_load()
        except (
            HomeAssistantError,
            NotImplementedError,
            ValueError,
            KeyError,
            TypeError,
        ) as err:
            _LOGGER.exception("Gespeicherte PV-Messdaten können nicht geladen werden")
            self._storage_error = (
                "unsupported_version"
                if isinstance(err, NotImplementedError)
                else "storage_unavailable"
            )
            return
        source_data = stored.get("sources", {}) if isinstance(stored, dict) else {}
        if not isinstance(source_data, dict):
            source_data = {}
        for source_id, history in tuple(self._histories.items()):
            if isinstance(data := source_data.get(source_id), dict):
                try:
                    history = SourceHistory.from_dict(
                        history.source, data, self.timezone, self._max_power_kw
                    )
                    history.bind_location(
                        location_fingerprint(self.entry.data),
                        self.timezone,
                        dt_util.utcnow(),
                    )
                    self._histories[source_id] = history
                except (KeyError, TypeError, ValueError, OverflowError):
                    _LOGGER.warning("Ungültige gespeicherte Messquelle %s", source_id)
            history.mark_gap("restart")
        self._resolve_entity_ids()
        self._running = True
        self._subscribe()
        self._cancel_cleanup = async_track_time_interval(
            self.hass, self._cleanup, timedelta(hours=1)
        )
        for history in self._histories.values():
            if self._identity_matches(history.source):
                state = self.hass.states.get(history.source.entity_id)
                if (
                    state is not None
                    and (fresh_after is None or state.last_reported >= fresh_after)
                    and (
                        history.latest_reading is None
                        or state.last_reported > history.latest_reading.timestamp
                    )
                ):
                    self._record(history, state, state.last_reported)
        self._prune(dt_util.utcnow())
        self._schedule_save()

    async def async_stop(self) -> None:
        """Alle Listener und Timer beenden und ausstehende Daten speichern."""

        self._store.async_stop_retries()
        if not self._running:
            return
        self._running = False
        self._unsubscribe()
        if self._cancel_cleanup is not None:
            self._cancel_cleanup()
            self._cancel_cleanup = None
        self._prune(dt_util.utcnow())
        self._save_scheduled = False
        await self._store.async_save(self._serialize())

    async def async_delete_source_data(self, source_id: str) -> None:
        """Nur diese Historie löschen; der nächste Bericht beginnt neu."""

        if self._storage_error is not None:
            raise HomeAssistantError("Der Messdatenspeicher ist nicht lesbar")
        history = self._histories.get(source_id)
        if history is None:
            return
        replacement = SourceHistory(history.source, self.timezone, self._max_power_kw)
        replacement.bind_location(
            location_fingerprint(self.entry.data), self.timezone, dt_util.utcnow()
        )
        replacement.mark_gap("data_deleted")
        self._histories[source_id] = replacement
        self._save_scheduled = False
        await self._store.async_save_checked(self._serialize())

    @callback
    def preview(self, source_id: str) -> dict[str, Any]:
        """Den letzten gültigen Messstand für die Optionsvorschau bereitstellen."""

        history = self._histories.get(source_id)
        reading = history.last_valid_reading if history else None
        return {
            "last_valid_value": reading.value if reading else None,
            "unit": "kW" if history and history.source.kind == "power" else "kWh",
            "timestamp": reading.timestamp.isoformat() if reading else None,
        }

    @callback
    def day_outlook(
        self,
        forecast: ForecastResult,
        now: datetime,
        fetched_at: datetime | None,
        last_update_success: bool,
    ) -> dict[str, Any]:
        """Die lokale Tagesaussicht mit denselben geprüften Messquellen bilden."""
        return build_day_outlook(
            forecast,
            tuple(
                history.current_location_view() for history in self._histories.values()
            ),
            self.timezone,
            now,
            fetched_at,
            last_update_success,
            identity_unresolved=bool(self.identity_unresolved),
        )

    @callback
    def snapshot(self, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
        """Ein UTC-Fenster aus vorhandenen Messwerten ohne Nebenwirkungen lesen."""

        unresolved = set(self.identity_unresolved)
        sources = []
        for source_id, history in self._histories.items():
            result = history.snapshot(start, end, now)
            result["identity_unresolved"] = source_id in unresolved
            sources.append(result)
        result = self._snapshot_result(
            start, end, now, tuple(self._histories.values()), sources
        )
        result["current_location_total_energy"] = aggregate_energy(
            tuple(
                history.current_location_view() for history in self._histories.values()
            ),
            start,
            end,
            now,
        )
        return result

    async def async_snapshot(
        self, start: datetime, end: datetime, now: datetime
    ) -> dict[str, Any]:
        """Größere Bewertungen geben zwischen den Messquellen den Eventloop frei."""

        while True:
            histories = tuple(self._histories.items())
            sources = []
            for source_id, history in histories:
                await asyncio.sleep(0)
                if self._histories.get(source_id) is not history:
                    break
                result = history.snapshot(start, end, now)
                result["identity_unresolved"] = source_id in self.identity_unresolved
                sources.append(result)
            else:
                if histories == tuple(self._histories.items()):
                    return self._snapshot_result(
                        start,
                        end,
                        now,
                        tuple(history for _, history in histories),
                        sources,
                    )

    @callback
    async def async_interval_windows(
        self, windows: list[MeasurementWindow], now: datetime
    ) -> list[dict[str, Any]]:
        """Kurvenfenster ohne wiederholte Vollscans und ohne gelöschte Kopien lesen."""

        while True:
            histories = tuple(
                (source_id, history, history.source, history.segment_id)
                for source_id, history in self._histories.items()
            )
            result = await async_interval_windows(
                tuple(
                    history.current_location_view() for _, history, _, _ in histories
                ),
                windows,
                now,
            )
            if histories == tuple(
                (source_id, history, history.source, history.segment_id)
                for source_id, history in self._histories.items()
            ):
                return result

    def _snapshot_result(
        self,
        start: datetime,
        end: datetime,
        now: datetime,
        histories: tuple[SourceHistory, ...],
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "storage_error": self.storage_error,
            "timezone": self.timezone,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sources": sources,
            "total_energy": aggregate_energy(
                histories,
                start,
                end,
                now,
                cached_snapshots={source["source_id"]: source for source in sources},
            ),
            "retention_days": RETENTION.days,
            "max_readings_per_source": MAX_READINGS,
        }

    @callback
    def _source_entity_ids(self, source: SourceConfig) -> tuple[str, ...]:
        """Auch die ursprüngliche Leistungsmessung benötigt Leserechte."""
        ids = (self._resolved_entity_id(source),)
        if source.upstream_registry_id is not None:
            registered = er.async_get(self.hass).async_get(source.upstream_registry_id)
            ids += (registered.entity_id if registered else source.upstream_entity_id,)
        return ids

    @callback
    def _upstream_valid(self, source: SourceConfig, timestamp: datetime) -> bool:
        """Fehlende, ungültige oder stehengebliebene Leistungsdaten nicht bestätigen."""
        if source.upstream_registry_id is None:
            return True
        registry = er.async_get(self.hass).async_get(source.upstream_registry_id)
        state = self.hass.states.get(registry.entity_id) if registry else None
        return bool(
            state is not None
            and not state.attributes.get("restored")
            and state.attributes.get("device_class") == "power"
            and state.attributes.get("state_class") == "measurement"
            and normalize_reading_value(
                state.state, state.attributes.get("unit_of_measurement"), "power"
            )[0]
            is not None
            and abs(timestamp - state.last_reported)
            <= timedelta(minutes=source.max_interval_minutes)
        )

    @callback
    def _upstream_event(
        self, entity_id: str, timestamp: datetime, previous: datetime | None
    ) -> None:
        """Lücken vor dem nachfolgenden Helferereignis in der Messhistorie vermerken."""
        for history in self._histories.values():
            source = history.source
            if (
                source.upstream_registry_id is None
                or self._source_entity_ids(source)[1] != entity_id
            ):
                continue
            if (
                not self._upstream_valid(source, timestamp)
                or previous is None
                or timestamp - previous > timedelta(minutes=source.max_interval_minutes)
            ):
                history.mark_gap("upstream_gap")

    @callback
    def _identity_matches(self, source: SourceConfig) -> bool:
        """Austausch unter gleichem Entitynamen nicht stillschweigend übernehmen."""

        registered = er.async_get(self.hass).async_get(source.entity_id)
        if source.upstream_registry_id is not None:
            helper = self.hass.config_entries.async_get_entry(source.helper_entry_id)
            if helper is None or not helper_matches(
                self.hass, helper, source.upstream_registry_id
            ):
                return False
        if source.registry_id is None:
            return (
                registered is None
                and self.hass.states.get(source.entity_id) is not None
            )
        return registered is not None and registered.id == source.registry_id

    @callback
    def _resolved_entity_id(self, source: SourceConfig) -> str:
        if source.registry_id is not None:
            registered = er.async_get(self.hass).async_get(source.registry_id)
            if registered is not None:
                return registered.entity_id
        return source.entity_id

    @callback
    def _resolve_entity_ids(self) -> None:
        registry = er.async_get(self.hass)
        for history in self._histories.values():
            source = history.source
            if source.registry_id is not None:
                registered = registry.async_get(source.registry_id)
                if registered is not None and registered.entity_id != source.entity_id:
                    history.replace_source(
                        replace(source, entity_id=registered.entity_id)
                    )

    @callback
    def _subscribe(self) -> None:
        entity_ids = tuple(
            dict.fromkeys(
                entity_id
                for history in self._histories.values()
                for entity_id in self._source_entity_ids(history.source)
            )
        )
        self._listeners = [
            async_track_state_change_event(self.hass, entity_ids, self._state_changed),
            async_track_state_report_event(self.hass, entity_ids, self._state_reported),
            async_track_entity_registry_updated_event(
                self.hass, entity_ids, self._registry_changed
            ),
        ]

    @callback
    def _unsubscribe(self) -> None:
        for cancel in self._listeners:
            cancel()
        self._listeners.clear()

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        old = event.data["old_state"]
        self._upstream_event(
            event.data["entity_id"],
            event.time_fired,
            old.last_reported if old else None,
        )
        self._receive(
            event.data["entity_id"],
            state,
            state.last_updated if state is not None else event.time_fired,
        )

    @callback
    def _state_reported(self, event: Event[EventStateReportedData]) -> None:
        # State.last_reported wird bei unverändertem Zustand mutiert. Das Datum
        # des konkreten Ereignisses schützt vor später eingetroffenen Berichten.
        self._upstream_event(
            event.data["entity_id"],
            event.data["last_reported"],
            event.data["old_last_reported"],
        )
        self._receive(
            event.data["entity_id"],
            event.data["new_state"],
            event.data["last_reported"],
        )

    @callback
    def _receive(
        self, entity_id: str, state: State | None, timestamp: datetime
    ) -> None:
        if not self._running:
            return
        for history in self._histories.values():
            if history.source.entity_id == entity_id:
                if self._identity_matches(history.source):
                    self._record(history, state, timestamp)
                else:
                    history.mark_gap("identity_unresolved")
        self._schedule_save()

    @callback
    def _record(
        self, history: SourceHistory, state: State | None, timestamp: datetime
    ) -> None:
        attributes = state.attributes if state is not None else {}
        last_reset = attributes.get("last_reset")
        if isinstance(last_reset, str):
            last_reset = dt_util.parse_datetime(last_reset)
        if not isinstance(last_reset, datetime) or last_reset.tzinfo is None:
            last_reset = None
        flags: set[str] = set()
        if not self._upstream_valid(history.source, timestamp):
            history.mark_gap("upstream_gap")
            return
        if attributes.get("restored"):
            flags.add("restored_state")
            value = "unavailable"
        else:
            value = state.state if state is not None else "unavailable"
        if value not in ("unknown", "unavailable"):
            kind = history.source.kind
            expected_class = "power" if kind == "power" else "energy"
            allowed_state_classes = (
                {"measurement"} if kind == "power" else {"total", "total_increasing"}
            )
            if (
                attributes.get("device_class") != expected_class
                or not isinstance(attributes.get("state_class"), str)
                or attributes.get("state_class") not in allowed_state_classes
            ):
                flags.add("invalid_metadata")
                value = "unavailable"
        unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        history.add_reading(
            timestamp,
            value,
            unit if isinstance(unit, str) else None,
            last_reset,
            quality_flags=flags,
        )
        history.prune(dt_util.utcnow() - RETENTION, MAX_READINGS)

    @callback
    def _registry_changed(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        if not self._running:
            return
        self._resolve_entity_ids()
        for history in self._histories.values():
            if not self._identity_matches(history.source):
                history.mark_gap("identity_unresolved")
        self._unsubscribe()
        self._subscribe()
        self._schedule_save()

    @callback
    def _cleanup(self, now: datetime) -> None:
        self._prune(now)
        self._schedule_save()

    @callback
    def _prune(self, now: datetime) -> None:
        for history in self._histories.values():
            history.prune(now - RETENTION, MAX_READINGS)

    @callback
    def _schedule_save(self) -> None:
        if self._running and not self._save_scheduled:
            self._save_scheduled = True
            self._store.async_delay_save(self._serialize_scheduled_save, SAVE_DELAY)

    @callback
    def _serialize_scheduled_save(self) -> dict[str, Any]:
        # Häufige Berichte dürfen den bereits geplanten Schreibtermin nicht
        # verschieben. Der Snapshot enthält trotzdem alle seitherigen Werte.
        self._save_scheduled = False
        return self._serialize()

    @callback
    def _serialize(self) -> dict[str, Any]:
        return {
            "sources": {
                source_id: history.to_dict()
                for source_id, history in self._histories.items()
            }
        }
