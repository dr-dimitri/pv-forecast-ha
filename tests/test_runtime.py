"""Tests der gemeinsam genutzten Anbieterpause über neue HA-Clients hinweg."""

from unittest.mock import patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import (
    OpenMeteoRateLimitError,
    OpenMeteoRetryPendingError,
)
from custom_components.pv_forecast.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
    OPEN_METEO_FORECAST_URL,
)
from custom_components.pv_forecast.runtime import async_get_open_meteo_client

from .helpers import persisted_roof
from .test_api import _hourly_payload
from .test_config_flow import _advance_address_to_system


@pytest.mark.asyncio
async def test_new_clients_keep_the_same_provider_deadline(
    hass, aioclient_mock
) -> None:
    """Eine neue Client-Instanz umgeht und verlängert die aktive Pause nicht."""

    aioclient_mock.get(
        OPEN_METEO_FORECAST_URL, status=429, headers={"Retry-After": "7200"}
    )
    with patch(
        "custom_components.pv_forecast.api.monotonic", return_value=100
    ) as clock:
        first = async_get_open_meteo_client(hass)
        with pytest.raises(OpenMeteoRateLimitError):
            await first.async_resolve_timezone(35.6852, 139.7528)
        assert first.retry_after == pytest.approx(7200)
        assert aioclient_mock.call_count == 1

        clock.return_value = 400
        second = async_get_open_meteo_client(hass)
        assert second is not first
        with pytest.raises(OpenMeteoRetryPendingError):
            await second.async_resolve_timezone(35.6852, 139.7528)
        assert first.retry_after == second.retry_after == pytest.approx(6900)
        assert aioclient_mock.call_count == 1

        aioclient_mock.clear_requests()
        aioclient_mock.get(OPEN_METEO_FORECAST_URL, json={"timezone": "Asia/Tokyo"})
        clock.return_value = 7300
        assert await second.async_resolve_timezone(35.6852, 139.7528) == "Asia/Tokyo"
        assert first.retry_after is second.retry_after is None
        assert aioclient_mock.call_count == 1


@pytest.mark.asyncio
async def test_setup_retries_and_unload_preserve_provider_pause(
    hass, aioclient_mock
) -> None:
    """Wiederholtes Erstsetup behält die Schranke, auch beim Entladen eines Eintrags."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={CONF_ROOFS: [persisted_roof()]},
    )
    entry.add_to_hass(hass)
    with (
        freeze_time("2026-08-23T12:00:00+02:00"),
        patch("custom_components.pv_forecast.api.monotonic", return_value=100) as clock,
    ):
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL, status=429, headers={"Retry-After": "7200"}
        )
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        state = hass.data[DOMAIN]
        assert aioclient_mock.call_count == 1

        clock.return_value = 400
        assert not await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert hass.data[DOMAIN] is state
        assert aioclient_mock.call_count == 1
        assert async_get_open_meteo_client(hass).retry_after == pytest.approx(6900)

        aioclient_mock.clear_requests()
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL,
            json=_hourly_payload("2026-08-22T23:00", "2026-08-24T22:00"),
        )
        clock.return_value = 7300
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert aioclient_mock.call_count == 1
        assert async_get_open_meteo_client(hass).retry_after is None

        # Ein späteres Entladen entfernt den Coordinator, aber keine noch aktive
        # Anbieterpause: Auch ein danach erzeugter Client muss weiter warten.
        aioclient_mock.clear_requests()
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL, status=429, headers={"Retry-After": "7200"}
        )
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert coordinator._cancel_midnight is None
        assert hass.data[DOMAIN] is state
        with pytest.raises(OpenMeteoRetryPendingError):
            await async_get_open_meteo_client(hass).async_resolve_timezone(52.52, 13.41)
        assert aioclient_mock.call_count == 1


@pytest.mark.asyncio
async def test_config_flow_retries_respect_metadata_provider_pause(
    hass, aioclient_mock
) -> None:
    """Der Zeitzonentest einer Anschrift teilt seine Pause mit späteren Flow-Clients."""

    await hass.config.async_set_time_zone("Europe/Berlin")
    with (
        freeze_time("2026-08-23T12:00:00+02:00"),
        patch("custom_components.pv_forecast.api.monotonic", return_value=100) as clock,
    ):
        result = await _advance_address_to_system(hass)
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL, status=429, headers={"Retry-After": "7200"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "system"
        assert result["errors"] == {"base": "cannot_connect"}
        assert aioclient_mock.call_count == 1

        clock.return_value = 400
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "system"
        assert result["errors"] == {"base": "cannot_connect"}
        assert aioclient_mock.call_count == 1
        assert async_get_open_meteo_client(hass).retry_after == pytest.approx(6900)

        aioclient_mock.clear_requests()
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL,
            params={"timezone": "auto"},
            json={"timezone": "Asia/Tokyo"},
        )
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL,
            params={"timezone": "UTC"},
            json=_hourly_payload("2026-08-22T16:00", "2026-08-24T15:00"),
        )
        clock.return_value = 7300
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "summary"
        assert result["description_placeholders"]["timezone"] == "Asia/Tokyo"
        assert aioclient_mock.call_count == 2
        assert async_get_open_meteo_client(hass).retry_after is None
