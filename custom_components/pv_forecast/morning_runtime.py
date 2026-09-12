"""Opt-in-Morgenprüfung mit dem vorhandenen Archiv und bestätigter AC-Messung."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError

from .const import CONF_LATITUDE, CONF_LONGITUDE, CONF_TIME_ZONE, DOMAIN
from .coordinator import PvForecastCoordinator
from .history_runtime import ArchiveManager, _configuration_id
from .measurements import SourceConfig
from .models import TotalForecastInterval
from .morning import (
    METHOD,
    WINDOW,
    MorningCase,
    MorningLearning,
    instant,
    measured_trace,
    morning_start,
    number,
)
from .storage import ConfirmedStore

_LOGGER = logging.getLogger(__name__)
MAX_BYTES = 2 * 1024 * 1024
STORAGE_VERSION = 1


class _MorningStore(ConfirmedStore):
    """Auch native Einrückung und HA-Hülle gehören zur harten Dateigrenze."""

    def _write_prepared_data(self, mode: str, json_data: str | bytes) -> None:
        size = len(json_data.encode()) if isinstance(json_data, str) else len(json_data)
        if size > MAX_BYTES:
            raise WriteError("Der Morgenspeicher überschreitet seine Dateigrenze")
        super()._write_prepared_data(mode, json_data)


def _store(hass: HomeAssistant, entry_id: str) -> ConfirmedStore:
    return _MorningStore(
        hass, 1, f"{DOMAIN}.morning.{entry_id}", private=True, atomic_writes=True
    )


async def async_remove_morning_store(hass: HomeAssistant, entry_id: str) -> None:
    """Die begrenzten Zusatzbelege bei bewusster Löschung ebenfalls entfernen."""
    await _store(hass, entry_id).async_remove()


async def async_delete_morning_data(hass: HomeAssistant, entry: ConfigEntry) -> None:
    manager = getattr(getattr(entry, "runtime_data", None), "morning", None)
    if manager is not None:
        await manager.async_reset()
    else:
        await async_remove_morning_store(hass, entry.entry_id)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class MorningManager:
    """Ein Lernpfad pro Anlage; keine zusätzlichen Wetter- oder Geräteabrufe."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: PvForecastCoordinator,
        history: ArchiveManager,
    ) -> None:
        self.hass, self.entry, self.coordinator, self.history = (
            hass,
            entry,
            coordinator,
            history,
        )
        self.mode = entry.options.get("morning_mode", "off")
        self.threshold = entry.options.get("morning_threshold_kw", 0.5)
        self.timezone = str(entry.data[CONF_TIME_ZONE])
        self._learning = MorningLearning(dt_util.utcnow(), self.threshold)
        self._context = self._context_id()
        self._records: dict[str, dict[str, Any]] = {}
        self._store = _store(hass, entry.entry_id)
        self._store.async_track_writes(self._write_finished, 300)
        self._resetting = False
        self._running = False
        self._loaded = False
        self._stopped = False
        self._dirty = False
        self._save_scheduled = False
        self._storage_error: str | None = None
        self._cancel: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_assessed: datetime | None = None
        self._last_payload: str | None = None
        self._exclusions: dict[str, int] = {}
        self._generation = 0
        self._applying = False
        self._reconcile_pending = False
        self._proof_pending = False
        self.history.morning = self

    def _context_id(self) -> str:
        return _digest(
            [
                METHOD,
                _configuration_id(self.entry),
                self.threshold,
                self.coordinator.calibration_factor,
            ]
        )

    @property
    def prerequisites_met(self) -> bool:
        sources = [
            s
            for s in self.entry.options.get("measurement_sources", [])
            if s.get("kind") != "power"
        ]
        return bool(
            self.entry.options.get("history_enabled") is True
            and self.history.loaded
            and self.history.running
            and self.history.storage_error is None
            and self.history.measurements is not None
            and self.history.measurements.running
            and not self.history.identity_unresolved
            and not self.history.measurements.identity_unresolved
            and sources
            and all(
                s.get("confirmed_pv") is True and s.get("confirmed_disjoint") is True
                for s in sources
            )
        )

    @callback
    def _write_finished(self) -> None:
        if not self._store.write_pending:
            self._dirty = False
        if self._store.write_error:
            self.coordinator.async_set_morning(0.0, None)

    async def async_start(self) -> None:
        if self._loaded or self._stopped or "morning_mode" not in self.entry.options:
            return
        self._context = self._context_id()
        try:
            data = await self._store.async_load()
            if data is not None:
                validate_morning_store(data)
                if (
                    len(json.dumps(data).encode()) > MAX_BYTES
                    or not isinstance(data["records"], dict)
                    or len(data["records"]) > 90
                ):
                    raise ValueError("Ungültiger Morgenstore")
                if data["context"] == self._context:
                    self._learning = MorningLearning.from_dict(data["learning"])
                    if (
                        self._learning.began > dt_util.utcnow()
                        or self._learning.threshold != self.threshold
                    ):
                        raise ValueError("Ungültiger Lernbeginn")
                    self._records = data["records"]
                    self._validate_records()
        except (
            HomeAssistantError,
            NotImplementedError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
        ):
            self._storage_error = "storage_unavailable"
            _LOGGER.exception("Der lokale Morgenvergleich ist nicht lesbar")
            return
        self._loaded = self._running = True
        self._cancel = self.coordinator.async_add_listener(self._updated)
        self._updated(force=True)
        self._schedule_save()

    def _validate_records(self) -> None:
        for record in self._records.values():
            for key in ("captured_at", "cutoff", "fetched_at", "dawn"):
                instant(record[key])
            if (
                instant(record["captured_at"]) > instant(record["cutoff"])
                or instant(record["captured_at"]) < self._learning.began
                or instant(record["fetched_at"]) > instant(record["captured_at"])
            ):
                raise ValueError("Ungültiger Vorabendbeleg")
            date.fromisoformat(record["target_date"])
            if not isinstance(record["forecast_fingerprint"], str) or (
                record.get("trial_id") is not None
                and not isinstance(record["trial_id"], str)
            ):
                raise ValueError("Ungültiger Kandidatenbeleg")
            if number(record["factor"]) != self.coordinator.calibration_factor:
                raise ValueError("Ungültiger Anlagenfaktor")
            self._trace(record)

    @staticmethod
    def _trace(record: dict[str, Any]) -> tuple[TotalForecastInterval, ...]:
        values = record.get("measured", [])
        if not isinstance(values, list) or len(values) > 96:
            raise ValueError("Ungültige Messspur")
        result = []
        dawn = instant(record["dawn"])
        for left, right, energy in values:
            start, end = instant(left), instant(right)
            energy = number(energy)
            if (
                not dawn <= start < end <= dawn + WINDOW
                or end - start > timedelta(minutes=15)
                or (result and result[-1].end != start)
            ):
                raise ValueError("Ungültige Messgrenzen")
            result.append(
                TotalForecastInterval(
                    start, end, energy, energy * 3600 / (end - start).total_seconds()
                )
            )
        if result and (
            result[0].start - dawn > timedelta(minutes=15)
            or dawn + WINDOW - result[-1].end > timedelta(minutes=15)
        ):
            raise ValueError("Unvollständige Morgenmessung")
        return tuple(result)

    async def async_stop(self) -> None:
        self._running = False
        self._stopped = True
        self._store.async_stop_retries()
        if self._cancel:
            self._cancel()
            self._cancel = None
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if (
            self._loaded
            and self._storage_error is None
            and (self._dirty or self._store.write_pending)
        ):
            await self._store.async_save(self._serialize())

    async def async_reset(self) -> None:
        self._resetting = True
        self._generation += 1
        self._proof_pending = True
        self.coordinator.async_set_morning(0.0, None)
        try:
            if self._task:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            await self._store.async_remove()
            self._records = {}
            self._learning = MorningLearning(dt_util.utcnow(), self.threshold)
            self._context = self._context_id()
            self._storage_error = None
            self._last_payload = None
            self._save_scheduled = False
            self._loaded = True
            if not self._stopped:
                self._running = True
                if self._cancel is None:
                    self._cancel = self.coordinator.async_add_listener(self._updated)
                self._schedule_save()
        except HomeAssistantError:
            self._storage_error = "storage_unavailable"
            raise
        finally:
            self._resetting = False

    @staticmethod
    def _forecast_fingerprint(record: Any) -> str:
        return _digest(
            [
                record.basis.to_dict() if record.basis else None,
                record.configuration_id,
                record.model_version,
                record.fetched_at.isoformat(),
                record.observed_at.isoformat(),
                [s.measurement_identity for s in record.measurement_sources],
            ]
        )

    def _capture(self, now: datetime) -> None:
        if (
            not self.coordinator.last_update_success
            or getattr(self.coordinator, "origin", "live") != "live"
        ):
            return
        for key, record in self.history._archive.records.items():
            if (
                record.horizon != "daily_previous_18"
                or record.basis is None
                or record.quality_flags
                or record.deleted_sources
                or not self._learning.began
                <= record.observed_at
                <= now
                <= record.cutoff
                or record.configuration_id != _configuration_id(self.entry)
                or not timedelta(0) <= now - record.fetched_at <= timedelta(hours=2)
            ):
                continue
            dawn = morning_start(
                record.target_date,
                self.timezone,
                self.entry.data[CONF_LATITUDE],
                self.entry.data[CONF_LONGITUDE],
            )
            if dawn is None or not record.start <= dawn < dawn + WINDOW <= record.end:
                continue
            candidate = self._learning.candidate
            trial = (
                candidate["id"]
                if candidate and instant(candidate["learned_at"]) <= now
                else None
            )
            signature = self._forecast_fingerprint(record)
            previous = self._records.get(key)
            if (
                previous
                and previous["forecast_fingerprint"] == signature
                and previous.get("trial_id") == trial
            ):
                continue
            self._records[key] = {
                "target_date": record.target_date.isoformat(),
                "cutoff": record.cutoff.isoformat(),
                "fetched_at": record.fetched_at.isoformat(),
                "captured_at": now.isoformat(),
                "dawn": dawn.isoformat(),
                "factor": self.coordinator.calibration_factor,
                "forecast_fingerprint": signature,
                "trial_id": trial,
                "trial_coefficient": candidate["coefficient"] if trial else None,
                "measured": [],
            }

    @callback
    def _updated(self, *, force: bool = False) -> None:
        if not self._running or self._resetting or self._applying:
            return
        self._generation += 1
        if force:
            self._proof_pending = True
        if self._context != self._context_id():
            self._context = self._context_id()
            self._learning = MorningLearning(dt_util.utcnow(), self.threshold)
            self._records = {}
            self.coordinator.async_set_morning(0.0, None)
            self._schedule_save()
        if (
            self.mode not in ("observe", "auto")
            or not self.prerequisites_met
            or self.history.learning_paused
            or self.storage_error
        ):
            self.coordinator.async_set_morning(0.0, None)
            return
        now = dt_util.utcnow()
        self._apply_learning(now, allow=not force)
        self._capture(now)
        self._schedule_save()
        if (
            force
            or self._last_assessed is None
            or now - self._last_assessed >= timedelta(minutes=5)
        ):
            if self._task is not None:
                self._reconcile_pending = True
            else:
                self._task = self.entry.async_create_task(
                    self.hass,
                    self._async_assess(),
                    "PV-Morgenvergleich lokal bewerten",
                    eager_start=False,
                )

    def _cases(self, now: datetime) -> list[MorningCase]:
        result = []
        self._exclusions = {}
        excluded = {
            item["date"]
            for item in self.entry.options.get("calibration_exclusions", [])
        }
        for key, saved in self._records.items():
            record = self.history._archive.records.get(key)
            reason = None
            if (
                not record
                or record.deleted_sources
                or record.basis is None
                or not self.history._archive._configuration_valid(record)
                or self._forecast_fingerprint(record) != saved["forecast_fingerprint"]
            ):
                reason = "forecast_or_source_changed"
            elif record.target_date.isoformat() in excluded:
                reason = "known_curtailment"
            elif (
                not record.assessment or not record.assessment.valid or record.end > now
            ):
                reason = "day_measurement_missing"
            elif not saved.get("measured"):
                reason = saved.get("measurement_reason", "morning_measurement_missing")
            if reason:
                self._exclusions[reason] = self._exclusions.get(reason, 0) + 1
                continue
            evidence = record.assessment.to_dict()
            evidence.pop("assessed_at", None)
            fingerprint = _digest(
                [
                    saved["measured"],
                    evidence,
                    saved["forecast_fingerprint"],
                    saved["factor"],
                ]
            )
            result.append(
                MorningCase(
                    key,
                    record.target_date,
                    record.cutoff,
                    fingerprint,
                    instant(saved["dawn"]),
                    record.basis,
                    saved["factor"],
                    self._trace(saved),
                    record.assessment.actual_energy_kwh,
                    (
                        saved.get("trial_id")
                        if self._learning.candidate
                        and saved.get("trial_coefficient")
                        == self._learning.candidate["coefficient"]
                        else None
                    ),
                )
            )
        return result

    async def _async_reconcile(self, now: datetime) -> bool:
        self._proof_pending = True
        self._apply_learning(now)
        today = now.astimezone(ZoneInfo(self.timezone)).date()
        self._records = {
            key: record
            for key, record in self._records.items()
            if date.fromisoformat(record["target_date"]) >= today - timedelta(days=89)
        }
        generation, context = self._generation, self._context
        learning = deepcopy(self._learning)
        cases = self._cases(now)
        # Kandidatenraster und alle Zeitfehler laufen außerhalb des HA-Eventloops.
        # Der Worker besitzt seinen Lernstand und darf Live-Daten nicht verändern.
        await self.hass.async_add_executor_job(learning.update, cases, now, today)
        if (
            not self._running
            or self._resetting
            or generation != self._generation
            or context != self._context_id()
            or not self.prerequisites_met
        ):
            self._reconcile_pending = self._running and not self._resetting
            return False
        self._learning = learning
        self._proof_pending = False
        self._apply_learning(dt_util.utcnow())
        return True

    def _apply_learning(self, now: datetime, *, allow: bool = True) -> None:
        coefficient = (
            self._learning.effective_coefficient
            if allow
            and not self._proof_pending
            and self.mode == "auto"
            and not self.storage_error
            and self.coordinator.last_update_success
            and getattr(self.coordinator, "origin", "live") == "live"
            and self.coordinator.last_update_success_time is not None
            and timedelta(0)
            <= now - self.coordinator.last_update_success_time
            <= timedelta(minutes=60)
            else 0.0
        )
        self._applying = True
        try:
            self.coordinator.async_set_morning(
                coefficient, self._learning.candidate["id"] if coefficient else None
            )
        finally:
            self._applying = False

    async def _async_assess(self) -> None:
        self._reconcile_pending = False
        try:
            now = dt_util.utcnow()
            self._last_assessed = now
            context = self._context
            for key, saved in tuple(self._records.items()):
                record = self.history._archive.records.get(key)
                if (
                    not record
                    or not now - timedelta(days=6) <= record.start < record.end <= now
                ):
                    continue
                dawn = instant(saved["dawn"])
                evidence = await self.history.measurements.async_snapshot(
                    dawn, dawn + WINDOW, now
                )
                if (
                    not self._running
                    or context != self._context_id()
                    or self._records.get(key) is not saved
                    or not self.prerequisites_met
                ):
                    return
                try:
                    expected = {s.source_id: s for s in record.measurement_sources}
                    for source in evidence["sources"]:
                        if source["kind"] == "power":
                            continue
                        for segment in {d["segment_id"] for d in source["deltas"]}:
                            actual = SourceConfig.from_dict(source["segments"][segment])
                            if (
                                actual.measurement_identity
                                != expected[source["source_id"]].measurement_identity
                            ):
                                raise ValueError("measurement_identity_unresolved")
                    trace = measured_trace(evidence, set(expected), dawn)
                    saved["measured"] = [
                        [i.start.isoformat(), i.end.isoformat(), i.energy_kwh]
                        for i in trace
                    ]
                    saved.pop("measurement_reason", None)
                except (KeyError, TypeError, ValueError, OverflowError):
                    # Ein erneut geprüfter, nun mangelhafter Zeitbeleg darf keine
                    # alte Freigabe erhalten. Außerhalb der Rohfrist bleibt die Kopie.
                    saved["measured"] = []
                    saved["measurement_reason"] = "morning_measurement_incomplete"
            if self._running and self.prerequisites_met:
                if await self._async_reconcile(now):
                    self._capture(dt_util.utcnow())
                    self._schedule_save()
        finally:
            self._task = None
            if self._reconcile_pending and self._running and not self._resetting:
                self._updated(force=True)

    @property
    def storage_error(self) -> str | None:
        return self._storage_error or self._store.write_error

    def snapshot(self, *, public: bool = False) -> dict[str, Any]:
        report = dict(self._learning.report)
        if self.mode == "off":
            report["status"] = "off"
        elif self.storage_error:
            report["status"] = "storage_unavailable"
        elif not self.prerequisites_met:
            report["status"] = "prerequisites_missing"
        elif self.history.learning_paused:
            report["status"] = "learning_paused"
        report["reasons"] = [] if report["status"] == "approved" else [report["status"]]
        report.update(
            schema_version=1,
            mode=self.mode,
            method=METHOD,
            primary_metric="rise_time_mae_minutes",
            threshold_ac_kw=self.threshold,
            window_hours_after_sunrise=4,
            stability_minutes=60,
            max_measurement_interval_minutes=15,
            boundary_tolerance_minutes=15,
            forecast_cutoff="previous_day_18_local",
            application_days="today_and_tomorrow_only",
            validation_scope="frozen_previous_day_18_not_every_live_lead",
            forecast_model_issued_at=None,
            measurement_timestamp="ha_reported_at_not_verified_device_time",
            assumption="constant_interval_mean_power_not_continuous_minimum",
            applied=getattr(
                self.coordinator,
                "morning_applied",
                self.coordinator.morning_coefficient != 0,
            ),
            effective_coefficient=self.coordinator.morning_coefficient,
            applied_candidate_id=self.coordinator.morning_candidate_id,
            uncertainty={"status": "unavailable", "reason": "unsupported_horizon"},
            exclusion_reasons=dict(self._exclusions),
        )
        if public:
            for key in ("raw", "baseline", "candidate", "exclusion_reasons"):
                report.pop(key, None)
        return report

    def _schedule_save(self) -> None:
        if not self._running or self._storage_error:
            return
        payload = {
            "context": self._context,
            "learning": self._learning.to_dict(),
            "records": self._records,
        }
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded.encode()) > MAX_BYTES:
            self._storage_error = "storage_limit"
            self.coordinator.async_set_morning(0.0, None)
            return
        if encoded == self._last_payload:
            return
        self._last_payload = encoded
        self._dirty = True
        if not self._save_scheduled:
            self._save_scheduled = True
            self._store.async_delay_save(self._serialize, 300)

    def _serialize(self) -> dict[str, Any]:
        self._save_scheduled = False
        return (
            json.loads(self._last_payload)
            if self._last_payload
            else {
                "context": self._context,
                "learning": self._learning.to_dict(),
                "records": self._records,
            }
        )


def validate_morning_store(data: dict[str, Any]) -> None:
    """Einen gespeicherten alten Standortkontext ohne Manager oder I/O prüfen."""
    if (
        not isinstance(data.get("context"), str)
        or not isinstance(data.get("records"), dict)
        or len(data["records"]) > 90
        or len(json.dumps(data, allow_nan=False).encode()) > MAX_BYTES
    ):
        raise ValueError("Ungültiger Morgenspeicher")
    learning = MorningLearning.from_dict(data["learning"])
    for record in data["records"].values():
        cutoff = instant(record["cutoff"])
        captured = instant(record["captured_at"])
        if (
            not learning.began <= captured <= cutoff
            or instant(record["fetched_at"]) > captured
            or not isinstance(record["forecast_fingerprint"], str)
            or not 0.5 <= number(record["factor"]) <= 1.5
        ):
            raise ValueError("Ungültiger Morgenbeleg")
        trial_id, coefficient = record.get("trial_id"), record.get("trial_coefficient")
        if (trial_id is None) != (coefficient is None) or (
            trial_id is not None
            and (
                not isinstance(trial_id, str)
                or type(coefficient) is bool
                or coefficient
                not in (-0.5, -0.4, -0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3, 0.4, 0.5)
            )
        ):
            raise ValueError("Ungültig eingefrorener Morgenkandidat")
        MorningManager._trace(record)
