"""Rohmodell-Rundreise, Zuordnung und Offline-Lebenszyklus des Neustartcaches."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import OpenMeteoConnectionError
from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.const import CONF_TIME_ZONE
from custom_components.pv_forecast.energy import async_get_solar_forecast
from custom_components.pv_forecast.forecast_cache import (
    CONF_FORECAST_CACHE,
    decode_forecast,
    encode_forecast,
)
from custom_components.pv_forecast.forecast_cache_runtime import ForecastCacheManager
from custom_components.pv_forecast.models import AcInverterGroup

from .helpers import configure_options, persisted_roof, roof, weather

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)


def basis(day=date(2026, 9, 10), zone="UTC", days=2):
    timezone = ZoneInfo(zone)
    start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=days), time.min, timezone).astimezone(
        UTC
    )
    cursor = start.replace(minute=0)
    rows = []
    while cursor < end:
        cursor += timedelta(hours=1)
        rows.append(weather(end=cursor))
    roofs = (roof("a"), roof("b"))
    groups = (AcInverterGroup("group", "Gerät", 12, ("a", "b")),)
    result = calculate_forecast(
        roofs,
        {"a": tuple(rows), "b": tuple(rows)},
        8,
        day,
        timezone,
        inverter_groups=groups,
        forecast_days=days,
    )
    now = start + timedelta(hours=12)
    options = dict(
        entry_id="plant",
        configuration_id="physical",
        timezone_name=zone,
        forecast_days=days,
        roofs=roofs,
        groups=groups,
        limit=8,
        now=now,
    )
    data = encode_forecast(result, now, "plant", "physical", zone, 8)
    return result, data, options


@pytest.mark.parametrize(
    "day,zone",
    [
        (date(2026, 3, 29), "Europe/Berlin"),
        (date(2026, 10, 25), "Europe/Berlin"),
        (date(2026, 9, 10), "Asia/Kathmandu"),
    ],
)
@pytest.mark.parametrize("days", [2, 7])
def test_lossless_raw_roundtrip_and_current_names(day, zone, days):
    original, data, args = basis(day, zone, days)
    loaded = decode_forecast(data, **args)
    assert loaded.forecast == original
    renamed = tuple(replace(r, name="Neuer Name") for r in args["roofs"])
    changed = decode_forecast(data, **(args | {"roofs": renamed}))
    assert changed.forecast.roofs["a"].roof.name == "Neuer Name"
    assert changed.forecast.total_intervals == original.total_intervals
    assert loaded.fetched_at == args["now"]
    assert "name" not in data["roofs"]["a"][0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_version", "future"),
        ("configuration_id", "other"),
        ("timezone", "Asia/Tokyo"),
        ("entry_id", "other"),
        ("forecast_days", 7),
        ("schema_version", True),
        ("inverter_max_power_kw", 10),
    ],
)
def test_foreign_or_incompatible_snapshot(field, value):
    _, data, args = basis()
    data[field] = value
    with pytest.raises(ValueError):
        decode_forecast(data, **args)


@pytest.mark.parametrize(
    "fault",
    [
        "roof",
        "duplicate",
        "dc",
        "ac",
        "energy",
        "nan",
        "bool",
        "future",
        "naive",
        "incomplete",
        "quality",
        "expired",
    ],
)
def test_invalid_raw_or_coverage(fault):
    _, data, args = basis()
    if fault == "roof":
        del data["roofs"]["b"]
    elif fault == "duplicate":
        data["intervals"].append(data["intervals"][0])
    elif fault in ("dc", "ac", "energy"):
        field = {"dc": "dc_power_kw", "ac": "ac_power_kw", "energy": "energy_kwh"}[
            fault
        ]
        data["roofs"]["a"][0][field] = 0
    elif fault in ("nan", "bool"):
        data["intervals"][0]["energy_kwh"] = float("nan") if fault == "nan" else True
    elif fault == "future":
        data["fetched_at"] = (args["now"] + timedelta(seconds=1)).isoformat()
    elif fault == "naive":
        data["fetched_at"] = args["now"].replace(tzinfo=None).isoformat()
    elif fault == "incomplete":
        data["intervals"][0]["is_complete"] = False
    elif fault == "quality":
        data["intervals"][0]["quality_flags"] = ["invented"]
    else:
        args["now"] += timedelta(days=2)
    with pytest.raises(ValueError):
        decode_forecast(data, **args)


def test_size_limit_rejects_without_truncation():
    result, data, args = basis()
    with patch("custom_components.pv_forecast.forecast_cache.MAX_CACHE_BYTES", 1500):
        with pytest.raises(OverflowError):
            encode_forecast(result, args["now"], "plant", "physical", "UTC", 8)
        with pytest.raises(ValueError):
            decode_forecast(data, **args)


def entry_for(hass, enabled=True):
    entry = MockConfigEntry(
        domain="pv_forecast",
        title="PV",
        data={"latitude": 52.52, "longitude": 13.41, CONF_TIME_ZONE: "UTC"},
        options={"roofs": [persisted_roof("a")], CONF_FORECAST_CACHE: enabled},
    )
    entry.add_to_hass(hass)
    return entry


def complete_weather():
    start = NOW.replace(hour=0)
    return {"a": tuple(weather(end=start + timedelta(hours=i)) for i in range(1, 49))}


async def test_restart_offline_preserves_raw_age_failure_and_recovers(
    hass, freezer, hass_storage
):
    freezer.move_to(NOW)
    entry = entry_for(hass)
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value=complete_weather(),
    ) as fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first = entry.runtime_data.coordinator
        raw = first.raw_data
        stored = deepcopy(hass_storage[f"pv_forecast.forecast_cache.{entry.entry_id}"])
        first.async_set_calibration(1.2, None)
        first._async_handle_minute(NOW)
        await hass.async_block_till_done()
        assert hass_storage[f"pv_forecast.forecast_cache.{entry.entry_id}"] == stored
        assert await hass.config_entries.async_unload(entry.entry_id)
        freezer.move_to(NOW + timedelta(minutes=15))
        fetch.side_effect = OpenMeteoConnectionError("offline")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        coordinator = runtime.coordinator
        assert coordinator.raw_data == raw == coordinator.data
        assert coordinator.origin == "restored"
        assert not coordinator.last_update_success
        assert coordinator.last_update_success_time == NOW
        assert coordinator.restored_at == NOW + timedelta(minutes=15)
        assert runtime.history._archive.records == {}
        assert await async_get_solar_forecast(hass, entry.entry_id) is None
        # SAX darf auch einen erst 15 Minuten alten Offline-Cache nicht verwenden.
        from .test_sensor_sax import states

        offline_states = states(hass, entry)
        assert all(state.state == "unavailable" for state in offline_states.values())
        result = await hass.services.async_call(
            "pv_forecast",
            "get_forecast",
            {
                "config_entry_id": entry.entry_id,
                "include_view": True,
                "window": {
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(hours=1)).isoformat(),
                },
            },
            blocking=True,
            return_response=True,
        )
        assert result["origin"] == result["view"]["origin"] == "restored"
        assert result["window"]["reason"] == "stale_forecast"
        fetch.side_effect = None
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.origin == "live"
        assert coordinator.restored_at is None
        assert coordinator.last_update_success_time == NOW + timedelta(minutes=15)
        live_states = states(hass, entry)
        assert all(state.state != "unavailable" for state in live_states.values())
        assert {p: s.entity_id for p, s in offline_states.items()} == {
            p: s.entity_id for p, s in live_states.items()
        }
        assert fetch.call_count == 3
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("enabled", [False, True])
async def test_without_usable_cache_setup_still_retries(hass, freezer, enabled):
    freezer.move_to(NOW)
    entry = entry_for(hass, enabled)
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        side_effect=OpenMeteoConnectionError("offline"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unknown_store_version_cannot_be_overwritten(hass, freezer):
    freezer.move_to(NOW)
    entry = entry_for(hass)
    coordinator = type("Coordinator", (), {"raw_data": basis()[0]})()
    manager = ForecastCacheManager(hass, entry, coordinator)
    manager._store = AsyncMock()
    manager._store.path = "/tmp/nonexistent-pv-cache-test"
    manager._store.async_load.side_effect = NotImplementedError
    assert await manager.async_load() is None
    assert manager.status == "unsupported_version"
    manager.async_capture()
    manager._store.async_save.assert_not_called()


async def test_deletion_waits_for_writing_and_blocks_new_generations(hass, freezer):
    freezer.move_to(NOW)
    entry = entry_for(hass)
    manager = ForecastCacheManager(hass, entry, None)
    began, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def save(data):
        began.set()
        await release.wait()
        calls.append(data)

    manager._store = AsyncMock()
    manager._store.async_save.side_effect = save
    manager._generation = 1
    task = hass.async_create_task(manager._save({"old": True}, 1))
    manager._tasks.add(task)
    await began.wait()
    stop = hass.async_create_task(manager.async_stop(remove=True))
    await asyncio.sleep(0)
    manager._store.async_remove.assert_not_called()
    release.set()
    await stop
    await manager._save({"late": True}, 1)
    assert calls == [{"old": True}]
    manager._store.async_remove.assert_awaited_once()


async def test_cache_option_preserves_concurrent_options_and_removes_only_cache(
    hass, freezer, hass_storage
):
    freezer.move_to(NOW)
    entry = entry_for(hass)
    hass_storage[f"pv_forecast.forecast_cache.{entry.entry_id}"] = {
        "version": 1,
        "data": {},
    }
    hass_storage["pv_forecast.history.other"] = {"version": 6, "data": {"keep": True}}
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await configure_options(
        hass, flow["flow_id"], {"next_step_id": "forecast_cache"}
    )
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"forecast_days": 7}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_FORECAST_CACHE: False}
    )
    assert entry.options["forecast_days"] == 7
    assert f"pv_forecast.forecast_cache.{entry.entry_id}" not in hass_storage
    assert hass_storage["pv_forecast.history.other"]["data"] == {"keep": True}


async def test_newest_pending_generation_wins_and_disabled_cache_stays_removed(
    hass, freezer
):
    freezer.move_to(NOW)
    entry = entry_for(hass)
    manager = ForecastCacheManager(hass, entry, None)
    manager._store = AsyncMock()
    manager._generation = 2
    await manager._save({"generation": 1}, 1)
    await manager._save({"generation": 2}, 2)
    manager._store.async_save.assert_awaited_once_with({"generation": 2})
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {CONF_FORECAST_CACHE: False}
    )
    await manager._save({"generation": 2}, 2)
    assert manager._store.async_save.await_count == 1


async def test_native_store_reports_failed_atomic_write(hass):
    """Der native Store protokolliert Schreibfehler; der Cache behält seinen Status."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.pv_forecast.forecast_cache_runtime import _ForecastStore

    store = _ForecastStore(hass, 1, "pv_forecast.cache.test")
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        side_effect=HomeAssistantError("disk"),
    ):
        with pytest.raises(HomeAssistantError):
            await store._async_write_data({"data": {}})
    assert store.write_status == "storage_unavailable"


async def test_outer_store_envelope_obeys_hard_size_limit(hass):
    from custom_components.pv_forecast.forecast_cache_runtime import _ForecastStore

    store = _ForecastStore(hass, 1, "pv_forecast.cache.test")
    with (
        patch(
            "custom_components.pv_forecast.forecast_cache_runtime.MAX_CACHE_BYTES", 50
        ),
        patch("homeassistant.helpers.storage.Store._async_write_data") as write,
    ):
        await store._async_write_data({"data": "a" * 50})
    assert store.write_status == "storage_limit"
    write.assert_not_called()
