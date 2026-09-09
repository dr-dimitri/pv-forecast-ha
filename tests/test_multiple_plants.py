"""Getrennte logische Anlagen, Duplikate und unveränderte Bestandsdaten prüfen."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.energy import async_get_solar_forecast

from .helpers import persisted_roof
from .test_config_flow import ROOF_FORM
from .test_forecast_horizon import HorizonSession
from .test_measurement_runtime import _source


def existing(hass, *, name="Bestand", latitude=None, unique_id=DOMAIN):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=name,
        unique_id=unique_id,
        version=1,
        minor_version=1,
        data={
            "latitude": hass.config.latitude if latitude is None else latitude,
            "longitude": hass.config.longitude,
            "time_zone": hass.config.time_zone,
            "location_name": name,
            "location_source": "home_assistant",
        },
        options={"roofs": [persisted_roof()]},
    )
    entry.add_to_hass(hass)
    return entry


async def at_location(hass):
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"location_source": "home_assistant"}
    )


async def finish(hass, flow):
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], ROOF_FORM)
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
        flow = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    return flow


async def test_new_site_and_confirmed_independent_same_site_keep_legacy_id(hass):
    old = existing(hass, latitude=10)
    old_data, old_options = dict(old.data), dict(old.options)
    flow = await at_location(hass)
    assert flow["step_id"] == "plant"
    assert "confirm_separate_plant" not in flow["data_schema"].schema
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"plant_name": "Haus"}
    )
    first = (await finish(hass, flow))["result"]
    flow = await at_location(hass)
    assert flow["step_id"] == "plant"
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"plant_name": " haus ", "confirm_separate_plant": True}
    )
    assert flow["errors"] == {"base": "duplicate_plant_name"}
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"plant_name": "Balkon", "confirm_separate_plant": True}
    )
    second = (await finish(hass, flow))["result"]
    assert len({old.unique_id, first.unique_id, second.unique_id}) == 3
    assert old.unique_id == DOMAIN
    assert old.data == old_data and old.options == old_options
    assert first.title.endswith("Haus") and second.title.endswith("Balkon")
    assert first.data["latitude"] == second.data["latitude"]
    assert all(
        (entry.version, entry.minor_version) == (1, 1) for entry in (old, first, second)
    )


async def test_concurrent_same_site_setup_is_blocked_without_http(hass):
    first = await at_location(hass)
    second = await at_location(hass)
    assert first["step_id"] == "roof"
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"


async def test_new_neighbor_before_finish_requires_fresh_confirmation(hass):
    flow = await at_location(hass)
    existing(hass)
    result = await finish(hass, flow)
    assert result["step_id"] == "plant"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_multiple_runtime_stores_and_unload_remove_are_isolated(
    hass, freezer, hass_storage
):
    start = datetime(2026, 9, 9, 17, tzinfo=UTC)
    freezer.move_to(start)
    attrs = {
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
    }
    entries = []
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=HorizonSession(),
    ):
        for index, name in enumerate(("haus", "garten")):
            hass.states.async_set(f"sensor.{name}", "0", attrs)
            entry = MockConfigEntry(
                domain=DOMAIN,
                title=name,
                unique_id=DOMAIN if index == 0 else name,
                version=1,
                minor_version=1,
                data={"latitude": 48 + index, "longitude": 16, "time_zone": "UTC"},
                options={
                    "roofs": [persisted_roof("a")],
                    "inverter_max_power_kw": index + 1,
                    "measurement_sources": [_source(entity_id=f"sensor.{name}")],
                    "history_enabled": True,
                    "calibration_mode": "observe",
                },
                pref_disable_polling=True,
            )
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            entries.append(entry)
    await hass.async_block_till_done()
    first, second = entries
    left, right = first.runtime_data, second.runtime_data
    for attribute in ("coordinator", "measurements", "history", "calibration"):
        assert getattr(left, attribute) is not getattr(right, attribute)
    assert (
        left.coordinator._client._request_state
        is right.coordinator._client._request_state
    )
    left_ids = {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), first.entry_id)
    }
    right_ids = {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), second.entry_id)
    }
    assert len(left_ids) == len(right_ids) == 8
    assert not left_ids & right_ids
    assert left.coordinator.data.total.today == 24
    assert right.coordinator.data.total.today == 48
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set("sensor.haus", "0.25", attrs)
    hass.states.async_set("sensor.garten", "0.5", attrs)
    await hass.async_block_till_done()
    now = start + timedelta(minutes=30)
    assert (
        left.measurements.snapshot(start, now, now)["total_energy"]["energy_kwh"]
        == 0.25
    )
    assert (
        right.measurements.snapshot(start, now, now)["total_energy"]["energy_kwh"]
        == 0.5
    )
    right_before = right.history._archive.to_dict()
    assert await hass.config_entries.async_unload(first.entry_id)
    assert not left.measurements.running and not left.history.running
    assert right.measurements.running and right.history.running
    assert left.coordinator._cancel_minute is None
    assert right.coordinator._cancel_minute is not None
    assert await hass.config_entries.async_remove(first.entry_id)
    assert right.history._archive.to_dict() == right_before
    assert all(first.entry_id not in key for key in hass_storage)
    assert (
        set(
            e.entity_id
            for e in er.async_entries_for_config_entry(
                er.async_get(hass), second.entry_id
            )
        )
        == right_ids
    )
    result = await async_get_solar_forecast(hass, second.entry_id)
    assert sum(result["wh_hours"].values()) == 96_000
    response = await hass.services.async_call(
        DOMAIN,
        "get_forecast",
        {"config_entry_id": second.entry_id},
        blocking=True,
        return_response=True,
    )
    assert response["coverage"]["complete"] is True
    assert await hass.config_entries.async_unload(second.entry_id)
    assert any(second.entry_id in key for key in hass_storage)


async def test_name_claimed_at_other_site_while_setup_is_open_is_checked_again(hass):
    existing(hass, latitude=10)
    flow = await at_location(hass)
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"plant_name": "Garten"}
    )
    existing(hass, latitude=20, name="Garten", unique_id="garden-existing")
    result = await finish(hass, flow)
    assert result["step_id"] == "plant"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_reconfigure_preserves_new_logical_identity_and_name(hass):
    from .test_reconfiguration import FETCH, _begin

    entry = existing(hass, latitude=10, name="Haus", unique_id="stable-plant")
    hass.config_entries.async_update_entry(
        entry, data=dict(entry.data) | {"plant_name": "Haus"}
    )
    before_options = dict(entry.options)
    with patch(FETCH, return_value={}):
        flow = await _begin(hass, entry)
        flow = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"location_source": "home_assistant"}
        )
        with patch.object(hass.config_entries, "async_schedule_reload"):
            flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert flow["reason"] == "reconfigure_successful"
    assert entry.unique_id == "stable-plant"
    assert entry.title == "Haus"
    assert entry.data["plant_name"] == "Haus"
    assert entry.options == before_options
