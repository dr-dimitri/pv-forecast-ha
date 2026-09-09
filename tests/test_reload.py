"""Konsistente Dachkonfiguration bei Änderungen während eines Reloads prüfen."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pv_forecast.api import OpenMeteoConnectionError
from custom_components.pv_forecast.const import (
    CONF_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_SYSTEM_EFFICIENCY,
    CONF_TILT,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.coordinator import PvForecastCoordinator

from .helpers import persisted_roof, weather


@pytest.fixture(autouse=True)
def fixed_reload_date(freezer) -> None:
    """Reloads ohne zufälligen Tageswechsel ausführen."""

    freezer.move_to("2026-08-23T12:00:00+00:00")


@pytest.fixture
def created_coordinators():
    """Reale Coordinator-Instanzen für die spätere Unload-Prüfung behalten."""

    coordinators = []

    def create(*args, **kwargs):
        coordinator = PvForecastCoordinator(*args, **kwargs)
        coordinators.append(coordinator)
        return coordinator

    with patch(
        "custom_components.pv_forecast.PvForecastCoordinator", side_effect=create
    ):
        yield coordinators


def _entry(hass) -> MockConfigEntry:
    """Anlage mit zwei vor einem Reload bereits bestehenden Dach-IDs erzeugen."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Bestehende PV-Anlage",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={CONF_ROOFS: [persisted_roof("a"), persisted_roof("b", name="Garage")]},
    )
    entry.add_to_hass(hass)
    return entry


def _entity_ids(hass, entry) -> dict[str, str]:
    """Registry-Identität über mehrere vollständige Setups hinweg vergleichen."""

    return {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


@pytest.mark.asyncio
async def test_options_saved_during_reload_trigger_consistent_followup(
    hass, freezer, created_coordinators
) -> None:
    """Ein während des Abrufs gespeichertes Dach gelangt in genau ein Folge-Reload."""

    entry = _entry(hass)
    paused = asyncio.Event()
    release = asyncio.Event()
    requested_roofs = []

    async def fetch(*args, **kwargs):
        roof_ids = {roof.id for roof in args[3]}
        requested_roofs.append(roof_ids)
        if len(requested_roofs) == 2:
            paused.set()
            await release.wait()
        return {roof_id: (weather(),) for roof_id in roof_ids}

    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        side_effect=fetch,
    ) as client_fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old_entity_ids = _entity_ids(hass, entry)
        assert len(old_entity_ids) == 10

        reload_task = hass.async_create_background_task(
            hass.config_entries.async_reload(entry.entry_id),
            "Optionsänderung während eines Reloads testen",
        )
        await paused.wait()
        assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
        options = await hass.config_entries.options.async_init(entry.entry_id)
        options = await hass.config_entries.options.async_configure(
            options["flow_id"], {"next_step_id": "add_roof"}
        )
        result = await hass.config_entries.options.async_configure(
            options["flow_id"],
            {
                CONF_NAME: "Neues Dach",
                CONF_INSTALLED_POWER_KWP: 8.2,
                CONF_AZIMUTH: "south",
                CONF_TILT: 35,
                CONF_SYSTEM_EFFICIENCY: 90,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        release.set()
        assert await reload_task
        await hass.async_block_till_done(wait_background_tasks=True)

        saved_ids = {roof[CONF_ROOF_ID] for roof in entry.options[CONF_ROOFS]}
        new_roof_ids = saved_ids - {"a", "b"}
        assert len(new_roof_ids) == 1
        assert requested_roofs == [{"a", "b"}, {"a", "b"}, saved_ids]
        assert set(entry.runtime_data.coordinator.data.roofs) == saved_ids
        entity_ids = _entity_ids(hass, entry)
        daily_unique_ids = {
            f"{entry.entry_id}_{scope}_{day}"
            for scope in ("total", *saved_ids)
            for day in ("today", "tomorrow")
        }
        assert set(entity_ids) == daily_unique_ids | {
            f"{entry.entry_id}_total_{key}"
            for key in ("remaining_today", "next_60_minutes", "power_now", "peak_today")
        }
        assert len(entity_ids) == 12
        assert old_entity_ids.items() <= entity_ids.items()
        for unique_id, entity_id in entity_ids.items():
            state = hass.states.get(entity_id)
            assert state is not None
            if unique_id in daily_unique_ids:
                assert state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else:
                assert state.state == STATE_UNAVAILABLE
        assert len(entry.update_listeners) == 1
        assert len(created_coordinators) == 3

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not entry.update_listeners
        assert all(
            coordinator._cancel_midnight is None and coordinator._cancel_minute is None
            for coordinator in created_coordinators
        )
        freezer.move_to("2026-08-25T12:00:00+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert client_fetch.await_count == 3


@pytest.mark.asyncio
async def test_failed_setup_cleans_listener_before_successful_retry(
    hass, freezer, created_coordinators
) -> None:
    """Ein gescheitertes Erstsetup hinterlässt weder Optionslistener noch Tagestimer."""

    entry = _entry(hass)
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        side_effect=[OpenMeteoConnectionError("offline"), {}],
    ) as client_fetch:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert not entry.update_listeners
        assert created_coordinators[0]._cancel_midnight is None
        assert created_coordinators[0]._cancel_minute is None

        freezer.move_to("2026-08-23T12:00:06+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED
        assert len(entry.update_listeners) == 1
        assert len(_entity_ids(hass, entry)) == 10
        assert len(created_coordinators) == 2

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not entry.update_listeners
        assert all(
            coordinator._cancel_midnight is None and coordinator._cancel_minute is None
            for coordinator in created_coordinators
        )
        freezer.move_to("2026-08-25T12:00:00+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert client_fetch.await_count == 2
