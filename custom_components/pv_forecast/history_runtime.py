"""Passives Prognosearchiv am gemeinsamen Coordinator und lokalen Messpfad."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .configuration import inverter_groups_from_options, roofs_from_options
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
from .shading import CONF_HORIZON_PROFILES, HORIZON_RULE_VERSION
from .short_term import trial_report
from .temperature_comparison import comparison_report, mountings_from_options
from .uncertainty_data import current_experience_bands
from .underperformance import empty_state, notification_due, observe

if TYPE_CHECKING:
    from .calibration_runtime import CalibrationManager

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 6
SAVE_DELAY = 300
MAX_STORAGE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 6000
ASSESSMENT_RETENTION = timedelta(days=7)


class _HistoryStore(Store[dict[str, Any]]):
    """Alte Archive mit ihrer ursprünglichen Modell- und Tagesbasis bewahren."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version not in (1, 2, 3, 4, 5):
            raise NotImplementedError
        # Version 1 erhält weiterhin keine erfundene Kalibrierungsbasis.
        # Version 3 erlaubt verschiedene, je Record unverändert validierte
        # Tageszeitzonen. Version 4 ergänzt ausschließlich neue Versuchsdaten;
        # keine Vorgängerversion erhält nachträgliche Kandidaten. Version 5
        # ergänzt nur neue Temperaturvergleiche, keine historischen Modellwerte.
        # Version 6 beginnt ohne rückwirkend erfundene Minderertragshinweise.
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
    persistent_notification.async_dismiss(hass, f"{DOMAIN}.observation.{entry_id}")


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
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}.observation.{entry.entry_id}"
        )


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
    groups = inverter_groups_from_options(entry.options)
    if groups:
        physical["inverter_groups"] = sorted(
            [
                {
                    "id": group.id,
                    "max_power_kw": float(group.max_power_kw),
                    "roof_ids": sorted(group.roof_ids),
                }
                for group in groups
            ],
            key=lambda group: group["id"],
        )
    profiles = (
        {
            roof.id: list(roof.horizon_profile)
            for roof in roofs_from_options(entry.options)
            if roof.horizon_profile
        }
        if entry.options.get(CONF_HORIZON_PROFILES)
        else {}
    )
    if profiles:
        physical["horizon_shading"] = {
            "rule_version": HORIZON_RULE_VERSION,
            "profiles": profiles,
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
        self._capture_configuration_id = _configuration_id(entry)
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
        self._observation_report: dict[str, Any] = {
            "schema_version": 1,
            "status": "off",
            "experimental": True,
        }

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
        # Auch ohne verbliebene Rohpunkte widerlegt eine gespeicherte
        # Integral-Lücke den früheren Messbeleg. Vor jedem Lernstart korrigieren.
        if self._archive.invalidate_derived_gaps(dt_util.utcnow()):
            self._dirty = True
        self._observe(dt_util.utcnow())
        if self.enabled:
            self._running = True
            self._cancel_listener = self.coordinator.async_add_listener(self._updated)
            self._updated()
            if self._dirty:
                self._schedule_save()
        elif self._archive.records and self._archive.note_configuration(
            _configuration_id(self.entry), dt_util.utcnow()
        ):
            # Auch ein pausiertes Archiv darf alte Zielintervalle bei einem
            # Standortwechsel nicht bis zur späteren Wiederaufnahme verlängern.
            self._dirty = True

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
        self._observation_report = {
            "schema_version": 1,
            "status": "insufficient_days",
            "experimental": True,
            "learning_paused": False,
        }
        self._dismiss_observation()
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
                self._dismiss_observation()
                self._observe(dt_util.utcnow())
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
        # Der Options-Reload ist asynchron. Ein inzwischen fertig gewordener
        # Alt-Abruf darf niemals unter den neuen Anlagen-/Quellenparametern
        # eingefroren werden. Erst der neue Laufzeitmanager erfasst sie wieder.
        configuration_id = _configuration_id(self.entry)
        if configuration_id != self._capture_configuration_id:
            return
        now = dt_util.utcnow()
        changed = self._archive.note_configuration(configuration_id, now)
        fetched_at = self.coordinator.last_update_success_time
        calibration = (
            self.calibration.capture_parameters()
            if self.calibration is not None
            else {}
        )
        calibration_signature = tuple(calibration.items())
        if (
            self.coordinator.last_update_success
            and getattr(self.coordinator, "origin", "live") == "live"
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
                configuration_id,
                _configured_measurements(self.entry),
                self._comparison(now),
                inverter_max_power_kw=self.entry.options.get(
                    CONF_INVERTER_MAX_POWER_KW
                ),
                **calibration,
                temperature_forecast=getattr(
                    self.coordinator, "temperature_data", None
                ),
                temperature_mountings=getattr(
                    self.coordinator, "temperature_mountings", None
                ),
                short_term_enabled=self.entry.options.get("short_term_enabled") is True,
                excluded_dates={
                    date.fromisoformat(item["date"])
                    for item in self.entry.options.get("calibration_exclusions", [])
                },
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
                self._observe(now)
                if self.calibration is not None:
                    self.calibration.async_reconcile()
        finally:
            self._assessment_task = None

    @property
    def learning_paused(self) -> bool:
        event = self._archive.underperformance["event"]
        return bool(
            event and event["configuration_id"] == _configuration_id(self.entry)
        )

    @callback
    def _dismiss_observation(self) -> None:
        persistent_notification.async_dismiss(
            self.hass, f"{DOMAIN}.observation.{self.entry.entry_id}"
        )

    @callback
    def _observe(self, now: datetime) -> None:
        enabled = (
            self.enabled and self.entry.options.get("underperformance_enabled") is True
        )
        state = self._archive.underperformance
        if state["event"] and state["event"]["configuration_id"] != _configuration_id(
            self.entry
        ):
            state = self._archive.underperformance = empty_state(now)
            self._dirty = True
            self._schedule_save()
        if not enabled or self.identity_unresolved or self._storage_error is not None:
            self._observation_report = {
                "schema_version": 1,
                "experimental": True,
                "status": "off" if not enabled else "basis_unavailable",
                "learning_paused": self.learning_paused,
            }
            self._dismiss_observation()
            return
        updated, report = observe(
            tuple(self._archive.records.values()),
            state,
            now,
            _configuration_id(self.entry),
            self.timezone,
            {
                date.fromisoformat(item["date"])
                for item in self.entry.options.get("calibration_exclusions", [])
            },
            accepted_factor=getattr(self.coordinator, "calibration_factor", 1.0),
        )
        self._archive.underperformance = updated
        self._observation_report = report
        if updated != state:
            self._dirty = True
            self._schedule_save()
        notifications = self.entry.options.get("underperformance_notifications") is True
        if (
            not notifications
            or report["status"] != "active"
            or updated["event"]["acknowledged"]
        ):
            self._dismiss_observation()
        elif notification_due(updated, now, self.timezone, enabled=notifications):
            # Bewusst keine Haushaltswerte in der HA-weit sichtbaren Mitteilung.
            persistent_notification.async_create(
                self.hass,
                "Ein experimenteller Prüfhinweis liegt vor. Bitte die "
                "berechtigungsgeprüfte PV-Forecast-Karte und die Messquelle "
                "ansehen. Das ist keine Defektdiagnose. Quittierung und "
                "Löschen stehen in den Integrationsoptionen bereit.",
                "PV-Prognose: Vergleich prüfen",
                f"{DOMAIN}.observation.{self.entry.entry_id}",
            )
            updated["event"]["notified"] = True
            self._dirty = True
            self._schedule_save()

    async def async_observation_control(self, action: str) -> None:
        """Einen Hinweis bewusst quittieren oder nach Prüfung neu beginnen."""
        if not self._loaded or self._storage_error is not None:
            raise HomeAssistantError("Das Prognosearchiv ist nicht lesbar")
        if action == "acknowledge":
            if self._archive.underperformance["event"] is not None:
                self._archive.underperformance["event"]["acknowledged"] = True
        elif action == "clear":
            self._archive.underperformance = empty_state(dt_util.utcnow())
        else:
            raise ValueError("Unbekannte Hinweisbedienung")
        self._dismiss_observation()
        self._observe(dt_util.utcnow())
        self._dirty = True
        await self._store.async_save(self._serialize())
        if self.calibration is not None:
            self.calibration.async_reconcile()

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

        result = self._archive.snapshot(
            now or dt_util.utcnow(),
            days,
            include_records,
            configuration_id=_configuration_id(self.entry),
        )
        result.update(
            enabled=self.enabled,
            running=self.running,
            storage_error=self._storage_error,
        )
        result["short_term"] = trial_report(
            tuple(self._archive.records.values()),
            now or dt_util.utcnow(),
            _configuration_id(self.entry),
            {
                date.fromisoformat(item["date"])
                for item in self.entry.options.get("calibration_exclusions", [])
            },
        )
        result["short_term"]["enabled"] = (
            self.enabled and self.entry.options.get("short_term_enabled") is True
        )
        result["temperature_comparison"] = comparison_report(
            tuple(self._archive.records.values()),
            now or dt_util.utcnow(),
            _configuration_id(self.entry),
            mountings_from_options(
                self.entry.options, roofs_from_options(self.entry.options)
            ),
        )
        result["temperature_comparison"]["enabled"] = (
            self.enabled
            and self.entry.options.get("temperature_comparison_enabled") is True
        )
        observation = dict(self._observation_report)
        if self._archive.underperformance["event"] is not None and observation[
            "status"
        ] in ("active", "reference_changed"):
            # Ein zwischenzeitlicher Speicherbeschnitt oder eine Messkorrektur
            # darf keinen alten Messbericht außerhalb seiner Belege ausliefern.
            _, observation = observe(
                tuple(self._archive.records.values()),
                self._archive.underperformance,
                now or dt_util.utcnow(),
                _configuration_id(self.entry),
                self.timezone,
                {
                    date.fromisoformat(item["date"])
                    for item in self.entry.options.get("calibration_exclusions", [])
                },
            )
        elif self._archive.underperformance["event"] is None:
            observation = {
                key: value
                for key, value in observation.items()
                if key
                in (
                    "schema_version",
                    "experimental",
                    "status",
                    "reasons",
                    "learning_paused",
                )
            }
        result["underperformance"] = observation
        if self.calibration is not None:
            result["calibration"] = self.calibration.snapshot()
        return result

    @callback
    def day_view(self, now: datetime, **selection) -> dict[str, Any]:
        """Datierte Archivansicht ohne neue Erfassung oder schwere Bandberechnung."""
        result = self._archive.day_view(now, _configuration_id(self.entry), **selection)
        result.update(
            enabled=self.enabled,
            running=self.running,
            storage_error=self._storage_error,
        )
        if self._storage_error is not None or not self.loaded:
            result.update(
                status="unavailable",
                reason=(
                    "storage_unavailable"
                    if self._storage_error
                    else "archive_not_loaded"
                ),
            )
        return result

    @callback
    def current_targets(self, now: datetime) -> dict[str, Any]:
        """Aktuelle feste Prognoseintervalle ohne neue Erfassung zurückgeben."""

        return self._archive.current_targets(now, _configuration_id(self.entry))

    @callback
    def experience_bands(self, now: datetime) -> dict[str, Any]:
        """Erfahrungsbänder aus demselben geschützten Archiv ohne neue Abrufe lesen."""

        return current_experience_bands(
            self._archive, now, _configuration_id(self.entry)
        )

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
