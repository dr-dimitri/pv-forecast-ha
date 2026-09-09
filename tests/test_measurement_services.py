"""Öffentlicher Messdatenvertrag, externe Leserechte und Anlagen-Lebenszyklus."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.measurement_services import (
    SERVICE_GET_MEASUREMENTS,
    async_setup_measurement_services,
)

from .helpers import persisted_roof, weather


@pytest.fixture
async def measured_entry(hass, freezer):
    """Eine echte Integration mit vorhandener externer PV-Ertragsquelle laden."""

    freezer.move_to("2026-09-09T12:00:00+00:00")
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "test", "solar_counter", suggested_object_id="pv_counter"
    )
    attributes = {
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
    }
    hass.states.async_set(entity.entity_id, "100", attributes)
    source = {
        "source_id": "source-a",
        "entity_id": entity.entity_id,
        "registry_id": entity.id,
        "kind": "total",
        "scope": "Gesamte AC-PV-Erzeugung ohne Batterie",
        "derived_energy": False,
        "max_interval_minutes": 60,
        "confirmed_pv": True,
        "confirmed_disjoint": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "Europe/Berlin"},
        options={"roofs": [persisted_roof("a")], "measurement_sources": [source]},
    )
    entry.add_to_hass(hass)
    start = datetime(2026, 9, 8, 22, tzinfo=UTC)
    intervals = tuple(
        weather(end=start + timedelta(hours=hour)) for hour in range(1, 49)
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={"a": intervals},
    ) as fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        freezer.move_to("2026-09-09T12:10:00+00:00")
        hass.states.async_set(entity.entity_id, "101", attributes)
        await hass.async_block_till_done()
        yield entry, entity, fetch
        if hass.config_entries.async_get_entry(entry.entry_id) is not None:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def _read(hass, entry_id, *, user_id=None, **window):
    """Aktion mit ausdrücklichem absolutem Zeitfenster ausführen."""

    return await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_MEASUREMENTS,
        {
            "config_entry_id": entry_id,
            "start": "2026-09-09T12:00:00Z",
            "end": "2026-09-09T12:10:00Z",
            **window,
        },
        blocking=True,
        return_response=True,
        context=Context(user_id=user_id),
    )


async def test_measurement_action_requires_entry_and_absolute_window(hass):
    """Kein implizites Ziel und keine mehrdeutigen lokalen Zeitstempel zulassen."""

    async_setup_measurement_services(hass)
    assert hass.services.supports_response(DOMAIN, SERVICE_GET_MEASUREMENTS) is (
        SupportsResponse.ONLY
    )
    for payload in ({}, {"config_entry_id": "missing"}):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GET_MEASUREMENTS,
                payload,
                blocking=True,
                return_response=True,
            )
    with pytest.raises(vol.Invalid):
        await _read(hass, "missing", start="2026-09-09T12:00:00")
    with pytest.raises(ServiceValidationError) as error:
        await _read(hass, "missing")
    assert error.value.translation_key == "entry_not_found"


async def test_measurement_action_reads_without_fetch_and_preserves_forecast(
    hass, measured_entry
):
    """Lokale Messdaten lesen verändert weder Prognose noch ihren Abrufzustand."""

    entry, _, fetch = measured_entry
    coordinator = entry.runtime_data.coordinator
    forecast = coordinator.data
    fetched_at = coordinator.last_update_success_time
    manager = entry.runtime_data.measurements
    with patch.object(manager, "snapshot", wraps=manager.snapshot) as snapshot:
        result = await _read(hass, entry.entry_id)
    assert result["schema_version"] == 1
    assert result["timezone"] == "Europe/Berlin"
    assert result["sources"]
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert snapshot.call_args.args == (
        datetime(2026, 9, 9, 12, tzinfo=UTC),
        datetime(2026, 9, 9, 12, 10, tzinfo=UTC),
        datetime(2026, 9, 9, 12, 10, tzinfo=UTC),
    )
    assert coordinator.data is forecast
    assert coordinator.last_update_success_time == fetched_at
    assert fetch.await_count == 1


@pytest.mark.parametrize("with_intervals", [False, True])
async def test_measurement_action_day_outlook_is_explicit_local_and_permissioned(
    hass, measured_entry, with_intervals
):
    """Die Tagesaussicht wahrt Quellenrechte und kennzeichnet den fehlenden Morgen."""

    entry, source, fetch = measured_entry
    coordinator = entry.runtime_data.coordinator
    fetched_at = coordinator.last_update_success_time
    forecast = coordinator.data
    request = {"include_outlook": True, "start": "2026-09-08T22:00:00Z"}
    if with_intervals:
        request["interval_windows"] = [
            {"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T12:10:00Z"}
        ]
    assert "outlook" not in await _read(hass, entry.entry_id)
    user = MockUser().add_to_hass(hass)
    permitted = {
        entity.entity_id: {"read": True}
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    user.mock_policy({"entities": {"entity_ids": permitted}})
    with pytest.raises(Unauthorized):
        await _read(hass, entry.entry_id, user_id=user.id, **request)
    permitted[source.entity_id] = {"read": True}
    user.mock_policy({"entities": {"entity_ids": permitted}})
    result = await _read(hass, entry.entry_id, user_id=user.id, **request)
    outlook = result["outlook"]
    assert outlook["schema_version"] == 1
    assert outlook["status"] == "unavailable"
    assert outlook["reason"] == "incomplete_measurements"
    assert result["current_location_total_energy"]["energy_kwh"] == pytest.approx(1)
    assert not result["current_location_total_energy"]["energy_complete"]
    assert outlook["total_kwh"] is None
    assert outlook["remaining_kwh"] is not None
    assert outlook["correction"] == "off"
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert fetch.await_count == 1
    assert coordinator.data is forecast
    assert coordinator.last_update_success_time == fetched_at


async def test_measurement_action_rejects_reversed_window(hass, measured_entry):
    """Ein leeres oder rückwärts laufendes Fenster liefert einen erklärten Fehler."""

    entry, _, _ = measured_entry
    with pytest.raises(ServiceValidationError) as error:
        await _read(hass, entry.entry_id, end="2026-09-09T11:00:00Z")
    assert error.value.translation_key == "invalid_measurement_window"


async def test_measurement_action_requires_external_source_permission(
    hass, measured_entry
):
    """Prognoseleserechte geben keine Leserechte auf einen fremden PV-Zähler."""

    entry, source, _ = measured_entry
    user = MockUser().add_to_hass(hass)
    permitted = {
        entity.entity_id: {"read": True}
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    user.mock_policy({"entities": {"entity_ids": permitted}})
    with pytest.raises(Unauthorized):
        await _read(hass, entry.entry_id, user_id=user.id)
    permitted[source.entity_id] = {"read": True}
    user.mock_policy({"entities": {"entity_ids": permitted}})
    assert (await _read(hass, entry.entry_id, user_id=user.id))["sources"]


async def test_measurement_action_unload_and_entry_removal_clean_up(
    hass, measured_entry
):
    """Unload beendet Messungen; Anlagenentfernung entfernt ihren eigenen Store."""

    entry, _, _ = measured_entry
    manager = entry.runtime_data.measurements
    with patch.object(manager, "async_stop", wraps=manager.async_stop) as stop:
        assert await hass.config_entries.async_unload(entry.entry_id)
    stop.assert_awaited_once()
    with pytest.raises(ServiceValidationError) as error:
        await _read(hass, entry.entry_id)
    assert error.value.translation_key == "entry_not_loaded"
    with patch(
        "custom_components.pv_forecast.async_remove_measurement_store", new=AsyncMock()
    ) as remove:
        assert await hass.config_entries.async_remove(entry.entry_id)
    remove.assert_awaited_once_with(hass, entry.entry_id)


async def test_measurement_action_protects_history_after_source_replacement(
    hass, measured_entry
):
    """Ein neuer Sensor darf die Lesesperre einer früheren Quelle nicht umgehen."""

    entry, old_source, _ = measured_entry
    registry = er.async_get(hass)
    new_source = registry.async_get_or_create("sensor", "test", "new_solar_counter")
    hass.states.async_set(
        new_source.entity_id,
        "20",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
    )
    source_options = {
        **entry.options["measurement_sources"][0],
        "entity_id": new_source.entity_id,
        "registry_id": new_source.id,
    }
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "measurement_sources": [source_options]}
    )
    await hass.async_block_till_done()
    user = MockUser().add_to_hass(hass)
    permitted = {
        entity.entity_id: {"read": True}
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    permitted[new_source.entity_id] = {"read": True}
    user.mock_policy({"entities": {"entity_ids": permitted}})
    with pytest.raises(Unauthorized):
        await _read(hass, entry.entry_id, user_id=user.id)
    permitted[old_source.entity_id] = {"read": True}
    user.mock_policy({"entities": {"entity_ids": permitted}})
    assert (await _read(hass, entry.entry_id, user_id=user.id))["sources"]

    registry.async_remove(old_source.entity_id)
    await hass.async_block_till_done()
    with pytest.raises(Unauthorized):
        await _read(hass, entry.entry_id, user_id=user.id)
    owner = MockUser(is_owner=True).add_to_hass(hass)
    result = await _read(hass, entry.entry_id, user_id=owner.id)
    assert result["sources"][0]["identity_unresolved"]


async def test_cancelled_setup_stops_measurement_listeners(hass, measured_entry):
    """Ein abgebrochenes Plattform-Setup lässt keine lokale Erfassung zurück."""

    from asyncio import CancelledError

    from custom_components.pv_forecast.measurement_runtime import MeasurementManager

    entry, _, _ = measured_entry
    assert await hass.config_entries.async_unload(entry.entry_id)
    manager = MeasurementManager(hass, entry)
    try:
        with (
            patch(
                "custom_components.pv_forecast.MeasurementManager", return_value=manager
            ),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                side_effect=CancelledError,
            ),
        ):
            assert not await hass.config_entries.async_setup(entry.entry_id)
        assert not manager.running
    finally:
        await manager.async_stop()


async def test_interval_windows_are_additive_and_do_not_fetch(hass, measured_entry):
    """Der alte Vertrag bleibt identisch; eine optionale Kurve nutzt dieselben Daten."""

    entry, _, fetch = measured_entry
    old = await _read(hass, entry.entry_id)
    result = await _read(
        hass,
        entry.entry_id,
        interval_windows=[
            {"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T12:10:00Z"},
            {"start": "2026-09-09T12:10:00Z", "end": "2026-09-09T12:20:00Z"},
        ],
    )
    intervals = result.pop("total_intervals")
    assert result == old
    assert intervals[0]["energy_kwh"] == 1
    assert intervals[0]["ac_power_kw"] == 6
    assert intervals[0]["energy_complete"]
    assert intervals[1]["energy_kwh"] is None
    assert "future_window" in intervals[1]["quality_flags"]
    assert fetch.await_count == 1


@pytest.mark.parametrize(
    "windows",
    [
        [{"start": "2026-09-09T12:00:00", "end": "2026-09-09T13:00:00Z"}],
        [{"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T12:00:00Z"}],
        [{"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T11:00:00Z"}],
        [{"start": "2026-09-09T12:00:00Z", "end": "2026-09-11T12:00:01Z"}],
        [
            {"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T13:00:00Z"},
            {"start": "2026-09-09T12:59:00Z", "end": "2026-09-09T14:00:00Z"},
        ],
        [{"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T13:00:00Z"}] * 51,
    ],
)
async def test_interval_windows_validate_bounds_before_reading(hass, windows):
    """Anzahl, UTC-Offsets, Dauer, Überschneidungen und Spanne werden begrenzt."""

    async_setup_measurement_services(hass)
    with pytest.raises(vol.Invalid):
        await _read(hass, "missing", interval_windows=windows)


async def test_interval_windows_preserve_dst_offsets_and_input_order(
    hass, measured_entry
):
    """Zwei gleich bezeichnete Ortsstunden bleiben eigenständige absolute Fenster."""

    entry, _, _ = measured_entry
    result = await _read(
        hass,
        entry.entry_id,
        interval_windows=[
            {"start": "2026-10-25T02:00:00+01:00", "end": "2026-10-25T03:00:00+01:00"},
            {"start": "2026-10-25T02:00:00+02:00", "end": "2026-10-25T02:00:00+01:00"},
        ],
    )
    assert [item["start"] for item in result["total_intervals"]] == [
        "2026-10-25T01:00:00+00:00",
        "2026-10-25T00:00:00+00:00",
    ]
    assert all(item["energy_kwh"] is None for item in result["total_intervals"])


async def test_interval_windows_keep_external_permissions(hass, measured_entry):
    """Die zusätzliche Kurve kann die Lesesperre einer Messquelle nicht umgehen."""

    entry, _, _ = measured_entry
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
    with pytest.raises(Unauthorized):
        await _read(hass, entry.entry_id, user_id=user.id, interval_windows=[])


async def test_source_deleted_while_building_curve_does_not_return_old_copy(
    hass, measured_entry
):
    """Eine Löschung während kooperativer Arbeit entfernt Kurven- und KPI-Messkopien."""

    from custom_components.pv_forecast.measurement_windows import async_interval_windows

    entry, _, _ = measured_entry
    paused, resume = asyncio.Event(), asyncio.Event()
    calls = 0

    async def delayed(histories, windows, now):
        nonlocal calls
        calls += 1
        result = await async_interval_windows(histories, windows, now)
        if calls == 1:
            paused.set()
            await resume.wait()
        return result

    with patch(
        "custom_components.pv_forecast.measurement_runtime.async_interval_windows",
        new=delayed,
    ):
        task = asyncio.create_task(
            _read(
                hass,
                entry.entry_id,
                interval_windows=[
                    {"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T12:10:00Z"}
                ],
            )
        )
        await paused.wait()
        await entry.runtime_data.measurements.async_delete_source_data("source-a")
        resume.set()
        result = await task
    assert calls == 2
    assert result["total_energy"]["energy_kwh"] is None
    assert result["total_intervals"][0]["energy_kwh"] is None


async def test_unload_while_building_curve_rejects_completed_response(
    hass, measured_entry
):
    """Entladen beim Lesen verhindert eine Antwort aus der alten Laufzeit."""

    entry, _, _ = measured_entry
    paused, resume = asyncio.Event(), asyncio.Event()
    manager = entry.runtime_data.measurements
    original = manager.async_interval_windows

    async def delayed(windows, now):
        result = await original(windows, now)
        paused.set()
        await resume.wait()
        return result

    with patch.object(manager, "async_interval_windows", new=delayed):
        task = asyncio.create_task(_read(hass, entry.entry_id, interval_windows=[]))
        await paused.wait()
        assert await hass.config_entries.async_unload(entry.entry_id)
        resume.set()
        with pytest.raises(ServiceValidationError) as error:
            await task
    assert error.value.translation_key == "entry_not_loaded"
