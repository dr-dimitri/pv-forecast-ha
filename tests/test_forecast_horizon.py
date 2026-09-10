"""Optionale Mehrtagestendenzen durch Abruf, Clipping, Planung und HA prüfen."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import OpenMeteoClient, OpenMeteoDataError
from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.card_data import build_forecast_view
from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.energy import async_get_solar_forecast
from custom_components.pv_forecast.horizon import forecast_days_from_options
from custom_components.pv_forecast.models import AcInverterGroup
from custom_components.pv_forecast.planning import plan_solar_window
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
