"""Morgenvergleich im vorhandenen Archiv und gemeinsamen lokalen Aktualisierungstakt."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import CONF_LATITUDE, CONF_LONGITUDE
from .morning import MorningState

if TYPE_CHECKING:
    from .history_runtime import ArchiveManager


def _known_at(state: MorningState, now: datetime) -> bool:
    """Ein erst künftig belegter Zustand kann heute keine Freigabe rechtfertigen."""
    return (
        state.segment_start <= now
        and (state.last_updated_at is None or state.last_updated_at <= now)
        and (
            state.candidate is None
            or datetime.fromisoformat(state.candidate["created_at"]) <= now
        )
    )


class MorningRuntime:
    """Kein eigener Store, Listener, Zeitgeber oder Wetterzugriff."""

    def async_update_morning_options(self: ArchiveManager) -> bool:
        """Eine reine Moduswahl sofort und ohne fachlichen Reload übernehmen."""
        from .dashboard import DASHBOARD_OPTIONS

        ignored = DASHBOARD_OPTIONS | {"morning_mode"}
        if self._morning_original_data != dict(self.entry.data) or {
            key: value
            for key, value in self._morning_original_options.items()
            if key not in ignored
        } != {
            key: value
            for key, value in self.entry.options.items()
            if key not in ignored
        }:
            return False
        old_mode = self._morning_original_options.get("morning_mode", "off")
        mode = self.entry.options.get("morning_mode", "off")
        if old_mode != mode:
            self._morning_original_options["morning_mode"] = mode
            now = dt_util.utcnow()
            self._reconcile_morning(now)
            if self._capture_morning(now):
                self._dirty = True
                self._schedule_save()
        return True

    def _morning_prerequisites(self: ArchiveManager) -> bool:
        from .history_runtime import _configured_measurements

        return bool(
            self.enabled
            and self._loaded
            and not self._stopped
            and self.storage_error is None
            and self.measurements is not None
            and self.measurements.storage_error is None
            and not self.measurements.identity_unresolved
            and any(
                source.kind != "power"
                for source in _configured_measurements(self.entry)
            )
        )

    def _morning_state(self: ArchiveManager, now: datetime) -> MorningState:
        from .history_runtime import _configuration_id

        configuration_id = _configuration_id(self.entry)
        state = self._archive.morning
        if state is None or state.configuration_id != configuration_id:
            state = self._archive.morning = MorningState(
                configuration_id, now, self.timezone
            )
            self._dirty = True
        return state

    def _capture_morning(self: ArchiveManager, now: datetime) -> bool:
        """Nur live beobachtete vollständige Profile vor dem Stichtag einfrieren."""
        from .history_runtime import _configured_measurements

        coordinator = self.coordinator
        if (
            self.entry.options.get("morning_mode", "off") == "off"
            or not self._morning_prerequisites()
            or not coordinator.last_update_success
            or getattr(coordinator, "origin", "live") != "live"
            or coordinator.raw_data is None
            or coordinator.last_update_success_time is None
            or (
                self._morning_fresh_after is not None
                and coordinator.last_update_success_time <= self._morning_fresh_after
            )
        ):
            return False
        return self._morning_state(now).capture(
            coordinator.raw_data,
            coordinator.last_update_success_time,
            now,
            _configured_measurements(self.entry),
            latitude=float(self.entry.data[CONF_LATITUDE]),
            longitude=float(self.entry.data[CONF_LONGITUDE]),
            inverter_max_power_kw=self.entry.options.get("inverter_max_power_kw"),
            global_factor=coordinator.calibration_factor,
        )

    def _reconcile_morning(self: ArchiveManager, now: datetime) -> None:
        """Messbelege prüfen und eigenständig freigegebene Automatik anwenden."""
        if getattr(self, "_morning_reconciling", False):
            return
        self._morning_reconciling = True
        try:
            mode = self.entry.options.get("morning_mode", "off")
            factor, candidate_id = 1.0, None
            if mode != "off" and self._morning_prerequisites():
                state = self._morning_state(now)
                changed = state.reconcile(
                    tuple(
                        history.current_location_view()
                        for history in self.measurements._histories.values()
                    ),
                    tuple(self._archive.records.values()),
                    now,
                    excluded_dates={
                        date.fromisoformat(item["date"])
                        for item in self.entry.options.get("calibration_exclusions", [])
                    },
                )
                if changed:
                    self._dirty = True
                    self._schedule_save()
                if (
                    mode == "auto"
                    and _known_at(state, now)
                    and not self.learning_paused
                    and getattr(self.coordinator, "origin", "live") == "live"
                ):
                    factor = state.approved_factor
                    candidate_id = state.snapshot(mode=mode).get("candidate_id")
                    if factor == 1:
                        candidate_id = None
            self.coordinator.async_set_morning(factor, candidate_id)
        finally:
            self._morning_reconciling = False

    def morning_snapshot(self: ArchiveManager, now: datetime) -> dict[str, Any]:
        """Nur lesen; Stichproben bleiben hinter den bestehenden Quellenrechten."""
        from .history_runtime import _configuration_id

        mode = self.entry.options.get("morning_mode", "off")
        state = self._archive.morning
        report = (
            state.snapshot(mode=mode)
            if state is not None
            and state.configuration_id == _configuration_id(self.entry)
            else MorningState(
                _configuration_id(self.entry), now, self.timezone
            ).snapshot(mode=mode)
        )
        report = dict(report)
        if state is not None and not _known_at(state, now):
            report.update(status="invalidated", reasons=["future_evidence"])
        if mode != "off" and not self._morning_prerequisites():
            report.update(
                status="prerequisites_missing", reasons=["prerequisites_missing"]
            )
        report.update(
            mode=mode,
            applied=self.coordinator.morning_factor != 1,
            effective_factor=self.coordinator.morning_factor,
        )
        return report

    async def async_reset_morning(self: ArchiveManager) -> None:
        """Bewusst neu beginnen, ohne alte Morgenprofile rückwirkend zu übernehmen."""
        from .history_runtime import _configuration_id

        if (
            not self._loaded
            or self.storage_error is not None
            or self._stopped
            or self._mutation_in_progress
        ):
            raise HomeAssistantError("Das Prognosearchiv ist nicht verfügbar")
        self._mutation_in_progress = True
        try:
            await self._async_cancel_assessment()
            self._morning_fresh_after = dt_util.utcnow()
            self._archive.morning = MorningState(
                _configuration_id(self.entry), dt_util.utcnow(), self.timezone
            )
            self.coordinator.async_set_morning(1.0, None)
            self._dirty = True
            await self._store.async_save_checked(self._serialize())
        finally:
            self._mutation_in_progress = False
