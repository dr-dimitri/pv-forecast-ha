"""Morgenprofile, Quellenrechte und lokale Anwendung im echten HA-Lebenszyklus."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.history_runtime import ArchiveManager
from custom_components.pv_forecast.morning import MorningState

from .helpers import persisted_roof, weather

NOW = datetime(2026, 9, 8, 17, tzinfo=UTC)


@pytest.fixture
async def morning_entry(hass, freezer):
    freezer.move_to(NOW)
    hass.states.async_set("sensor.pv_energy", "0", {"unit_of_measurement": "kWh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "UTC"},
        options={
            "roofs": [persisted_roof("a")],
            "history_enabled": True,
            "morning_mode": "observe",
            "measurement_sources": [
                {
                    "source_id": "pv",
                    "entity_id": "sensor.pv_energy",
                    "kind": "total",
                    "scope": "AC-Gesamtanlage",
                    "confirmed_pv": True,
                    "confirmed_disjoint": True,
                }
            ],
        },
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    start = NOW.replace(hour=0)
    intervals = tuple(weather(end=start + timedelta(hours=i)) for i in range(1, 49))
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={"a": intervals},
    ) as fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, fetch
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_capture_reset_and_restart_never_reconstruct_old_profiles(
    hass, freezer, morning_entry
):
    entry, fetch = morning_entry
    manager = entry.runtime_data.history
    coordinator = entry.runtime_data.coordinator
    assert isinstance(manager._archive.morning, MorningState)
    original = manager._archive.morning.to_dict()
    assert manager._archive.morning.sources
    fetched = coordinator.last_update_success_time
    for minute in range(1, 4):
        freezer.move_to(NOW + timedelta(minutes=minute))
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
    assert coordinator.last_update_success_time == fetched
    assert coordinator.morning_factor == 1
    assert fetch.call_count == 1
    await manager.async_stop()
    restarted = ArchiveManager(
        hass, entry, coordinator, entry.runtime_data.measurements
    )
    try:
        await restarted.async_start()
        assert restarted._archive.morning.segment_start == datetime.fromisoformat(
            original["segment_start"]
        )
        freezer.move_to(NOW.replace(hour=19))
        await restarted.async_reset_morning()
        reset = restarted._archive.morning.to_dict()
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert restarted._archive.morning.cases == {}
        assert (
            restarted._archive.morning.segment_start.isoformat()
            == reset["segment_start"]
        )
        assert fetch.call_count == 1
    finally:
        await restarted.async_stop()


async def test_local_application_and_revocation_preserve_global_factor_and_fetch(
    hass, morning_entry
):
    entry, fetch = morning_entry
    coordinator = entry.runtime_data.coordinator
    fetched = coordinator.last_update_success_time
    raw = coordinator.raw_data
    coordinator.async_set_calibration(1.1, "global")
    baseline = coordinator.data
    coordinator.async_set_morning(0.85, "morning")
    assert coordinator.data != baseline
    assert coordinator.raw_data is raw
    assert coordinator.calibration_factor == 1.1
    coordinator.async_set_morning(1, None)
    assert coordinator.data == baseline
    assert coordinator.last_update_success_time == fetched
    assert coordinator.last_update_success
    assert fetch.call_count == 1


async def test_morning_details_require_sources_without_restricting_forecast(
    hass, morning_entry
):
    entry, fetch = morning_entry
    user = MockUser().add_to_hass(hass)
    user.mock_policy(
        {
            "entities": {
                "entity_ids": {
                    entity.entity_id: {"read": True}
                    for entity in er.async_entries_for_config_entry(
                        er.async_get(hass), entry.entry_id
                    )
                }
            }
        }
    )
    data = {"config_entry_id": entry.entry_id}
    basic = await hass.services.async_call(
        DOMAIN,
        "get_forecast",
        data,
        blocking=True,
        return_response=True,
        context=Context(user_id=user.id),
    )
    assert "metrics" not in basic["morning"]
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "get_forecast",
            data | {"include_morning": True},
            blocking=True,
            return_response=True,
            context=Context(user_id=user.id),
        )
    before = entry.runtime_data.history._archive.to_dict()
    result = await hass.services.async_call(
        DOMAIN,
        "get_forecast",
        data | {"include_morning": True},
        blocking=True,
        return_response=True,
    )
    assert result["morning"]["applied"] is False
    assert result["morning"]["uncertainty"]["status"] == "unavailable"
    assert entry.runtime_data.history._archive.to_dict() == before
    assert fetch.call_count == 1


async def test_delete_active_morning_never_captures_the_old_weather_again(
    hass, morning_entry
):
    entry, fetch = morning_entry
    manager = entry.runtime_data.history
    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_morning(0.8, "approved-morning")
    await hass.async_block_till_done()
    # Die Löschung muss auch einen zu diesem Zeitpunkt angewendeten Faktor beenden.
    coordinator.morning_factor = 0.8
    coordinator.morning_candidate_id = "approved-morning"
    manager._last_calibration_capture = (("morning", "approved-morning"),)
    await manager.async_delete_data()
    await hass.async_block_till_done()
    assert manager._archive.records == {}
    assert manager._archive.morning is None
    assert coordinator.morning_factor == 1
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert manager._archive.records == {}
    assert manager._archive.morning is None or manager._archive.morning.cases == {}
    assert fetch.call_count == 1


async def test_mode_changes_are_local_and_keep_the_global_calibration(
    hass, morning_entry
):
    entry, fetch = morning_entry
    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_calibration(1.1, "global")
    baseline = coordinator.data
    fetched = coordinator.last_update_success_time
    with patch.object(hass.config_entries, "async_reload") as reload:
        for mode in ("auto", "off", "observe"):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, "morning_mode": mode}
            )
            await hass.async_block_till_done()
            assert entry.runtime_data.coordinator is coordinator
            assert coordinator.data == baseline
            assert coordinator.morning_factor == 1
        reload.assert_not_called()
    assert coordinator.calibration_factor == 1.1
    assert coordinator.last_update_success_time == fetched
    assert fetch.call_count == 1


async def test_future_evidence_cannot_enable_automatic_application(hass, morning_entry):
    entry, fetch = morning_entry
    manager = entry.runtime_data.history
    state = manager._archive.morning
    state.status = "approved"
    state.last_updated_at = NOW + timedelta(days=1)
    state.candidate = {
        "id": "future-candidate",
        "factor": 0.8,
        "created_at": state.last_updated_at.isoformat(),
    }
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "morning_mode": "auto"}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.morning_factor == 1
    assert manager.morning_snapshot(NOW)["reasons"] == ["future_evidence"]
    assert fetch.call_count == 1
