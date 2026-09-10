"""Lokale Kalibrierung aus rechtzeitigem Archiv ohne weitere Wetterabrufe."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .calibration import CalibrationDay, CalibrationState
from .const import CONF_TIME_ZONE, DOMAIN
from .coordinator import PvForecastCoordinator
from .history_runtime import ArchiveManager, _configuration_id
from .storage import ConfirmedStore

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
MAX_STORAGE_BYTES = 1024 * 1024
SAVE_DELAY = 300


def _calibration_store(hass: HomeAssistant, entry_id: str) -> ConfirmedStore:
    return ConfirmedStore(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.calibration.{entry_id}",
        private=True,
        atomic_writes=True,
    )


async def async_remove_calibration_store(hass: HomeAssistant, entry_id: str) -> None:
    """Beim Entfernen der Anlage auch ihre Lernbelege löschen."""

    await _calibration_store(hass, entry_id).async_remove()


async def async_delete_calibration_data(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Bewusst neu beginnen; auch abhängige Quellen-/Archivlöschungen nutzen dies."""

    manager = getattr(getattr(entry, "runtime_data", None), "calibration", None)
    if manager is not None:
        await manager.async_reset()
    else:
        await async_remove_calibration_store(hass, entry.entry_id)


class CalibrationManager:
    """Reine Lernentscheidungen mit bestehendem Archiv und Coordinator verbinden."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: PvForecastCoordinator,
        history: ArchiveManager,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.history = history
        self.timezone = str(entry.data[CONF_TIME_ZONE])
        self.mode = entry.options.get("calibration_mode", "off")
        self._store = _calibration_store(hass, entry.entry_id)
        self._store.async_track_writes(self._write_finished, SAVE_DELAY)
        self._state = self._new_state()
        self._loaded = False
        self._running = False
        self._stopped = False
        self._reconciling = False
        self._resetting = False
        self._cancel_listener: CALLBACK_TYPE | None = None
        self._storage_error: str | None = None
        self._save_scheduled = False
        self._dirty = False
        self._pending_payload: dict[str, Any] | None = None
        self._last_checked_day: date | None = None
        self.history.calibration = self

    def _new_state(self) -> CalibrationState:
        return CalibrationState(
            _configuration_id(self.entry), dt_util.utcnow(), timezone=self.timezone
        )

    @property
    def storage_error(self) -> str | None:
        """Unlesbare Versionen und wiederholbare Schreibfehler getrennt halten."""
        return self._storage_error or self._store.write_error

    @callback
    def _write_finished(self) -> None:
        if not self._store.write_pending:
            self._dirty = False

    @property
    def prerequisites_met(self) -> bool:
        """Nur ein bewusstes Archiv mit bestätigter AC-Energie trägt das Lernen."""

        return (
            self.entry.options.get("history_enabled") is True
            and self.history.loaded
            and self.history.running
            and any(
                source.get("kind") in ("total", "daily")
                and source.get("confirmed_pv") is True
                and source.get("confirmed_disjoint") is True
                for source in self.entry.options.get("measurement_sources", [])
            )
        )

    async def async_start(self) -> None:
        """Gespeicherten Kandidaten vor jeder Anwendung gegen seine Belege prüfen."""

        if self._loaded or "calibration_mode" not in self.entry.options:
            return
        try:
            stored = await self._store.async_load()
            if stored is not None:
                self._state = CalibrationState.from_dict(stored["state"])
                if (
                    self._state.timezone.key != self.timezone
                    or self._state.configuration_id != _configuration_id(self.entry)
                ):
                    self._state = self._new_state()
        except (
            HomeAssistantError,
            NotImplementedError,
            ValueError,
            TypeError,
            KeyError,
        ):
            self._storage_error = "storage_unavailable"
            _LOGGER.exception("Der lokale Kalibrierungszustand ist nicht lesbar")
            return
        self._loaded = True
        self._running = True
        self._cancel_listener = self.coordinator.async_add_listener(self._updated)
        self.async_reconcile()
        # Der Start des Lernsegments muss auch ohne vollständige Lerntage einen
        # Neustart überstehen; dadurch werden alte Stände nicht nachträglich benutzt.
        self._schedule_save()
        self.history._updated()

    async def async_stop(self) -> None:
        """Listener beenden und den letzten begrenzten Lernzustand sichern."""

        self._running = False
        self._store.async_stop_retries()
        self._stopped = True
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        if (
            self._loaded
            and self._storage_error is None
            and (self._dirty or self._store.write_pending)
        ):
            await self._store.async_save(self._serialize())

    async def async_reset(self) -> None:
        """Sofort zum Grundmodell zurückkehren und frühere Lernreferenzen entfernen."""

        self._resetting = True
        self.coordinator.async_set_calibration(1.0, None)
        try:
            await self._store.async_remove()
            self._state = self._new_state()
            self._storage_error = None
            self._loaded = True
            self._dirty = False
            self._save_scheduled = False
            self._pending_payload = None
            if not self._stopped:
                if not self._running:
                    self._running = True
                    self._cancel_listener = self.coordinator.async_add_listener(
                        self._updated
                    )
                self._schedule_save()
        finally:
            self._resetting = False

    @callback
    def _updated(self) -> None:
        # Neue Messrevisionen stoßen die Prüfung am Ende der Archivbewertung an.
        # Der lokale Tageswechsel prüft zusätzlich Fristen ohne Wetterabruf.
        local_day = dt_util.utcnow().astimezone(ZoneInfo(self.timezone)).date()
        if local_day != self._last_checked_day:
            self.async_reconcile()

    def _days(self) -> list[CalibrationDay]:
        result = []
        for record in self.history._archive.records.values():
            if record.horizon != "daily_previous_18":
                continue
            assessment = record.assessment
            evidence = assessment.to_dict() if assessment is not None else None
            if evidence is not None:
                evidence.pop("assessed_at", None)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "assessment": evidence,
                        "deleted": record.deleted_sources,
                        "model": record.model_version,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            result.append(
                CalibrationDay(
                    record_id=record.record_id,
                    target_date=record.target_date,
                    start=record.start,
                    end=record.end,
                    cutoff=record.cutoff,
                    observed_at=record.observed_at,
                    configuration_id=record.configuration_id,
                    basis=record.basis,
                    raw_energy_kwh=record.raw_energy_kwh,
                    actual_energy_kwh=(
                        assessment.actual_energy_kwh if assessment else None
                    ),
                    valid=bool(
                        assessment
                        and assessment.valid
                        and not record.deleted_sources
                        and self.history._archive._configuration_valid(record)
                    ),
                    quality_flags=record.quality_flags,
                    evidence_fingerprint=fingerprint,
                    candidate_id=record.candidate_id,
                    candidate_energy_kwh=record.candidate_energy_kwh,
                )
            )
        return result

    @callback
    def async_reconcile(self) -> None:
        """Nach echten Archivbewertungen einen Kandidaten prüfen, ohne I/O."""

        if not self._running or self._reconciling or self._resetting:
            return
        self._reconciling = True
        try:
            now = dt_util.utcnow()
            self._last_checked_day = now.astimezone(ZoneInfo(self.timezone)).date()
            previous_capture = self.capture_parameters()
            if self.mode not in ("observe", "auto") or not self.prerequisites_met:
                self.coordinator.async_set_calibration(1.0, None)
                return
            configuration_id = _configuration_id(self.entry)
            if self._state.configuration_id != configuration_id:
                self._state = self._new_state()
                self._schedule_save()
            if self.history.learning_paused:
                self.coordinator.async_set_calibration(1.0, None)
                return
            excluded = tuple(
                date.fromisoformat(item["date"])
                for item in self.entry.options.get("calibration_exclusions", [])
            )
            if self._state.update(
                self._days(),
                now,
                configuration_id,
                self._state.segment_start,
                excluded_dates=excluded,
            ):
                self._schedule_save()
            candidate = self._state.candidate
            factor = (
                self._state.approved_factor
                if self.mode == "auto" and self._storage_error is None
                else 1.0
            )
            self.coordinator.async_set_calibration(
                factor,
                candidate.candidate_id if factor != 1.0 and candidate else None,
            )
            if previous_capture != self.capture_parameters():
                self.history._updated()
        finally:
            self._reconciling = False

    @callback
    def capture_parameters(self) -> dict[str, Any]:
        """Nur den jetzt bekannten Kandidaten zur rechtzeitigen Erfassung anbieten."""

        if (
            not self._running
            or self._resetting
            or self._storage_error is not None
            or self.mode not in ("observe", "auto")
            or not self.prerequisites_met
            or self.history.learning_paused
        ):
            return {}
        candidate = self._state.candidate_for_capture
        return {
            "applied_factor": self.coordinator.calibration_factor,
            "applied_candidate_id": self.coordinator.calibration_candidate_id,
            "trial_factor": candidate.factor if candidate else None,
            "trial_candidate_id": candidate.candidate_id if candidate else None,
        }

    @callback
    def snapshot(self) -> dict[str, Any]:
        """Den bereits geprüften Zustand ohne Lernen oder Schreiben zurückgeben."""

        result = self._state.snapshot()
        result.update(
            mode=self.mode,
            effective_factor=self.coordinator.calibration_factor,
            storage_error=self.storage_error,
            learning_paused=self.history.learning_paused,
        )
        if self.mode == "off":
            result["status"] = "off"
        elif self.storage_error:
            result["status"] = "storage_unavailable"
        elif not self.prerequisites_met:
            result["status"] = "prerequisites_missing"
        elif self.history.learning_paused:
            result["status"] = "underperformance_paused"
        return result

    @callback
    def _schedule_save(self) -> None:
        if not self._running or self._storage_error is not None:
            return
        payload = {"state": self._state.to_dict()}
        if len(json.dumps(payload, allow_nan=False).encode()) > MAX_STORAGE_BYTES:
            self._storage_error = "storage_limit"
            self.coordinator.async_set_calibration(1.0, None)
            return
        self._pending_payload = payload
        self._dirty = True
        if not self._save_scheduled:
            self._save_scheduled = True
            self._store.async_delay_save(self._serialize, SAVE_DELAY)

    @callback
    def _serialize(self) -> dict[str, Any]:
        self._save_scheduled = False
        return self._pending_payload or {"state": self._state.to_dict()}
