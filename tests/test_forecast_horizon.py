"""Optionale Mehrtagestendenzen durch Abruf, Clipping, Planung und HA prüfen."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pv_forecast.api import (
    OpenMeteoClient,
    OpenMeteoConnectionError,
    OpenMeteoDataError,
)
from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.card_data import build_forecast_view
from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.energy import async_get_solar_forecast
from custom_components.pv_forecast.horizon import forecast_days_from_options
from custom_components.pv_forecast.models import AcInverterGroup
from custom_components.pv_forecast.planning import plan_solar_window
from custom_components.pv_forecast.sensor import (
    PvForecastRoofSensor,
    PvForecastTotalSensor,
)
from custom_components.pv_forecast.services import _serialize_forecast

from .helpers import configure_options, persisted_roof, roof
from .test_api import _hourly_payload, _Response, _Session


class HorizonSession(_Session):
    """Antwortumfang aus den ausdrücklich angefragten UTC-Grenzen erzeugen."""

    def __init__(self):
        super().__init__(_Response({}))
        self.response_bytes = 0

    def get(self, *args, **kwargs):
        params = kwargs["params"]
        payload = _hourly_payload(params["start_hour"], params["end_hour"])
        self.response_bytes += len(json.dumps(payload).encode())
        self.response = _Response(payload)
        return super().get(*args, **kwargs)


@pytest.mark.parametrize(
    "zone,day",
    [
        ("UTC", date(2026, 9, 9)),
        ("Europe/Berlin", date(2026, 3, 25)),
        ("Europe/Berlin", date(2026, 10, 21)),
        ("Asia/Kathmandu", date(2026, 9, 9)),
    ],
)
@pytest.mark.parametrize("days", [2, 7])
async def test_horizon_covers_all_local_days_with_same_geometry_requests(
    zone, day, days
):
    session = HorizonSession()
    roofs = (roof("a"), roof("b"))
    weather = await OpenMeteoClient(session).async_fetch_roofs(
        48, 16, zone, roofs, local_date=day, forecast_days=days
    )
    assert session.calls == 1
    assert weather["a"] is weather["b"]
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=days), time.min, tz).astimezone(UTC)
    data = calculate_forecast(
        roofs,
        weather,
        12,
        day,
        tz,
        forecast_days=days,
        calibration_factor=1.2,
        inverter_groups=(AcInverterGroup("g", "Gerät", 8, ("a",)),),
    )
    assert data.forecast_days == days
    assert data.total_intervals[0].start == start
    assert data.total_intervals[-1].end == end
    assert sum(i.energy_kwh for i in data.total_intervals) == pytest.approx(
        12 * (end - start).total_seconds() / 3600
    )
    view = build_forecast_view(data, zone, "Anlage", start, start, True)
    assert len(view["daily_forecasts"]) == days
    assert sum(i["energy_kwh"] for i in view["daily_forecasts"]) == pytest.approx(
        sum(i.energy_kwh for i in data.total_intervals)
    )
    assert all(i["tendency"] for i in view["daily_forecasts"][2:])
    assert all(
        i["measured_quality"]["status"] == "unavailable"
        for i in view["daily_forecasts"]
    )
    envelope = _serialize_forecast(data, zone, start, True)
    assert envelope["coverage"]["complete"]
    assert envelope["forecast_days"] == days
    short = HorizonSession()
    await OpenMeteoClient(short).async_fetch_roofs(48, 16, zone, roofs, local_date=day)
    assert short.calls == session.calls
    assert (session.response_bytes > short.response_bytes) == (days > 2)


async def test_seven_day_response_missing_last_gti_is_rejected():
    session = HorizonSession()
    client = OpenMeteoClient(session)
    with patch.object(
        client,
        "_async_request",
        return_value=_hourly_payload("2026-09-09T01:00", "2026-09-15T23:00"),
    ):
        with pytest.raises(OpenMeteoDataError):
            await client.async_fetch(
                48,
                16,
                "UTC",
                tilt_deg=30,
                open_meteo_azimuth_deg=0,
                local_date=date(2026, 9, 9),
                forecast_days=7,
            )


@pytest.mark.parametrize(
    "zone,day",
    [
        ("UTC", date(2026, 9, 9)),
        ("Europe/Berlin", date(2026, 3, 27)),
        ("Europe/Berlin", date(2026, 10, 23)),
        ("Asia/Kathmandu", date(2026, 9, 9)),
    ],
)
async def test_sensors_follow_all_covered_days_after_midnight(hass, freezer, zone, day):
    """Vorhandene Folgetage bleiben mit Karte, Clipping und Fehlerstatus konsistent."""
    timezone = ZoneInfo(zone)
    started = datetime.combine(day, time(23, 59), timezone).astimezone(UTC)
    freezer.move_to(started)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 48, "longitude": 16, "time_zone": zone},
        options={
            "roofs": [persisted_roof("a"), persisted_roof("b")],
            "forecast_days": 7,
            "inverter_max_power_kw": 15,
        },
    )
    entry.add_to_hass(hass)
    session = HorizonSession()
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_calibration(0.5, None)
    snapshot = coordinator.data
    fetched_at = coordinator.last_update_success_time
    sensors = {
        (selected, roof_id): (
            PvForecastTotalSensor(coordinator, entry, selected)
            if roof_id is None
            else PvForecastRoofSensor(coordinator, entry, roof_id, "Dach", selected)
        )
        for selected in ("today", "tomorrow")
        for roof_id in (None, "a", "b")
    }
    try:
        for offset in range(1, 8):
            now = datetime.combine(
                day + timedelta(days=offset), time.min, timezone
            ).astimezone(UTC)
            freezer.move_to(now)
            coordinator.async_update_listeners()
            for (selected, roof_id), sensor in sensors.items():
                view = build_forecast_view(
                    snapshot, zone, "Anlage", now, fetched_at, True, roof_id=roof_id
                )
                expected = view["summary"][f"{selected}_kwh"]
                assert sensor.native_value == (
                    round(expected, 2) if expected is not None else None
                )
                # Die rohe Mehrtagesansicht bleibt lesbar; SAX-Gesamtsensoren
                # geben tagelang alte Daten trotz Abdeckung nicht operativ frei.
                assert sensor.available is (
                    expected is not None
                    and (roof_id is not None or now - fetched_at <= timedelta(hours=1))
                )
            assert coordinator.get_daily_yield("today", "entfernt") is None
        assert session.calls == 1
        assert coordinator.data is snapshot
        assert coordinator.last_update_success_time == fetched_at

        # Null bleibt intern lesbar; der Gesamtsensor sperrt auch alte Nulltage.
        zero_day = day + timedelta(days=2)
        zero_start = datetime.combine(zero_day, time.min, timezone).astimezone(UTC)
        freezer.move_to(zero_start)
        zero_snapshot = replace(
            snapshot,
            total_intervals=tuple(
                replace(i, energy_kwh=0, ac_power_kw=0)
                for i in snapshot.total_intervals
            ),
            roofs={
                key: replace(
                    value,
                    intervals=tuple(
                        replace(i, energy_kwh=0, ac_power_kw=0) for i in value.intervals
                    ),
                )
                for key, value in snapshot.roofs.items()
            },
        )
        coordinator.data = zero_snapshot
        assert all(sensor.native_value == 0 for sensor in sensors.values())
        assert not sensors[("today", None)].available
        first_interval = next(
            i for i in zero_snapshot.total_intervals if i.start <= zero_start < i.end
        )
        for missing in (False, True):
            coordinator.data = replace(
                zero_snapshot,
                total_intervals=tuple(
                    replace(i, is_complete=False) if i is first_interval else i
                    for i in zero_snapshot.total_intervals
                    if not missing or i is not first_interval
                ),
            )
            assert all(
                coordinator.get_daily_yield("today", roof_id) is None
                for roof_id in (None, "a", "b")
            )
        coordinator.data = snapshot

        # Auch ein belegter Folgetag macht einen fehlgeschlagenen Abruf nicht gesund.
        freezer.move_to(started + timedelta(days=2))
        with patch.object(
            coordinator._client,
            "async_fetch_roofs",
            side_effect=OpenMeteoConnectionError("offline"),
        ):
            await coordinator.async_refresh()
        assert sensors[("today", None)].native_value is not None
        assert all(not sensor.available for sensor in sensors.values())
        assert coordinator.last_update_success_time == fetched_at
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_midnight_uses_existing_horizon_without_extra_request(hass, freezer):
    """Ein abgedeckter dritter Tag benötigt keinen zusätzlichen Mitternachtsabruf."""
    freezer.move_to("2026-09-09T23:59:00Z")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 48, "longitude": 16, "time_zone": "UTC"},
        options={"roofs": [persisted_roof("a")], "forecast_days": 3},
    )
    entry.add_to_hass(hass)
    session = HorizonSession()
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    scheduled = coordinator._unsub_refresh
    try:
        freezer.move_to("2026-09-10T00:00:00Z")
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert session.calls == 1
        assert coordinator._unsub_refresh is scheduled
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "bad", [1, 8, 2.5, True, None, "7", float("nan"), float("inf")]
)
def test_invalid_horizon_is_not_silently_normalized(bad):
    with pytest.raises(ValueError):
        forecast_days_from_options({"forecast_days": bad})
    assert forecast_days_from_options({}) == 2


async def test_later_day_offers_better_contiguous_window_without_new_sensor(
    hass, freezer
):
    freezer.move_to("2026-09-09T00:00:00Z")
    start = datetime(2026, 9, 9, tzinfo=UTC)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 48, "longitude": 16, "time_zone": "UTC"},
        options={"roofs": [persisted_roof("a")], "forecast_days": 7},
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=HorizonSession(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    data = entry.runtime_data.coordinator.data
    assert data.forecast_days == 7
    ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert len(ids) == 8
    energy = await async_get_solar_forecast(hass, entry.entry_id)
    assert len(energy["wh_hours"]) == 48
    intervals = tuple(
        replace(
            i,
            energy_kwh=(
                2
                if start + timedelta(days=3, hours=12)
                <= i.start
                < start + timedelta(days=3, hours=14)
                else 1
            ),
            ac_power_kw=(
                2
                if start + timedelta(days=3, hours=12)
                <= i.start
                < start + timedelta(days=3, hours=14)
                else 1
            ),
        )
        for i in data.total_intervals
    )
    result = plan_solar_window(
        replace(data, total_intervals=intervals),
        "UTC",
        start,
        start,
        True,
        duration_minutes=120,
        earliest_start=start,
        latest_end=start + timedelta(days=7),
    )
    assert result["status"] == "available"
    assert result["start"] == (start + timedelta(days=3, hours=12)).isoformat()
    assert result["energy_kwh"] == 4
    assert result["includes_tendency"] is True
    options = await hass.config_entries.options.async_init(entry.entry_id)
    options = await configure_options(
        hass, options["flow_id"], {"next_step_id": "forecast_horizon"}
    )
    options = await hass.config_entries.options.async_configure(
        options["flow_id"], {"forecast_days": 2}
    )
    assert options["data"]["forecast_days"] == 2
    assert options["data"]["roofs"] == [persisted_roof("a")]
    assert (entry.version, entry.minor_version) == (1, 1)
