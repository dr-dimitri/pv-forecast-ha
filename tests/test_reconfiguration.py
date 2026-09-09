"""Standortkorrekturen mit stabilen IDs und sicheren Speichergrenzen prüfen."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import (
    OpenMeteoConnectionError,
    OpenMeteoDataError,
)
from custom_components.pv_forecast.const import (
    CONF_COUNTRY,
    CONF_LATITUDE,
    CONF_LOCATION_NAME,
    CONF_LOCATION_SOURCE,
    CONF_LONGITUDE,
    CONF_POSTAL_CODE,
    CONF_ROOFS,
    CONF_STREET,
    CONF_TIME_ZONE,
    DOMAIN,
    LOCATION_SOURCE_ADDRESS,
    LOCATION_SOURCE_HOME_ASSISTANT,
)
from custom_components.pv_forecast.models import GeocodedLocation, WeatherInterval

from .helpers import persisted_roof

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
FETCH = "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
RESOLVE = "custom_components.pv_forecast.api.OpenMeteoClient.async_resolve_timezone"


@pytest.fixture(autouse=True)
def fixed_time(freezer):
    freezer.move_to(NOW)


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        unique_id=DOMAIN,
        title="Bestehende Anlage",
        data={
            CONF_LATITUDE: 52.0,
            CONF_LONGITUDE: 13.0,
            CONF_TIME_ZONE: "UTC",
            CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS,
            CONF_LOCATION_NAME: "Bisheriger Standort",
            CONF_COUNTRY: "DE",
            CONF_STREET: "Alte Straße 1",
            CONF_POSTAL_CODE: "10117",
        },
        options={
            CONF_ROOFS: [persisted_roof("stable_roof")],
            "inverter_max_power_kw": 7.5,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _begin(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


async def test_reconfigure_home_location_changes_only_confirmed_location(hass):
    entry = _entry(hass)
    before = deepcopy(dict(entry.data))
    options = deepcopy(dict(entry.options))
    await hass.config.async_set_time_zone("Europe/Berlin")
    with patch(FETCH, return_value={}) as fetch, patch(RESOLVE) as resolve:
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        assert result["step_id"] == "reconfigure_confirm"
        assert dict(entry.data) == before
        fetch.assert_awaited_once()
        resolve.assert_not_awaited()
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            reload.assert_called_once_with(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_LATITUDE] == hass.config.latitude
    assert entry.data[CONF_TIME_ZONE] == "Europe/Berlin"
    assert CONF_STREET not in entry.data and CONF_POSTAL_CODE not in entry.data
    assert entry.unique_id == DOMAIN
    assert entry.title == "Bestehende Anlage"
    assert (entry.version, entry.minor_version) == (1, 1)
    assert dict(entry.options) == options


async def test_reconfigure_address_resolves_zone_once_and_preserves_old_data_on_cancel(
    hass,
):
    entry = _entry(hass)
    before = deepcopy(dict(entry.data))
    with (
        patch(
            "custom_components.pv_forecast.config_flow.NominatimClient.async_geocode",
            return_value=GeocodedLocation(35.6, 139.7, "Tokio"),
        ),
        patch(RESOLVE, return_value="Asia/Tokyo") as resolve,
        patch(FETCH, side_effect=[OpenMeteoConnectionError("offline"), {}]) as fetch,
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_COUNTRY: "JP",
                CONF_POSTAL_CODE: "100-0001",
                CONF_STREET: "Chiyoda 1",
            },
        )
        assert result["errors"] == {"base": "cannot_connect"}
        assert dict(entry.data) == before
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            reload.assert_called_once()
        resolve.assert_awaited_once_with(35.6, 139.7)
        assert fetch.await_count == 2
        assert entry.data[CONF_TIME_ZONE] == "Asia/Tokyo"
    result = await _begin(hass, entry)
    saved = deepcopy(dict(entry.data))
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert dict(entry.data) == saved


@pytest.mark.parametrize(
    "error", [OpenMeteoDataError("invalid"), OpenMeteoConnectionError("offline")]
)
async def test_failed_test_does_not_mutate_or_reload(hass, error):
    entry = _entry(hass)
    before = deepcopy(dict(entry.data))
    with (
        patch(FETCH, side_effect=error),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]
        assert dict(entry.data) == before
        reload.assert_not_called()


@pytest.mark.parametrize("store_name", ["measurements", "history", "calibration"])
async def test_unknown_store_blocks_location_change_without_overwrite(hass, store_name):
    entry = _entry(hass)
    original = deepcopy(dict(entry.data))
    store = Store(hass, 99, f"{DOMAIN}.{store_name}.{entry.entry_id}")
    payload = {"future": {"must_survive": True}}
    await store.async_save(payload)
    with (
        patch(FETCH, return_value={}),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["errors"] == {"base": "reconfigure_storage_unavailable"}
        assert dict(entry.data) == original
        assert await store.async_load() == payload
        reload.assert_not_called()


async def test_real_reload_preserves_registered_entity_ids(hass):
    entry = _entry(hass)
    interval = WeatherInterval(NOW, NOW + timedelta(hours=1), 1000, 25)
    with patch(FETCH, return_value={"stable_roof": (interval,)}) as fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        before = {
            entity.unique_id: entity.entity_id
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        old_runtime = entry.runtime_data
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        with patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=hass.config_entries.async_schedule_reload,
        ) as reload:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            await hass.async_block_till_done()
            reload.assert_called_once_with(entry.entry_id)
        assert result["reason"] == "reconfigure_successful"
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not old_runtime
        after = {
            entity.unique_id: entity.entity_id
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert before == after
        assert fetch.await_count == 3  # Erststart, Test des Entwurfs, Reload.
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_description_only_change_does_not_touch_location_stores(hass):
    entry = _entry(hass)
    hass.config.latitude = 52.0
    hass.config.longitude = 13.0
    await hass.config.async_set_time_zone("UTC")
    with (
        patch(FETCH, return_value={}),
        patch(
            "custom_components.pv_forecast.config_flow.async_prepare_location_change"
        ) as prepare,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["reason"] == "reconfigure_successful"
        prepare.assert_not_awaited()


async def test_parallel_options_change_is_not_overwritten(hass):
    entry = _entry(hass)
    with patch(FETCH, return_value={}):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "inverter_max_power_kw": 8}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure_entry_changed"
    assert entry.options["inverter_max_power_kw"] == 8
    assert entry.data[CONF_LOCATION_NAME] == "Bisheriger Standort"


async def test_failed_store_prepare_restores_the_running_original_entry(hass):
    """Ein Fehler nach Entladen startet die unveränderte alte Anlage wieder."""

    entry = _entry(hass)
    original = deepcopy(dict(entry.data))
    interval = WeatherInterval(NOW, NOW + timedelta(hours=1), 1000, 25)
    with patch(FETCH, return_value={"stable_roof": (interval,)}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        with patch(
            "custom_components.pv_forecast.reconfiguration._async_validate_stores",
            side_effect=[None, OSError("Speicher nicht verfügbar")],
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            await hass.async_block_till_done()
        assert result["errors"] == {"base": "reconfigure_storage_unavailable"}
        assert dict(entry.data) == original
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_unload_keeps_original_location(hass):
    entry = _entry(hass)
    original = deepcopy(dict(entry.data))
    interval = WeatherInterval(NOW, NOW + timedelta(hours=1), 1000, 25)
    with patch(FETCH, return_value={"stable_roof": (interval,)}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        with patch.object(hass.config_entries, "async_unload", return_value=False):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
        assert result["errors"] == {"base": "reconfigure_storage_unavailable"}
        assert dict(entry.data) == original
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_change_during_location_preparation_is_not_overwritten(hass):
    """Auch eine Änderung nach dem ersten Flow-Vergleich bleibt erhalten."""

    entry = _entry(hass)
    changed = dict(entry.data) | {
        CONF_LATITUDE: 45.0,
        CONF_LOCATION_NAME: "Neuerer Stand",
    }

    async def concurrent_change(*args):
        hass.config_entries.async_update_entry(entry, data=changed)

    with (
        patch(FETCH, return_value={}),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await _begin(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT}
        )
        with patch(
            "custom_components.pv_forecast.config_flow.async_prepare_location_change",
            side_effect=concurrent_change,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
        assert result["reason"] == "reconfigure_entry_changed"
        assert dict(entry.data) == changed
        reload.assert_called_once_with(entry.entry_id)
