"""Tests der Coordinator-Orchestrierung."""

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pv_forecast.api import (
    OpenMeteoConnectionError,
    OpenMeteoDataError,
    OpenMeteoError,
)
from custom_components.pv_forecast.const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    RoofForecast,
)

from .helpers import TIMEZONE, persisted_roof, roof, weather


def _entry(
    hass, timezone: str = "Europe/Berlin", *, disable_polling: bool = False
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: timezone,
        },
        options={
            CONF_ROOFS: [persisted_roof("a"), persisted_roof("b")],
            CONF_INVERTER_MAX_POWER_KW: 15,
        },
        pref_disable_polling=disable_polling,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_coordinator_uses_one_shared_client_update(hass) -> None:
    """Alle Dächer werden aus genau einem gebündelten Client-Aufruf berechnet."""

    client = AsyncMock()
    client.async_fetch_roofs.return_value = {
        "a": (weather(),),
        "b": (weather(),),
    }
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with patch(
        "custom_components.pv_forecast.coordinator.dt_util.now",
        return_value=datetime(2026, 8, 23, 10, tzinfo=TIMEZONE),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert client.async_fetch_roofs.await_count == 1
    assert set(coordinator.data.roofs) == {"a", "b"}
    assert coordinator.data.total.today == pytest.approx(15)


@pytest.mark.asyncio
async def test_coordinator_converts_api_error_to_update_failed(hass) -> None:
    """Externe Fehler werden in den HA-Coordinator-Lebenszyklus übersetzt."""

    client = AsyncMock()
    client.async_fetch_roofs.side_effect = OpenMeteoConnectionError("offline")
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        OpenMeteoConnectionError("offline"),
        OpenMeteoDataError("Zeitreihe unvollständig"),
    ],
)
async def test_coordinator_retains_valid_forecast_after_failed_update(
    hass, error: OpenMeteoError
) -> None:
    """Unbrauchbare Antworten ersetzen keinen zuvor gültigen Tagesforecast."""

    client = AsyncMock()
    client.async_fetch_roofs.return_value = {"a": (weather(),), "b": (weather(),)}
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with patch(
        "custom_components.pv_forecast.coordinator.dt_util.now",
        return_value=datetime(2026, 8, 23, 10, tzinfo=TIMEZONE),
    ):
        await coordinator.async_refresh()
        previous = coordinator.data
        assert previous.total.today == pytest.approx(15)
        client.async_fetch_roofs.side_effect = error
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert coordinator.data is previous


def _snapshot(local_day: date) -> ForecastResult:
    """Datierter Zweitagesstand mit unterscheidbaren Dachwerten."""

    return ForecastResult(
        local_date=local_day,
        roofs={
            "a": RoofForecast(roof("a"), (), DailyYield(5, 8)),
            "b": RoofForecast(roof("b"), (), DailyYield(7, 12)),
        },
        total=DailyYield(12, 20),
    )


def _two_day_weather(local_day: date, today: float = 250, tomorrow: float = 500):
    """Unterscheidbare Tageswerte für den gemockten gebündelten Client liefern."""

    points = (
        weather(
            today,
            end=datetime.combine(local_day, datetime.min.time(), TIMEZONE)
            + timedelta(hours=12),
        ),
        weather(
            tomorrow,
            end=datetime.combine(
                local_day + timedelta(days=1), datetime.min.time(), TIMEZONE
            )
            + timedelta(hours=12),
        ),
    )
    return {"a": points, "b": points}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "total_today", "total_tomorrow", "roof_today", "roof_tomorrow"),
    [
        ("2026-08-23T12:00:00+02:00", 12, 20, 5, 8),
        ("2026-08-24T00:00:00+02:00", 20, None, 8, None),
        ("2026-08-25T00:00:00+02:00", None, None, None, None),
    ],
)
async def test_daily_yield_exposes_only_covered_target_days(
    hass, now, total_today, total_tomorrow, roof_today, roof_tomorrow
) -> None:
    """Nur explizit im datierten Stand vorhandene Tage erhalten einen Zahlenwert."""

    coordinator = PvForecastCoordinator(hass, _entry(hass), AsyncMock())
    snapshot = _snapshot(date(2026, 8, 23))
    coordinator.async_set_updated_data(snapshot)
    with freeze_time(now):
        assert coordinator.get_daily_yield("today") == total_today
        assert coordinator.get_daily_yield("tomorrow") == total_tomorrow
        assert coordinator.get_daily_yield("today", "a") == roof_today
        assert coordinator.get_daily_yield("tomorrow", "a") == roof_tomorrow
    assert coordinator.data is snapshot


@pytest.mark.asyncio
async def test_request_crossing_midnight_fetches_current_day_once(hass) -> None:
    """Ein Datumswechsel erhält die Bindung und ergänzt genau einen neuen Abruf."""

    client = AsyncMock()
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with freeze_time("2026-08-23T23:59:59+02:00") as frozen:

        async def fetch(*args, **kwargs):
            frozen.move_to("2026-08-24T00:00:01+02:00")
            return _two_day_weather(kwargs["local_date"], 300, 400)

        client.async_fetch_roofs.side_effect = fetch
        await coordinator.async_refresh()
        assert [
            call.kwargs["local_date"]
            for call in client.async_fetch_roofs.await_args_list
        ] == [date(2026, 8, 23), date(2026, 8, 24)]
        assert coordinator.data.local_date == date(2026, 8, 24)
        assert coordinator.data.total == DailyYield(6, 8)
        assert coordinator.get_daily_yield("today") == 6
        assert coordinator.get_daily_yield("tomorrow") == 8


@pytest.mark.asyncio
async def test_first_setup_crossing_midnight_starts_with_both_current_days(
    hass,
) -> None:
    """Das Erstsetup ergänzt den neuen Zieltag schon vor dem Start des Tagestimers."""

    with freeze_time("2026-08-23T23:59:59+02:00") as frozen:
        entry = _entry(hass)

        async def fetch(*args, **kwargs):
            frozen.move_to("2026-08-24T00:00:01+02:00")
            return _two_day_weather(kwargs["local_date"], 300, 400)

        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            side_effect=fetch,
        ) as client_fetch:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            assert [
                call.kwargs["local_date"] for call in client_fetch.await_args_list
            ] == [date(2026, 8, 23), date(2026, 8, 24)]
            assert coordinator.get_daily_yield("today") == 6
            assert coordinator.get_daily_yield("tomorrow") == 8
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion_seconds", [1, 11], ids=["kurz", "laenger-als-debouncer"]
)
async def test_inflight_midnight_update_has_one_sequential_followup(
    hass, completion_seconds: int
) -> None:
    """Ein laufender Abruf ergänzt den neuen Tag ohne parallele oder dritte Abfrage."""

    started = asyncio.Event()
    release = asyncio.Event()
    active_calls = 0
    maximum_active_calls = 0
    with freeze_time("2026-08-23T23:59:00+02:00") as frozen:
        entry = _entry(hass)
        call_dates = []

        async def fetch(*args, **kwargs):
            nonlocal active_calls, maximum_active_calls
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
            requested_day = kwargs["local_date"]
            call_dates.append(requested_day)
            try:
                if len(call_dates) == 2:
                    started.set()
                    await release.wait()
                return _two_day_weather(requested_day, 300, 400)
            finally:
                active_calls -= 1

        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            side_effect=fetch,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            running = entry.async_create_background_task(
                hass, coordinator.async_refresh(), name="Langsamen Abruf testen"
            )
            await started.wait()
            frozen.move_to("2026-08-24T00:00:00+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            frozen.move_to(
                datetime(2026, 8, 24, tzinfo=TIMEZONE)
                + timedelta(seconds=completion_seconds)
            )
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            release.set()
            await running
            await hass.async_block_till_done(wait_background_tasks=True)
            frozen.move_to("2026-08-24T00:00:30+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)

            assert call_dates == [
                date(2026, 8, 23),
                date(2026, 8, 23),
                date(2026, 8, 24),
            ]
            assert maximum_active_calls == 1
            assert coordinator.data.local_date == date(2026, 8, 24)
            assert coordinator.get_daily_yield("tomorrow") == 8
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_midnight_followup_is_bounded_and_keeps_its_own_date(hass) -> None:
    """Auch bei erneutem Uhrsprung gibt es höchstens einen datierten Folgeabruf."""

    client = AsyncMock()
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    with freeze_time("2026-08-23T23:59:59+02:00") as frozen:

        async def fetch(*args, **kwargs):
            requested_day = kwargs["local_date"]
            frozen.move_to(
                datetime.combine(
                    requested_day + timedelta(days=1), datetime.min.time(), TIMEZONE
                )
            )
            return _two_day_weather(requested_day, 300, 400)

        client.async_fetch_roofs.side_effect = fetch
        await coordinator.async_refresh()
        assert client.async_fetch_roofs.await_count == 2
        assert coordinator.data.local_date == date(2026, 8, 24)
        assert coordinator.data.total == DailyYield(6, 8)
        assert coordinator.get_daily_yield("today") == 8
        assert coordinator.get_daily_yield("tomorrow") is None


@pytest.mark.asyncio
async def test_failed_midnight_followup_retains_previous_snapshot(hass) -> None:
    """Ein fehlgeschlagener Folgeabruf bleibt ein normaler Coordinator-Fehler."""

    client = AsyncMock()
    coordinator = PvForecastCoordinator(hass, _entry(hass), client)
    previous = _snapshot(date(2026, 8, 23))
    coordinator.async_set_updated_data(previous)
    with freeze_time("2026-08-23T23:59:59+02:00") as frozen:

        async def fetch(*args, **kwargs):
            requested_day = kwargs["local_date"]
            if requested_day == date(2026, 8, 24):
                raise OpenMeteoConnectionError("Folgeabruf offline")
            frozen.move_to("2026-08-24T00:00:01+02:00")
            return _two_day_weather(requested_day)

        client.async_fetch_roofs.side_effect = fetch
        await coordinator.async_refresh()
        assert client.async_fetch_roofs.await_count == 2
        assert not coordinator.last_update_success
        assert coordinator.data is previous
        assert coordinator.get_daily_yield("today") == 20
        assert coordinator.get_daily_yield("tomorrow") is None


@pytest.mark.asyncio
async def test_unload_cancels_inflight_midnight_followup(hass) -> None:
    """Entladen beendet einen laufenden Folgeabruf und entfernt dessen Timer."""

    started = asyncio.Event()
    cancelled = asyncio.Event()
    with freeze_time("2026-08-23T23:59:00+02:00") as frozen:
        entry = _entry(hass)
        call_dates = []

        async def fetch(*args, **kwargs):
            requested_day = kwargs["local_date"]
            call_dates.append(requested_day)
            if len(call_dates) == 2:
                frozen.move_to("2026-08-24T00:00:01+02:00")
            if len(call_dates) == 3:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return _two_day_weather(requested_day)

        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            side_effect=fetch,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            running = entry.async_create_background_task(
                hass, coordinator.async_refresh(), name="Folgeabruf beim Unload testen"
            )
            await started.wait()
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert cancelled.is_set()
            assert running.cancelled()
            frozen.move_to("2026-08-25T00:00:00+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)
            assert len(call_dates) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("offline", [False, True], ids=["erfolg", "api-ausfall"])
async def test_midnight_notifies_before_refresh_and_preserves_error_semantics(
    hass, offline: bool
) -> None:
    """HA-Setup schaltet sofort alle Entities um und ruft zentral genau einmal ab."""

    with freeze_time("2026-08-23T23:59:00+02:00") as frozen:
        entry = _entry(hass)
        new_data = (
            OpenMeteoConnectionError("offline")
            if offline
            else _two_day_weather(date(2026, 8, 24), 300, 400)
        )
        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            side_effect=[_two_day_weather(date(2026, 8, 23)), new_data],
        ) as fetch:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            previous = coordinator.data
            observations = []
            unsub = coordinator.async_add_listener(
                lambda: observations.append(
                    (
                        coordinator.get_daily_yield("today"),
                        coordinator.get_daily_yield("tomorrow"),
                        coordinator.last_update_success,
                    )
                )
            )
            frozen.move_to("2026-08-24T00:00:00+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)

            assert observations[0] == (10, None, True)
            assert fetch.await_count == 2
            assert coordinator.last_update_success is not offline
            if offline:
                assert coordinator.data is previous
                assert observations[-1] == (10, None, False)
            else:
                assert coordinator.data.local_date == date(2026, 8, 24)
                assert observations[-1] == (6, 8, True)
            unsub()
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timezone", "before", "midnight", "next_midnight"),
    [
        (
            "Europe/Berlin",
            "2026-03-28T22:59:59+00:00",
            "2026-03-28T23:00:00+00:00",
            "2026-03-29T22:00:00+00:00",
        ),
        (
            "Europe/Berlin",
            "2026-10-24T21:59:59+00:00",
            "2026-10-24T22:00:00+00:00",
            "2026-10-25T23:00:00+00:00",
        ),
        (
            "Asia/Kathmandu",
            "2026-09-08T18:14:59+00:00",
            "2026-09-08T18:15:00+00:00",
            "2026-09-09T18:15:00+00:00",
        ),
    ],
    ids=["23-stunden-tag", "25-stunden-tag", "anlagenzone-statt-ha-zone"],
)
async def test_midnight_listener_follows_location_days_without_resetting_api_error(
    hass, timezone: str, before: str, midnight: str, next_midnight: str
) -> None:
    """Lokale 23-/25-Stunden-Tage und Teilstunden funktionieren ohne Polling."""

    await hass.config.async_set_time_zone("UTC")
    with freeze_time(before) as frozen:
        entry = _entry(hass, timezone, disable_polling=True)
        client = AsyncMock()
        coordinator = PvForecastCoordinator(hass, entry, client)
        local_day = datetime.fromisoformat(before).astimezone(ZoneInfo(timezone)).date()
        snapshot = _snapshot(local_day)
        coordinator.async_set_updated_data(snapshot)
        coordinator.async_set_update_error(UpdateFailed("offline"))
        listener = Mock()
        unsub = coordinator.async_add_listener(listener)
        coordinator.async_start_day_updates()
        coordinator.async_start_day_updates()

        for point, expected_today in ((midnight, 20), (next_midnight, None)):
            moment = datetime.fromisoformat(point)
            frozen.move_to(moment - timedelta(seconds=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)
            listener.assert_not_called()
            frozen.move_to(moment)
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)
            listener.assert_called_once_with()
            assert coordinator.get_daily_yield("today") == expected_today
            assert coordinator.get_daily_yield("tomorrow") is None
            assert not coordinator.last_update_success
            assert coordinator.data is snapshot
            client.async_fetch_roofs.assert_not_awaited()
            listener.reset_mock()

        unsub()
        await coordinator.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_first_midnight", [False, True])
async def test_unloading_entry_cancels_midnight_refresh(
    hass, after_first_midnight: bool
) -> None:
    """Nach realem Config-Entry-Unload läuft kein alter Mitternachtstermin mehr."""

    with freeze_time("2026-08-23T23:59:00+02:00") as frozen:
        entry = _entry(hass)
        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            side_effect=[
                _two_day_weather(date(2026, 8, 23)),
                _two_day_weather(date(2026, 8, 24)),
            ],
        ) as fetch:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            if after_first_midnight:
                frozen.move_to("2026-08-24T00:00:00+02:00")
                async_fire_time_changed(hass)
                await hass.async_block_till_done(wait_background_tasks=True)
            with patch.object(coordinator, "async_request_refresh") as refresh:
                assert await hass.config_entries.async_unload(entry.entry_id)
                frozen.move_to("2026-08-25T00:00:00+02:00")
                async_fire_time_changed(hass)
                await hass.async_block_till_done(wait_background_tasks=True)
                frozen.move_to("2026-08-26T00:00:00+02:00")
                async_fire_time_changed(hass)
                await hass.async_block_till_done(wait_background_tasks=True)
                refresh.assert_not_awaited()
                assert fetch.await_count == (2 if after_first_midnight else 1)


@pytest.mark.asyncio
async def test_midnight_and_scheduled_poll_share_one_refresh(hass) -> None:
    """Ein zur Tagesgrenze fälliger Intervallabruf wird nicht verdoppelt."""

    with freeze_time("2026-08-23T23:30:00+02:00") as frozen:
        entry = _entry(hass)
        with patch(
            "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
            side_effect=[
                _two_day_weather(date(2026, 8, 23)),
                _two_day_weather(date(2026, 8, 24), 300, 400),
            ],
        ) as fetch:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            frozen.move_to("2026-08-24T00:00:00+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)
            # Auch der Debouncer darf keinen zweiten Abruf nachholen.
            frozen.move_to("2026-08-24T00:00:15+02:00")
            async_fire_time_changed(hass)
            await hass.async_block_till_done(wait_background_tasks=True)
            assert fetch.await_count == 2
            assert entry.runtime_data.coordinator.last_update_success
            assert await hass.config_entries.async_unload(entry.entry_id)
