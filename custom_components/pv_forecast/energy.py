"""Die gemeinsame Anlagenprognose für das native Energy Dashboard abbilden."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_TIME_ZONE, DOMAIN
from .forecast_intervals import project_intervals as _project_intervals

if TYPE_CHECKING:
    from homeassistant.components.energy.types import SolarForecastType

    from . import PvForecastConfigEntry


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> SolarForecastType | None:
    """Einen vollständigen aktuellen Stand ohne zusätzlichen Wetterabruf lesen."""

    entry = hass.config_entries.async_get_entry(config_entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        return None

    coordinator = cast("PvForecastConfigEntry", entry).runtime_data.coordinator
    forecast = coordinator.data
    if forecast is None or not coordinator.last_update_success:
        return None
    timezone = ZoneInfo(str(entry.data[CONF_TIME_ZONE]))
    local_date = dt_util.utcnow().astimezone(timezone).date()
    # Ein älterer Ankertag kann beide aktuellen Tage vollständig überdecken.
    start = datetime.combine(local_date, time.min, timezone).astimezone(UTC)
    midnight = datetime.combine(
        local_date + timedelta(days=1), time.min, timezone
    ).astimezone(UTC)
    end = datetime.combine(
        local_date + timedelta(days=2), time.min, timezone
    ).astimezone(UTC)
    intervals = _project_intervals(forecast.total_intervals, start, end)
    if (
        not intervals
        or intervals[0].start.astimezone(UTC) != start
        or intervals[-1].end.astimezone(UTC) != end
        or any(
            not interval.is_complete
            or interval.end.astimezone(UTC) <= interval.start.astimezone(UTC)
            for interval in intervals
        )
        or any(
            previous.end.astimezone(UTC) != following.start.astimezone(UTC)
            for previous, following in pairwise(intervals)
        )
    ):
        # wh_hours kann fehlende Abdeckung oder ältere Daten nicht kennzeichnen.
        return None

    wh_hours: dict[str, float | int] = {}
    for interval in intervals:
        interval_start = interval.start.astimezone(UTC)
        interval_end = interval.end.astimezone(UTC)
        boundaries = [interval_start]
        if interval_start < midnight < interval_end:
            boundaries.append(midnight)
        boundaries.append(interval_end)
        for part_start, part_end in pairwise(boundaries):
            fraction = (part_end - part_start).total_seconds() / (
                interval_end - interval_start
            ).total_seconds()
            energy_wh = interval.energy_kwh * fraction * 1000
            if not math.isfinite(energy_wh) or energy_wh < 0:
                return None
            wh_hours[part_start.isoformat()] = energy_wh

    return {"wh_hours": wh_hours}
