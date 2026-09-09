"""Bestandskonfiguration und Ablehnung unbekannter Hauptversionen absichern."""

from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import (
    CONF_AZIMUTH,
    CONF_COUNTRY,
    CONF_INSTALLED_POWER_KWP,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LOCATION_NAME,
    CONF_LOCATION_SOURCE,
    CONF_LONGITUDE,
    CONF_LOSS_FACTOR,
    CONF_NAME,
    CONF_POSTAL_CODE,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_STREET,
    CONF_TILT,
    CONF_TIME_ZONE,
    DOMAIN,
    LOCATION_SOURCE_ADDRESS,
)
from custom_components.pv_forecast.models import WeatherInterval


@pytest.fixture(autouse=True)
def fixed_config_date(freezer) -> None:
    """Alle Lifecycle-Abrufe auf denselben bekannten lokalen Tag beziehen."""

    freezer.move_to("2026-08-23T00:00:00+00:00")


def _existing_entry(hass: HomeAssistant, *, version: int = 1) -> MockConfigEntry:
    """Eine vollständig gespeicherte Anlage ohne optionales AC-Limit erzeugen."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=version,
        minor_version=1,
        entry_id="bestehende_pv_anlage",
        unique_id=DOMAIN,
        title="Bestehende PV-Anlage",
        data={
            CONF_LATITUDE: -43.95,
            CONF_LONGITUDE: -176.56,
            CONF_TIME_ZONE: "Pacific/Chatham",
            CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS,
            CONF_LOCATION_NAME: "Gespeicherter Anlagenstandort",
            CONF_STREET: "Gespeicherte Straße 12",
            CONF_POSTAL_CODE: "8942",
            CONF_COUNTRY: "NZ",
        },
        options={
            CONF_ROOFS: [
                {
                    CONF_ROOF_ID: "dach_id_aus_erstinstallation",
                    CONF_NAME: "Süddach mit Bestandswerten",
                    CONF_INSTALLED_POWER_KWP: 6.875,
                    CONF_AZIMUTH: 180,
                    CONF_TILT: 32,
                    CONF_LOSS_FACTOR: 12.375,
                },
                {
                    CONF_ROOF_ID: "zweite_stabile_dach_id",
                    CONF_NAME: "Garage",
                    CONF_INSTALLED_POWER_KWP: 2.125,
                    CONF_AZIMUTH: 270,
                    CONF_TILT: 12,
                    CONF_LOSS_FACTOR: 87.625,
                },
            ]
        },
    )
    entry.add_to_hass(hass)
    return entry


def _stored_fields(entry: MockConfigEntry) -> dict[str, object]:
    """Persistierte Inhalte unabhängig von veränderlichen Laufzeitdaten kopieren."""

    return {
        "entry_id": entry.entry_id,
        "unique_id": entry.unique_id,
        "title": entry.title,
        "version": entry.version,
        "minor_version": entry.minor_version,
        "data": deepcopy(dict(entry.data)),
        "options": deepcopy(dict(entry.options)),
    }


def _entity_identifiers(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """Stabile IDs und tatsächlich registrierte Entity-IDs gemeinsam erfassen."""

    return {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


@pytest.mark.asyncio
async def test_existing_schema_1_1_preserves_configuration_and_entity_ids(
    hass: HomeAssistant,
) -> None:
    """Setup, Unload und erneutes Setup bewahren vorhandene Angaben unverändert."""

    await hass.config.async_set_time_zone("Europe/Berlin")
    entry = _existing_entry(hass)
    original = _stored_fields(entry)
    roof_ids = [roof[CONF_ROOF_ID] for roof in entry.options[CONF_ROOFS]]
    expected_unique_ids = {
        f"{entry.entry_id}_{scope}_{day}"
        for scope in ("total", *roof_ids)
        for day in ("today", "tomorrow")
    } | {
        f"{entry.entry_id}_total_{key}"
        for key in ("remaining_today", "next_60_minutes", "power_now", "peak_today")
    }
    start = datetime(2026, 8, 23, 11, tzinfo=ZoneInfo("Pacific/Chatham"))
    point = WeatherInterval(start, start + timedelta(hours=1), 1000, 25)
    with (
        patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            return_value={roof_id: (point,) for roof_id in roof_ids},
        ) as fetch,
        patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_resolve_timezone"
        ) as resolve_timezone,
        patch(
            "custom_components.pv_forecast.geocoding.NominatimClient.async_geocode"
        ) as geocode,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        first_identifiers = _entity_identifiers(hass, entry)
        assert set(first_identifiers) == expected_unique_ids
        assert all(
            hass.states.get(entity_id) for entity_id in first_identifiers.values()
        )
        assert _stored_fields(entry) == original
        assert CONF_INVERTER_MAX_POWER_KW not in entry.options

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert _stored_fields(entry) == original
        assert _entity_identifiers(hass, entry) == first_identifiers
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert _stored_fields(entry) == original
        assert _entity_identifiers(hass, entry) == first_identifiers
        assert all(
            hass.states.get(entity_id) for entity_id in first_identifiers.values()
        )
        assert fetch.await_count == 2
        for call in fetch.await_args_list:
            assert call.args[:3] == (-43.95, -176.56, "Pacific/Chatham")
            assert [roof.id for roof in call.args[3]] == roof_ids
            assert [roof.loss_fraction for roof in call.args[3]] == [0.12375, 0.87625]
        resolve_timezone.assert_not_awaited()
        geocode.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_future_major_version_is_rejected_without_side_effects(
    hass: HomeAssistant,
) -> None:
    """Home Assistant lehnt unbekannte Hauptversionen vor dem Integrationssetup ab."""

    entry = _existing_entry(hass, version=2)
    original = _stored_fields(entry)
    with (
        patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
        ) as fetch,
        patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_resolve_timezone"
        ) as resolve_timezone,
        patch(
            "custom_components.pv_forecast.geocoding.NominatimClient.async_geocode"
        ) as geocode,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert _stored_fields(entry) == original
    assert not _entity_identifiers(hass, entry)
    assert not hass.states.async_entity_ids("sensor")
    fetch.assert_not_awaited()
    resolve_timezone.assert_not_awaited()
    geocode.assert_not_awaited()
