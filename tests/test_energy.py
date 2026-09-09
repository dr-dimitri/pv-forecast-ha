"""Native Energy-Discovery, Antwortvertrag und unveränderte Prognosedaten prüfen."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.energy import data as energy_data
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.energy import async_get_solar_forecast

from .helpers import persisted_roof, weather

DAY = date(2026, 8, 23)


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url) -> None:
    """Recorder-Datenbank vor der globalen HA-Custom-Integration-Fixture vorbereiten."""


@pytest.fixture(autouse=True)
def fixed_energy_date(freezer) -> None:
    """Normale Vertragstests auf einen festen lokalen Prognosetag beziehen."""

    freezer.move_to("2026-08-23T12:00:00+00:00")


@pytest.fixture
def forecast_client():
    """Alle Wetterabrufe durch einen gemeinsamen kontrollierbaren Client ersetzen."""

    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
    ) as fetch:
        yield fetch


def _weather(day: date, timezone_name: str, gti: float = 1000):
    """Die lokalen Zieltage mit stündlichen UTC-Eingabedaten überdecken."""

    timezone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=2), time.min, timezone).astimezone(UTC)
    cursor = start.replace(minute=0) + timedelta(hours=1)
    points = []
    while cursor - timedelta(hours=1) < end:
        points.append(weather(gti, end=cursor))
        cursor += timedelta(hours=1)
    return {"a": tuple(points), "b": tuple(points)}


async def _load_plant(
    hass, forecast_client, timezone_name: str = "Europe/Berlin", day: date = DAY
):
    """Eine echte Anlage mit zwei Dächern und gemeinsamem 15-kW-Limit laden."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gesamtanlage",
        version=1,
        minor_version=1,
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: timezone_name,
        },
        options={
            CONF_ROOFS: [persisted_roof("a"), persisted_roof("b", name="Garage")],
            CONF_INVERTER_MAX_POWER_KW: 15.0,
        },
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    forecast_client.return_value = _weather(day, timezone_name)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_energy_converts_shared_clipped_data_once(hass, forecast_client) -> None:
    """Native Antwort und Sensorbasis bleiben dieselben, mit Wh statt kWh."""

    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    fetched_at = coordinator.last_update_success_time
    config_data, config_options = dict(entry.data), dict(entry.options)
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    entity_ids = {entity.entity_id for entity in entities}
    assert len(entity_ids) == 10

    result = await async_get_solar_forecast(hass, entry.entry_id)

    assert result is not None
    assert set(result) == {"wh_hours"}
    assert len(result["wh_hours"]) == 48
    assert result["wh_hours"]["2026-08-22T22:00:00+00:00"] == 15000
    assert result["wh_hours"]["2026-08-24T21:00:00+00:00"] == 15000
    assert "2026-08-24T22:00:00+00:00" not in result["wh_hours"]
    assert sum(result["wh_hours"].values()) == pytest.approx(
        1000 * (snapshot.total.today + snapshot.total.tomorrow)
    )
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert coordinator.data is snapshot
    assert coordinator.last_update_success_time == fetched_at
    assert forecast_client.await_count == 1
    assert entry.data == config_data
    assert entry.options == config_options
    assert (entry.version, entry.minor_version) == (1, 1)
    assert "energy" not in hass.config.components
    assert {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == entity_ids
    assert all(
        "state_class" not in hass.states.get(entity_id).attributes
        for entity_id in entity_ids
    )


@pytest.mark.parametrize(
    ("timezone_name", "day", "today_hours", "point_count"),
    [
        ("Europe/Berlin", date(2026, 3, 29), 23, 47),
        ("Europe/Berlin", date(2026, 10, 25), 25, 49),
        ("Asia/Kolkata", DAY, 24, 50),
        ("Asia/Kathmandu", DAY, 24, 50),
    ],
    ids=["sommerzeit", "winterzeit", "indien", "nepal"],
)
async def test_energy_preserves_local_days_and_absolute_starts(
    hass, freezer, forecast_client, timezone_name, day, today_hours, point_count
) -> None:
    """Auch eine innere Teilstunden-Mitternacht ordnet Energie dem richtigen Tag zu."""

    timezone = ZoneInfo(timezone_name)
    freezer.move_to(datetime.combine(day, time(12), timezone))
    entry = await _load_plant(hass, forecast_client, timezone_name, day)
    snapshot = entry.runtime_data.coordinator.data

    result = await async_get_solar_forecast(hass, entry.entry_id)

    assert result is not None
    points = result["wh_hours"]
    assert len(points) == point_count
    starts = [datetime.fromisoformat(stamp) for stamp in points]
    assert all(instant.utcoffset() == timedelta(0) for instant in starts)
    assert len(set(starts)) == point_count
    assert starts == sorted(starts)
    assert starts[0] == datetime.combine(day, time.min, timezone).astimezone(UTC)
    assert all(
        day <= instant.astimezone(timezone).date() <= day + timedelta(days=1)
        for instant in starts
    )
    totals = {}
    for stamp, wh in points.items():
        local_day = datetime.fromisoformat(stamp).astimezone(timezone).date()
        totals[local_day] = totals.get(local_day, 0) + wh
    assert totals[day] == pytest.approx(15000 * today_hours)
    assert totals[day + timedelta(days=1)] == pytest.approx(15000 * 24)
    assert totals[day] == pytest.approx(snapshot.total.today * 1000)
    assert totals[day + timedelta(days=1)] == pytest.approx(
        snapshot.total.tomorrow * 1000
    )
    assert sum(points.values()) == pytest.approx(
        sum(interval.energy_kwh for interval in snapshot.total_intervals) * 1000
    )
    if today_hours == 25:
        assert points["2026-10-25T00:00:00+00:00"] == 15000
        assert points["2026-10-25T01:00:00+00:00"] == 15000
    if timezone_name.startswith("Asia/"):
        midnight = datetime.combine(
            day + timedelta(days=1), time.min, timezone
        ).astimezone(UTC)
        original = next(
            item
            for item in snapshot.total_intervals
            if item.start < midnight < item.end
        )
        assert points[original.start.isoformat()] == pytest.approx(
            15000 * (midnight - original.start).total_seconds() / 3600
        )
        assert points[midnight.isoformat()] == pytest.approx(
            15000 * (original.end - midnight).total_seconds() / 3600
        )
    assert forecast_client.await_count == 1


async def test_energy_keeps_complete_zero_forecast(hass, forecast_client) -> None:
    """Ein gültiger Nulltag behält sämtliche Nullintervalle statt fehlender Daten."""

    entry = await _load_plant(hass, forecast_client)
    forecast_client.return_value = _weather(DAY, "Europe/Berlin", 0)
    await entry.runtime_data.coordinator.async_refresh()

    result = await async_get_solar_forecast(hass, entry.entry_id)

    assert result is not None
    assert len(result["wh_hours"]) == 48
    assert set(result["wh_hours"].values()) == {0}
    assert forecast_client.await_count == 2


async def test_energy_handles_missing_and_unloaded_entries(hass) -> None:
    """Ungeladene und fremde Einträge erzeugen keine AttributeErrors oder Nullreihen."""

    assert await async_get_solar_forecast(hass, "missing") is None
    for domain in (DOMAIN, "other"):
        entry = MockConfigEntry(domain=domain)
        entry.add_to_hass(hass)
        assert await async_get_solar_forecast(hass, entry.entry_id) is None


@pytest.mark.parametrize(
    "problem",
    [
        "no_snapshot",
        "empty",
        "first_hour",
        "gap",
        "last_hour",
        "missing_roof",
        "overflow",
    ],
)
async def test_energy_rejects_unusable_snapshots(
    hass, forecast_client, problem
) -> None:
    """Fehlende Abdeckung und unbrauchbare Wh ergeben keine native Prognose."""

    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    intervals = snapshot.total_intervals
    if problem == "no_snapshot":
        coordinator.data = None
    else:
        broken = {
            "empty": (),
            "first_hour": intervals[1:],
            "gap": intervals[:12] + intervals[13:],
            "last_hour": intervals[:-1],
            "missing_roof": (
                replace(intervals[0], is_complete=False),
                *intervals[1:],
            ),
            "overflow": (
                replace(intervals[0], energy_kwh=1e308),
                *intervals[1:],
            ),
        }[problem]
        coordinator.data = replace(snapshot, total_intervals=broken)

    assert await async_get_solar_forecast(hass, entry.entry_id) is None
    assert forecast_client.await_count == 1


async def test_energy_hides_failed_or_expired_snapshot(
    hass, freezer, forecast_client
) -> None:
    """Der Vertrag ohne Metadaten verdeckt weder Fehler noch veraltete Daten."""

    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    coordinator.async_set_update_error(UpdateFailed("Open-Meteo ist nicht erreichbar"))
    assert await async_get_solar_forecast(hass, entry.entry_id) is None
    assert coordinator.data is snapshot
    assert not coordinator.last_update_success
    assert forecast_client.await_count == 1

    await coordinator.async_refresh()
    assert await async_get_solar_forecast(hass, entry.entry_id) is not None
    freezer.move_to("2026-08-23T22:00:00+00:00")
    assert coordinator.last_update_success
    assert await async_get_solar_forecast(hass, entry.entry_id) is None
    assert forecast_client.await_count == 2


async def test_energy_uses_fresh_runtime_after_reload(hass, forecast_client) -> None:
    """Nach Entladen verschwindet der alte Stand; Reload liefert die neue Berechnung."""

    entry = await _load_plant(hass, forecast_client)
    old_coordinator = entry.runtime_data.coordinator
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await async_get_solar_forecast(hass, entry.entry_id) is None
    forecast_client.return_value = _weather(DAY, "Europe/Berlin", 100)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await async_get_solar_forecast(hass, entry.entry_id)

    assert entry.runtime_data.coordinator is not old_coordinator
    assert result is not None
    assert set(result["wh_hours"].values()) == {2000}
    assert forecast_client.await_count == 2


@pytest.mark.parametrize(
    "energy_first", [True, False], ids=["energy-zuerst", "pv-zuerst"]
)
async def test_native_energy_discovers_platform_and_deduplicates_backend_calls(
    recorder_mock, hass, hass_ws_client, forecast_client, energy_first
) -> None:
    """Native Discovery und WebSocket rufen eine Gesamtanlage nur einmal ab."""

    with patch(
        "custom_components.pv_forecast.energy.async_get_solar_forecast",
        wraps=async_get_solar_forecast,
    ) as adapter:
        if energy_first:
            assert await async_setup_component(hass, "energy", {})
            client = await hass_ws_client(hass)
            await client.send_json({"id": 1, "type": "energy/info"})
            initial_info = await client.receive_json()
            assert initial_info["success"]
            assert DOMAIN not in initial_info["result"]["solar_forecast_domains"]

        entry = await _load_plant(hass, forecast_client)
        if not energy_first:
            assert await async_setup_component(hass, "energy", {})
            client = await hass_ws_client(hass)
        await hass.async_block_till_done()
        await client.send_json({"id": 2, "type": "energy/info"})
        info = await client.receive_json()
        assert info["success"]
        assert DOMAIN in info["result"]["solar_forecast_domains"]

        manager = await energy_data.async_get_manager(hass)
        manager.data = energy_data.EnergyManager.default_preferences()
        manager.data["energy_sources"] = [
            {
                "type": "solar",
                "stat_energy_from": f"sensor.real_production_{index}",
                "config_entry_solar_forecast": [entry.entry_id],
            }
            for index in (1, 2)
        ]
        await client.send_json({"id": 3, "type": "energy/solar_forecast"})
        response = await client.receive_json()

        assert response["success"]
        assert set(response["result"]) == {entry.entry_id}
        assert response["result"][entry.entry_id] == await async_get_solar_forecast(
            hass, entry.entry_id
        )
        adapter.assert_awaited_once_with(hass, entry.entry_id)
        assert forecast_client.await_count == 1
