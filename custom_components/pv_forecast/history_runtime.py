"""Passives Prognosearchiv am gemeinsamen Coordinator und lokalen Messpfad."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_LOSS_FACTOR,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_TILT,
    CONF_TIME_ZONE,
    DOMAIN,
)
from .coordinator import PvForecastCoordinator
from .history import HistoryArchive
from .measurement_runtime import MeasurementManager
from .measurements import SourceConfig

if TYPE_CHECKING:
    from .calibration_runtime import CalibrationManager

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 2
SAVE_DELAY = 300
MAX_STORAGE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 6000
ASSESSMENT_RETENTION = timedelta(days=7)


class _HistoryStore(Store[dict[str, Any]]):
    """Alte Archive ohne rückwirkend erfundene Kalibrierungsbasis übernehmen."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise NotImplementedError
        # Die optionalen neuen Recordfelder werden beim Lesen ergänzt. Der
        # vorhandene Inhalt bleibt bei dieser Migration vollständig erhalten.
        HistoryArchive.from_dict(old_data["archive"], old_data["archive"]["timezone"])
        return old_data


def _history_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return _HistoryStore(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.history.{entry_id}",
        private=True,
        atomic_writes=True,
    )


async def async_remove_history_store(hass: HomeAssistant, entry_id: str) -> None:
    """Das gesamte Archiv beim Entfernen der Anlage löschen."""

    await _history_store(hass, entry_id).async_remove()


async def async_delete_history_data(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bewusst das gesamte Archiv auch bei pausierter Erfassung löschen."""

    from .calibration_runtime import async_delete_calibration_data

    await async_delete_calibration_data(hass, entry)
    manager = getattr(getattr(entry, "runtime_data", None), "history", None)
    if manager is not None:
        await manager.async_delete_data()
    else:
        await async_remove_history_store(hass, entry.entry_id)


async def async_delete_history_source_data(
    hass: HomeAssistant, entry: ConfigEntry, source_id: str
) -> None:
    """Messkopien einer bestätigten Quelle auch aus einem entladenen Archiv löschen."""

    from .calibration_runtime import async_delete_calibration_data

    await async_delete_calibration_data(hass, entry)
    manager = getattr(getattr(entry, "runtime_data", None), "history", None)
    if manager is not None and manager.loaded:
        await manager.async_delete_measurement_source(source_id)
        return
    store = _history_store(hass, entry.entry_id)
    stored = await store.async_load()
    if stored is None:
        return
    archive = HistoryArchive.from_dict(
        stored["archive"], str(entry.data[CONF_TIME_ZONE])
    )
    if archive.delete_measurement_source(source_id):
        stored["archive"] = archive.to_dict()
        await store.async_save(stored)


def _configured_measurements(entry: ConfigEntry) -> tuple[SourceConfig, ...]:
    return tuple(
        SourceConfig.from_dict(source)
        for source in entry.options.get("measurement_sources", [])
    )


def _configuration_id(entry: ConfigEntry) -> str:
    """Physische Parameter und Messgrenzen ohne Anzeigenamen kanonisch markieren."""

    roof_keys = (CONF_INSTALLED_POWER_KWP, CONF_AZIMUTH, CONF_TILT, CONF_LOSS_FACTOR)
    roofs = [
        {
            CONF_ROOF_ID: roof[CONF_ROOF_ID],
            **{key: float(roof[key]) for key in roof_keys},
        }
        for roof in entry.options[CONF_ROOFS]
    ]
    physical = {
        CONF_LATITUDE: float(entry.data[CONF_LATITUDE]),
        CONF_LONGITUDE: float(entry.data[CONF_LONGITUDE]),
        CONF_TIME_ZONE: str(entry.data[CONF_TIME_ZONE]),
        CONF_ROOFS: sorted(roofs, key=lambda roof: roof[CONF_ROOF_ID]),
        CONF_INVERTER_MAX_POWER_KW: (
            float(value)
            if (value := entry.options.get(CONF_INVERTER_MAX_POWER_KW)) is not None
            else None
        ),
        "measurements": sorted(
            [
                (source.source_id, source.measurement_identity)
                for source in _configured_measurements(entry)
                if source.kind != "power"
            ],
            key=lambda value: value[0],
        ),
    }
    encoded = json.dumps(
        physical, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class ArchiveManager:
    """Tatsächlich beobachtete Prognosen ohne zusätzliche Abrufe archivieren."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: PvForecastCoordinator,
        measurements: MeasurementManager | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.measurements = measurements
        self.timezone = str(entry.data[CONF_TIME_ZONE])
        self.enabled = entry.options.get("history_enabled") is True
        self._archive = HistoryArchive(self.timezone)
        self._store = _history_store(hass, entry.entry_id)
        self._cancel_listener: CALLBACK_TYPE | None = None
        self._last_fetched_at: datetime | None = None
        self._storage_error: str | None = None
        self._loaded = False
        self._running = False
        self._stopped = False
        self._save_scheduled = False
        self._dirty = False
        self._assessment_task: asyncio.Task[None] | None = None
        self._assessment_requested = False
        self._mutation_in_progress = False
        self.calibration: CalibrationManager | None = None
        self._last_calibration_capture: tuple[Any, ...] | None = None

    @property
    def running(self) -> bool:
        """Ob der gemeinsame Coordinator derzeit zur Erfassung abonniert ist."""

        return self._running

    @property
    def loaded(self) -> bool:
        """Ob vorhandene lokale Daten erfolgreich geladen wurden."""

        return self._loaded

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Alle gespeicherten externen Quellidentitäten für die Leserechte."""

        return tuple(
            dict.fromkeys(
                self._resolve_source(source)[0]
                for source in self._archive.external_sources()
            )
        )

    @property
    def identity_unresolved(self) -> tuple[str, ...]:
        """Nicht mehr überprüfbare historische Quellen konservativ kenntlich machen."""

        return tuple(
            dict.fromkeys(
                entity_id
                for source in self._archive.external_sources()
                if not (resolved := self._resolve_source(source))[1]
                for entity_id in (resolved[0],)
            )
        )

    @callback
    def _resolve_source(self, source: dict[str, Any]) -> tuple[str, bool]:
        entity_id = source["entity_id"]
        registry = er.async_get(self.hass)
        if registry_id := source.get("registry_id"):
            registered = registry.async_get(registry_id)
            if registered is not None:
                return registered.entity_id, True
            return entity_id, False
        return entity_id, (
            registry.async_get(entity_id) is None
            and self.hass.states.get(entity_id) is not None
        )

    async def async_start(self) -> None:
        """Nur nach bewusster Konfiguration laden; pausierte Archive bleiben lesbar."""

        if self._loaded or "history_enabled" not in self.entry.options:
            return
        try:
            stored = await self._store.async_load()
            if stored is not None:
                self._archive = HistoryArchive.from_dict(
                    stored["archive"], self.timezone
                )
                if last_fetched := stored.get("last_fetched_at"):
                    parsed = datetime.fromisoformat(last_fetched)
                    if parsed.utcoffset() is None:
                        raise ValueError("Der Abrufzeitpunkt benötigt eine Zeitzone")
                    self._last_fetched_at = parsed.astimezone(UTC)
        except (
            HomeAssistantError,
            NotImplementedError,
            ValueError,
            TypeError,
            KeyError,
        ):
            self._storage_error = "storage_unavailable"
            _LOGGER.exception("Das lokale Prognosearchiv ist nicht lesbar")
            return
        self._loaded = True
        if self.enabled:
            self._running = True
            self._cancel_listener = self.coordinator.async_add_listener(self._updated)
            self._updated()

    async def async_stop(self) -> None:
        """Den gemeinsamen Listener beenden und ausstehende Daten speichern."""

        self._running = False
        self._stopped = True
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        await self._async_cancel_assessment()
        if self._loaded and self._storage_error is None and self._dirty:
            await self._store.async_save(self._serialize())

    async def async_delete_data(self) -> None:
        """Bewusst alle Daten löschen und erst beim nächsten neuen Abruf beginnen."""

        self._mutation_in_progress = True
        try:
            await self._async_cancel_assessment()
            await self._store.async_remove()
        finally:
            self._mutation_in_progress = False
        self._archive = HistoryArchive(self.timezone)
        self._last_fetched_at = self.coordinator.last_update_success_time
        self._storage_error = None
        self._loaded = True
        self._save_scheduled = False
        self._dirty = False
        if self.enabled and not self._running and not self._stopped:
            self._running = True
            self._cancel_listener = self.coordinator.async_add_listener(self._updated)

    async def async_delete_measurement_source(self, source_id: str) -> None:
        """Gezielt historische Messkopien und Bewertungsrevisionen entfernen."""

        if self._storage_error is not None:
            raise HomeAssistantError("Das Prognosearchiv ist nicht lesbar")
        self._mutation_in_progress = True
        try:
            await self._async_cancel_assessment()
            if self._archive.delete_measurement_source(source_id):
                self._dirty = True
                await self._store.async_save(self._serialize())
        finally:
            self._mutation_in_progress = False

    async def _async_cancel_assessment(self) -> None:
        self._assessment_requested = False
        if task := self._assessment_task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._assessment_task = None

    @callback
    def _updated(self) -> None:
        if not self._running or self._mutation_in_progress:
            return
        now = dt_util.utcnow()
        changed = self._archive.note_configuration(_configuration_id(self.entry), now)
        fetched_at = self.coordinator.last_update_success_time
        calibration = (
            self.calibration.capture_parameters()
            if self.calibration is not None
            else {}
        )
        calibration_signature = tuple(calibration.items())
        if (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and fetched_at is not None
            and (
                self._last_fetched_at is None
                or fetched_at > self._last_fetched_at
                or (
                    self._last_calibration_capture is not None
                    and calibration_signature != self._last_calibration_capture
                )
            )
        ):
            changed |= self._archive.capture(
                getattr(self.coordinator, "raw_data", None) or self.coordinator.data,
                fetched_at,
                now,
                _configuration_id(self.entry),
                _configured_measurements(self.entry),
                self._comparison(now),
                inverter_max_power_kw=self.entry.options.get(
                    CONF_INVERTER_MAX_POWER_KW
                ),
                **calibration,
            )
            self._last_fetched_at = fetched_at
            self._last_calibration_capture = calibration_signature
            changed = True
        if changed:
            self._dirty = True
            self._schedule_save()
        self._assessment_requested = True
        if self._assessment_task is None:
            self._assessment_task = self.entry.async_create_task(
                self.hass,
                self._async_assess_pending(),
                "PV-Archiv lokal bewerten",
                eager_start=False,
            )

    async def _async_assess_pending(self) -> None:
        """Folgeupdates bündeln und zwischen vergangenen Messfenstern weitergeben."""

        try:
            while self._running and self._assessment_requested:
                self._assessment_requested = False
                now = dt_util.utcnow()
                windows: dict[tuple[datetime, datetime], list[str]] = {}
                for record_id, record in tuple(self._archive.records.items()):
                    if (
                        not now - ASSESSMENT_RETENTION
                        <= record.start
                        < record.end
                        <= now
                    ):
                        continue
                    windows.setdefault((record.start, record.end), []).append(record_id)
                for window, record_ids in windows.items():
                    await asyncio.sleep(0)
                    evidence = (
                        await self.measurements.async_snapshot(*window, now)
                        if self.measurements is not None
                        else None
                    )
                    for record_id in record_ids:
                        await asyncio.sleep(0)
                        if record_id not in self._archive.records:
                            continue
                        if self._archive.assess(record_id, evidence, now):
                            self._dirty = True
                            self._schedule_save()
                if self._archive.prune(
                    now, max_records=MAX_RECORDS, max_bytes=MAX_STORAGE_BYTES - 1024
                ):
                    self._dirty = True
                    self._schedule_save()
                if self.calibration is not None:
                    self.calibration.async_reconcile()
        finally:
            self._assessment_task = None

    @callback
    def _comparison(self, now: datetime) -> dict[str, dict[str, Any]]:
        config = self.entry.options.get("comparison_forecast")
        if (
            not isinstance(config, dict)
            or config.get("confirmed_same_boundary") is not True
        ):
            return {}
        local_date = now.astimezone(ZoneInfo(self.timezone)).date()
        result = {}
        for offset, day in enumerate(("today", "tomorrow")):
            source = {
                "entity_id": config.get(f"{day}_entity_id"),
                "registry_id": config.get(f"{day}_registry_id"),
            }
            if not isinstance(source["entity_id"], str):
                continue
            entity_id, resolved = self._resolve_source(source)
            state = self.hass.states.get(entity_id) if resolved else None
            if (
                state is None
                or state.attributes.get("device_class") != "energy"
                or state.attributes.get("restored")
            ):
                continue
            age = (now - state.last_reported).total_seconds()
            unit = state.attributes.get("unit_of_measurement")
            if not 0 <= age <= 7200 or unit not in ("Wh", "kWh"):
                continue
            try:
                value = float(state.state) * (0.001 if unit == "Wh" else 1)
            except (ValueError, OverflowError):
                continue
            if not isfinite(value) or value < 0:
                continue
            result[(local_date + timedelta(days=offset)).isoformat()] = {
                "energy_kwh": value,
                "entity_id": entity_id,
                "registry_id": source["registry_id"],
                "scope": config["scope"],
                "reported_at": state.last_reported.isoformat(),
                "observed_at": now.isoformat(),
            }
        return result

    @callback
    def snapshot(
        self,
        days: int = 30,
        now: datetime | None = None,
        include_records: bool = False,
    ) -> dict[str, Any]:
        """Metriken aus bereits vorhandenen Daten ohne Änderung des Archivs lesen."""

        result = self._archive.snapshot(now or dt_util.utcnow(), days, include_records)
        result.update(
            enabled=self.enabled,
            running=self.running,
            storage_error=self._storage_error,
        )
        if self.calibration is not None:
            result["calibration"] = self.calibration.snapshot()
        return result

    @callback
    def current_targets(self, now: datetime) -> dict[str, Any]:
        """Aktuelle feste Prognoseintervalle ohne neue Erfassung zurückgeben."""

        return self._archive.current_targets(now)

    @callback
    def export(
        self,
        days: int = 30,
        format: Literal["json", "csv"] = "json",
        now: datetime | None = None,
    ) -> dict[str, Any] | str:
        """Nur nach ausdrücklichem Aufruf exportieren; keine Dateien veröffentlichen."""

        data = self.snapshot(days, now, include_records=True)
        if format == "json":
            return data
        if format != "csv":
            raise ValueError("Als Exportformat sind JSON und CSV vorgesehen")
        output = io.StringIO(newline="")
        records = data.get("records", [])
        fieldnames = sorted({key for record in records for key in record})
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {}
            for key, value in record.items():
                text = (
                    json.dumps(
                        value, ensure_ascii=False, sort_keys=True, allow_nan=False
                    )
                    if isinstance(value, dict | list | tuple)
                    else value
                )
                # CSV-Inhalte können in Tabellenprogrammen geöffnet werden.
                # Anwendertexte dürfen dabei keine Formel ausführen.
                if isinstance(text, str) and text.startswith(
                    ("=", "+", "-", "@", "\t", "\r")
                ):
                    text = "'" + text
                row[key] = text
            writer.writerow(row)
        return output.getvalue()

    @callback
    def _schedule_save(self) -> None:
        if self._running and not self._save_scheduled:
            self._save_scheduled = True
            self._store.async_delay_save(self._serialize, SAVE_DELAY)

    @callback
    def _serialize(self) -> dict[str, Any]:
        # Auch Lifecycle-Schreibungen können eine noch laufende Bewertung
        # überholen. Die harte Speichergrenze gilt vor jedem Schreiben.
        self._archive.prune(
            dt_util.utcnow(),
            max_records=MAX_RECORDS,
            max_bytes=MAX_STORAGE_BYTES - 1024,
        )
        self._save_scheduled = False
        self._dirty = False
        return {
            "archive": self._archive.to_dict(),
            "last_fetched_at": (
                self._last_fetched_at.isoformat() if self._last_fetched_at else None
            ),
        }
