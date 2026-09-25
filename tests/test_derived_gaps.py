"""Integral-Lücken dürfen keine beobachteten Energiebelege werden."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time

from custom_components.pv_forecast.measurement_helpers import (
    async_resolve_measurement_helpers,
)
from custom_components.pv_forecast.measurement_runtime import MeasurementManager
from custom_components.pv_forecast.measurements import SourceHistory

from .helpers import SOURCE, forecast
from .test_measurement_adapters import POWER, draft, ksem
from .test_measurement_flow import _entry


@pytest.mark.parametrize("derived", [True, False])
@pytest.mark.parametrize("gap", ["restart", "upstream_gap", "unknown"])
def test_only_real_counter_can_prove_energy_across_gap(derived, gap):
    """Nur echte Zähler belegen Energie auch über eine unbeobachtete Pause."""
    source = replace(SOURCE, derived_energy=derived)
    history = SourceHistory(source, "UTC", 20)
    start = datetime(2026, 9, 9, 12, tzinfo=UTC)
    history.add_reading(start, 0, "kWh")
    history.mark_gap(gap)
    history.add_reading(start + timedelta(minutes=10), 0.3, "kWh")
    history.add_reading(start + timedelta(minutes=20), 0.5, "kWh")
    for restored in (
        history,
        SourceHistory.from_dict(source, history.to_dict(), "UTC", 20),
    ):
        result = restored.snapshot(
            start, start + timedelta(minutes=20), start + timedelta(minutes=20)
        )
        assert result["energy_kwh"] == pytest.approx(0.2 if derived else 0.5)
        assert result["energy_complete"] is not derived
        assert result["complete"] is False
        assert ("derived_measurement_gap" in result["quality_flags"]) is derived


@pytest.mark.parametrize("minutes", [60, 1440])
async def test_native_helper_gap_is_excluded_from_measurement_and_outlook(
    hass, minutes
):
    """Die vom nativen Helfer interpolierte Pause bleibt unbeobachtet."""
    start = datetime(2026, 9, 9, tzinfo=UTC)
    end = start + timedelta(minutes=minutes)
    with freeze_time(start - timedelta(minutes=2), real_asyncio=True) as clock:
        _, sensor = ksem(hass)
        sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
        entry = _entry(hass, sources)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "time_zone": "UTC"}
        )
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        try:
            for minute in (-1, 0, 1, 2, *range(20, minutes + 1)):
                clock.move_to(start + timedelta(minutes=minute))
                hass.states.async_set(sensor.entity_id, 1000, POWER)
                await hass.async_block_till_done()
            snapshot = manager.snapshot(start, end, end)
            assert snapshot["total_energy"]["energy_kwh"] == pytest.approx(
                minutes / 60 - 0.3, abs=0.001
            )
            assert snapshot["total_energy"]["energy_complete"] is False
            windows = await manager.async_interval_windows([(start, end)], end)
            assert windows[0]["energy_kwh"] is None
            outlook = manager.day_outlook(
                forecast(), end - timedelta(minutes=1), end - timedelta(minutes=1), True
            )
            assert outlook["status"] == "unavailable"
            assert outlook["reason"] == "incomplete_measurements"
        finally:
            await manager.async_stop()
