"""Gemeinsame Aktualisierung und Berechnung aller PV-Prognosen."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, override
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_utc_time_change,
)
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import OpenMeteoClient, OpenMeteoError, OpenMeteoRetryError
from .calculations import (
    InvalidConfigurationError,
    aggregate_energy_for_day,
    apply_calibration,
    calculate_forecast,
    calculate_planning_values,
)
from .configuration import inverter_groups_from_options, roofs_from_options
from .const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_TIME_ZONE,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .explanation import ExplanationSnapshot, build_explanation
from .forecast_intervals import window_energy
from .horizon import forecast_days_from_options
from .models import ForecastDay, ForecastResult, PlanningValues
from .temperature_comparison import COEFFICIENTS, mountings_from_options

if TYPE_CHECKING:
    from .forecast_cache_runtime import ForecastCacheManager

_LOGGER = logging.getLogger(__name__)


class PvForecastCoordinator(TimestampDataUpdateCoordinator[ForecastResult]):
    """Lädt Wetterdaten zentral und erzeugt ein vollständiges Forecast-Ergebnis."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        client: OpenMeteoClient,
    ) -> None:
        """Coordinator initialisieren."""

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self._entry = entry
        self._client = client
        self._cancel_midnight: Callable[[], None] | None = None
        self._cancel_minute: Callable[[], None] | None = None
        self._update_in_progress = False
        self.planning_values: PlanningValues | None = None
        self.raw_data: ForecastResult | None = None
        self.explanation: ExplanationSnapshot | None = None
        self.origin = "live"
        self.restored_at: datetime | None = None
        self.forecast_cache: ForecastCacheManager | None = None
        self.temperature_data: ForecastResult | None = None
        self.temperature_mountings: dict[str, str] | None = None
        self.calibration_factor = 1.0
        self.calibration_candidate_id: str | None = None

    @callback
    @override
    def _async_refresh_finished(self) -> None:
        """Nur ein abgeschlossener echter Abruf ersetzt Herkunft und Cachegeneration."""
        super()._async_refresh_finished()
        if self.last_update_success and not self._shutdown_requested:
            self.origin = "live"
            self.restored_at = None
            if self.forecast_cache is not None:
                self.forecast_cache.async_capture()

    @callback
    def async_set_calibration(self, factor: float, candidate_id: str | None) -> None:
        """Vorhandene Wetterbasis lokal anwenden, ohne den Abrufzustand zu ändern."""

        if (factor, candidate_id) == (
            self.calibration_factor,
            self.calibration_candidate_id,
        ):
            return
        forecast = self.raw_data
        if forecast is not None and (
            factor != self.calibration_factor or self.data is None
        ):
            self.data = apply_calibration(
                forecast,
                factor,
                self._entry.options.get(CONF_INVERTER_MAX_POWER_KW),
                ZoneInfo(str(self._entry.data[CONF_TIME_ZONE])),
            )
        self.calibration_factor = factor
        self.calibration_candidate_id = candidate_id
        self.async_build_explanation()
        self.async_update_listeners()

    @callback
    def async_build_explanation(self, effective: ForecastResult | None = None) -> None:
        """Eine kohärente Roh-/Wirkgeneration lokal und ohne I/O festhalten."""
        current = effective if effective is not None else self.data
        timezone = str(self._entry.data[CONF_TIME_ZONE])
        if (
            self.explanation is not None
            and self.explanation.raw is self.raw_data
            and self.explanation.effective is current
            and self.explanation.factor == self.calibration_factor
            and self.explanation.timezone == timezone
        ):
            return
        self.explanation = build_explanation(
            self.raw_data,
            current,
            self.calibration_factor,
            self._entry.options.get(CONF_INVERTER_MAX_POWER_KW),
            str(self._entry.data[CONF_TIME_ZONE]),
        )

    @callback
    def async_start_day_updates(self) -> None:
        """Nach erfolgreichem Setup genau einen lokalen Tageswechsel planen."""

        if self._cancel_midnight is None:
            self._async_schedule_midnight()

    @callback
    def async_start_planning_updates(self) -> None:
        """Genau einen gemeinsamen Minutentakt für die Planungswerte starten."""

        if self._cancel_minute is None:
            self._cancel_minute = async_track_utc_time_change(
                self.hass, self._async_handle_minute, second=0
            )

    @callback
    def _async_handle_minute(self, _now: datetime) -> None:
        """Gespeicherte Intervalle nachführen, ohne den Abrufzustand zu verändern."""

        if self._cancel_minute is not None:
            self.async_update_listeners()

    @callback
    @override
    def async_update_listeners(self) -> None:
        """Planungswerte einmal für alle Sensoren aus demselben Stand bestimmen."""

        self.planning_values = (
            calculate_planning_values(
                self.data,
                dt_util.utcnow(),
                ZoneInfo(str(self._entry.data[CONF_TIME_ZONE])),
            )
            if self.data is not None
            else None
        )
        super().async_update_listeners()

    @callback
    def _async_schedule_midnight(self) -> None:
        """Die nächste Grenze aus der tatsächlichen Anlagenzeit bestimmen."""

        timezone = ZoneInfo(str(self._entry.data[CONF_TIME_ZONE]))
        local_now = dt_util.utcnow().astimezone(timezone)
        midnight = datetime.combine(
            local_now.date() + timedelta(days=1), time.min, timezone
        ).astimezone(UTC)
        self._cancel_midnight = async_track_point_in_utc_time(
            self.hass, self._async_handle_midnight, midnight
        )

    @callback
    def _async_handle_midnight(self, _now: datetime) -> None:
        """Tageslabels sofort aktualisieren und gemeinsam neue Daten anfordern."""

        if self._cancel_midnight is None:
            return
        self._async_schedule_midnight()
        # Nur die Tagesauswahl hat sich geändert, nicht der Erfolg des Abrufs.
        self.async_update_listeners()
        if (
            self._listeners
            and not self._entry.pref_disable_polling
            and not self._update_in_progress
            and self.get_daily_yield("tomorrow") is None
            and self._client.retry_after is None
        ):
            self._entry.async_create_background_task(
                self.hass,
                self.async_request_refresh(),
                name="PV-Prognose zum lokalen Tageswechsel aktualisieren",
            )

    @override
    async def async_shutdown(self) -> None:
        """Beim Entladen auch die beiden lokalen Fortschreibungstermine entfernen."""

        if self._cancel_midnight is not None:
            self._cancel_midnight()
            self._cancel_midnight = None
        if self._cancel_minute is not None:
            self._cancel_minute()
            self._cancel_minute = None
        await super().async_shutdown()

    @callback
    def get_daily_yield(
        self, day: ForecastDay, roof_id: str | None = None
    ) -> float | None:
        """Den datierten Snapshot auf den aktuellen lokalen Zieltag abbilden."""

        if self.data is None:
            return None
        timezone = ZoneInfo(str(self._entry.data[CONF_TIME_ZONE]))
        target_date = dt_util.utcnow().astimezone(timezone).date()
        if day == "tomorrow":
            target_date += timedelta(days=1)
        if roof_id is None:
            daily = self.data.total
        elif (roof := self.data.roofs.get(roof_id)) is not None:
            daily = roof.daily
        else:
            return None
        if target_date == self.data.local_date:
            return daily.today
        if target_date == self.data.local_date + timedelta(days=1):
            return daily.tomorrow
        if (
            self.data.local_date + timedelta(days=2)
            <= target_date
            < (self.data.local_date + timedelta(days=self.data.forecast_days))
        ):
            # Folgetage behalten ihre UTC-Intervallbasis auch nach Mitternacht.
            # Fehlende Abdeckung darf dabei nicht zu einer Nullprognose werden.
            start = datetime.combine(target_date, time.min, timezone).astimezone(UTC)
            end = datetime.combine(
                target_date + timedelta(days=1), time.min, timezone
            ).astimezone(UTC)
            total = window_energy(self.data.total_intervals, start, end)
            if roof_id is None or total is None:
                return total
            return aggregate_energy_for_day(
                self.data.roofs[roof_id].intervals, target_date, timezone
            )
        return None

    async def _async_update_data(self) -> ForecastResult:
        """Open-Meteo abrufen und die Prognose für alle Dachflächen berechnen."""

        self._update_in_progress = True
        try:
            latitude = float(self._entry.data[CONF_LATITUDE])
            longitude = float(self._entry.data[CONF_LONGITUDE])
            timezone_name = str(self._entry.data[CONF_TIME_ZONE])
            timezone = ZoneInfo(timezone_name)
            forecast_days = forecast_days_from_options(self._entry.options)
            roofs = roofs_from_options(self._entry.options)
            inverter_groups = inverter_groups_from_options(self._entry.options, roofs)
            raw_inverter_limit = self._entry.options.get(CONF_INVERTER_MAX_POWER_KW)
            inverter_limit = (
                float(raw_inverter_limit) if raw_inverter_limit is not None else None
            )
            # Ein über Mitternacht laufender Abruf ergänzt den neuen Zieltag
            # genau einmal. Das gilt auch vor dem Start des Timers beim Setup.
            for _ in range(2):
                requested_date = dt_util.now().astimezone(timezone).date()
                weather_by_roof = await self._client.async_fetch_roofs(
                    latitude,
                    longitude,
                    timezone_name,
                    roofs,
                    local_date=requested_date,
                    **({"forecast_days": forecast_days} if forecast_days != 2 else {}),
                )
                forecast = calculate_forecast(
                    roofs,
                    weather_by_roof,
                    inverter_limit,
                    requested_date,
                    timezone,
                    inverter_groups=inverter_groups,
                    forecast_days=forecast_days,
                    latitude=latitude,
                    longitude=longitude,
                )
                if (
                    self._shutdown_requested
                    or dt_util.now().astimezone(timezone).date() == requested_date
                ):
                    break
            effective = apply_calibration(
                forecast, self.calibration_factor, inverter_limit, timezone
            )
            self.raw_data = forecast
            self.temperature_data = None
            self.temperature_mountings = None
            if (
                self._entry.options.get("temperature_comparison_enabled") is True
                and self._entry.options.get("history_enabled") is True
            ):
                mountings = mountings_from_options(self._entry.options, roofs)
                if mountings is not None:
                    try:
                        self.temperature_data = calculate_forecast(
                            roofs,
                            weather_by_roof,
                            inverter_limit,
                            requested_date,
                            timezone,
                            inverter_groups=inverter_groups,
                            forecast_days=forecast_days,
                            latitude=latitude,
                            longitude=longitude,
                            temperature_coefficients={
                                key: COEFFICIENTS[value]
                                for key, value in mountings.items()
                            },
                        )
                        self.temperature_mountings = mountings
                    except (InvalidConfigurationError, ValueError, OverflowError):
                        # Ein fehlgeschlagener Vergleich ersetzt keine gültige Prognose.
                        self.temperature_data = None
            self.async_build_explanation(effective)
            return effective
        except OpenMeteoRetryError as err:
            raise UpdateFailed(
                f"PV-Prognose pausiert wegen eines vorübergehenden API-Fehlers: {err}",
                retry_after=self._client.retry_after,
            ) from err
        except (
            OpenMeteoError,
            InvalidConfigurationError,
            KeyError,
            ValueError,
            ZoneInfoNotFoundError,
        ) as err:
            raise UpdateFailed(
                f"PV-Prognose konnte nicht aktualisiert werden: {err}"
            ) from err
        finally:
            self._update_in_progress = False
