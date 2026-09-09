"""Lokale Fortschreibung im echten Home-Assistant-Lebenszyklus prüfen."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pv_forecast.models import WeatherInterval

from .test_coordinator import _entry


def _weather():
    """Zwei vollständig gedeckte Tage mit insgesamt 2 kW Anlagenleistung."""

    start = datetime(2026, 8, 22, 22, tzinfo=UTC)
    points = tuple(
        WeatherInterval(
            start + timedelta(hours=hour),
            start + timedelta(hours=hour + 1),
            100,
            25,
        )
        for hour in range(48)
    )
    return {"a": points, "b": points}


@pytest.mark.parametrize("disable_polling", [False, True])
async def test_minute_updates_values_without_changing_fetch_state(
    hass, disable_polling: bool
) -> None:
    """Der Minutentakt verändert ausschließlich die aus dem Snapshot gelesenen Werte."""

    with freeze_time("2026-08-23T10:30:00+02:00") as frozen:
        entry = _entry(hass, disable_polling=disable_polling)
        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            return_value=_weather(),
        ) as fetch:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            snapshot = coordinator.data
            success_time = coordinator.last_update_success_time
            next_refresh = coordinator._unsub_refresh
            assert snapshot.local_date == date(2026, 8, 23)
            assert coordinator.planning_values.remaining_today_kwh == 27
            assert coordinator.planning_values.next_60_minutes_kwh == 2
            listener = Mock()
            unsub = coordinator.async_add_listener(listener)
            coordinator.async_start_planning_updates()
            coordinator.async_start_planning_updates()

            frozen.move_to("2026-08-23T10:31:00+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)

            assert coordinator.planning_values.remaining_today_kwh == pytest.approx(
                27 - 2 / 60
            )
            assert coordinator.planning_values.power_now_kw == 2
            listener.assert_called_once_with()
            assert coordinator.data is snapshot
            assert coordinator.last_update_success
            assert coordinator.last_update_success_time == success_time
            assert coordinator._unsub_refresh is next_refresh
            assert fetch.await_count == 1

            listener.reset_mock()
            assert await hass.config_entries.async_unload(entry.entry_id)
            frozen.move_to("2026-08-23T10:32:00+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)
            listener.assert_not_called()
            assert fetch.await_count == 1
            assert coordinator._cancel_minute is None
            unsub()


async def test_minute_keeps_real_provider_pause_and_error(hass, aioclient_mock) -> None:
    """Nach einem echten HTTP-429 bleibt die Abrufpause auch bei Minutenticks gültig."""

    from custom_components.pv_forecast.const import OPEN_METEO_FORECAST_URL

    from .test_api import _hourly_payload

    with freeze_time("2026-08-23T10:30:00+02:00") as frozen:
        entry = _entry(hass)
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL,
            json=_hourly_payload("2026-08-22T23:00", "2026-08-24T22:00"),
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        success_time = coordinator.last_update_success_time
        snapshot = coordinator.data
        aioclient_mock.clear_requests()
        aioclient_mock.get(
            OPEN_METEO_FORECAST_URL, status=429, headers={"Retry-After": "36000"}
        )
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        error = coordinator.last_exception
        deadline = coordinator._client._request_state._retry_deadline
        next_refresh = coordinator._unsub_refresh

        frozen.move_to("2026-08-23T10:31:00+02:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert aioclient_mock.call_count == 1
        assert not coordinator.last_update_success
        assert coordinator.last_exception is error
        assert coordinator.last_update_success_time == success_time
        assert coordinator.data is snapshot
        assert coordinator._unsub_refresh is next_refresh
        assert coordinator._client._request_state._retry_deadline == deadline
        assert coordinator.planning_values.remaining_today_kwh is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
