"""SAX-Vertrag #175 am tatsächlichen HA-Zustand der bestehenden Sensoren prüfen."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pv_forecast.const import DOMAIN

from .test_energy import DAY, _load_plant, _weather

PERIODS = ("today", "tomorrow", "remaining_today")


@pytest.fixture(autouse=True)
def sensor_clock(freezer):
    """Der Abruf und alle Fenster beginnen bei einem expliziten Zeitpunkt."""
    freezer.move_to("2026-08-23T12:00:00Z")


@pytest.fixture
def forecast_client():
    """Die komplette Integration ohne Netzwerk mit kontrollierten Wetterdaten laden."""
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
    ) as fetch:
        yield fetch


def states(hass, entry):
    """Die drei öffentlich auswählbaren Gesamtzustände über stabile IDs lesen."""
    registry = er.async_get(hass)
    return {
        period: hass.states.get(
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_total_{period}"
            )
        )
        for period in PERIODS
    }


async def tick(hass, freezer, instant):
    """Den bestehenden lokalen Takt einschließlich HA-Zustandsschreibung ausführen."""
    freezer.move_to(instant)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("gti", [0, 1000])
async def test_sax_reads_numeric_total_energy_without_attributes(
    hass, forecast_client, gti
):
    """Zwei Dächer ergeben nach gemeinsamem AC-Limit kWh; gültige Null bleibt Null."""
    entry = await _load_plant(hass, forecast_client)
    forecast_client.return_value = _weather(DAY, "Europe/Berlin", gti)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    power = 15 if gti else 0
    expected = {
        "today": power * 24,
        "tomorrow": power * 24,
        "remaining_today": power * 10,
    }
    for period, state in states(hass, entry).items():
        assert isfinite(float(state.state))
        assert float(state.state) == expected[period]
        assert state.attributes["unit_of_measurement"] == "kWh"
        assert state.attributes["device_class"] == "energy"
        assert "state_class" not in state.attributes
        assert "detailedForecast" not in state.attributes
        assert "intervals" not in state.attributes
    assert (
        len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 10
    )


@pytest.mark.parametrize("energy", [5, 7.9, 8, 12.34])
async def test_sax_examples_use_only_the_sensor_state(hass, forecast_client, energy):
    """Die dokumentierten SAX-Rechenbeispiele benötigen keine Prognoseattribute."""
    entry = await _load_plant(hass, forecast_client)
    # 20 kWp, keine Verluste, 24 h konstante GTI: gewünschte Tagesenergie.
    forecast_client.return_value = _weather(
        DAY, "Europe/Berlin", energy / 24 / 20 * 1000
    )
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    state = states(hass, entry)["tomorrow"]
    value = float(state.state)
    assert value == energy
    if energy == 12.34:
        assert state.state == "12.34"
    if energy == 5:
        assert max(0, 6 - value * 0.8) == 2
    if energy in (7.9, 8):
        assert (value >= 8) is (energy == 8)


@pytest.mark.parametrize(
    ("age", "available"),
    [(None, False), (-1, False), (0, True), (3600, True), (3601, False)],
)
async def test_sax_age_is_visible_in_state(
    hass, freezer, forecast_client, age, available
):
    """Fehlende, zukünftige und über 60 Minuten alte Abrufzeiten sperren den Zustand."""
    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    if age is None:
        coordinator.last_update_success_time = None
    else:
        coordinator.last_update_success_time -= timedelta(seconds=age)
    stamp = coordinator.last_update_success_time
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert all(
        (state.state != STATE_UNAVAILABLE) is available
        for state in states(hass, entry).values()
    )
    assert coordinator.data is snapshot
    assert coordinator.last_update_success
    assert coordinator.last_update_success_time == stamp
    assert forecast_client.await_count == 1


async def test_sax_minute_expires_and_refresh_recovers(hass, freezer, forecast_client):
    """Ohne Polling sperrt der Minutentakt alte Daten und bewahrt Abruffehler."""
    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    stamp = coordinator.last_update_success_time
    await tick(hass, freezer, stamp + timedelta(minutes=1))
    assert float(states(hass, entry)["remaining_today"].state) == 149.75
    await tick(hass, freezer, stamp + timedelta(minutes=60))
    assert float(states(hass, entry)["remaining_today"].state) == 135
    await tick(hass, freezer, stamp + timedelta(minutes=61))
    assert all(
        state.state == STATE_UNAVAILABLE for state in states(hass, entry).values()
    )
    assert coordinator.data is snapshot
    assert coordinator.last_update_success_time == stamp
    assert coordinator.last_update_success
    assert forecast_client.await_count == 1
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert all(
        state.state != STATE_UNAVAILABLE for state in states(hass, entry).values()
    )
    error = UpdateFailed("Offline")
    coordinator.async_set_update_error(error)
    await tick(hass, freezer, stamp + timedelta(minutes=62))
    assert all(
        state.state == STATE_UNAVAILABLE for state in states(hass, entry).values()
    )
    assert coordinator.last_exception is error
    assert forecast_client.await_count == 2


@pytest.mark.parametrize(
    "problem",
    ["missing", "gap", "incomplete", "fallback", "nan", "infinity", "negative"],
)
async def test_sax_invalid_window_is_not_zero(hass, forecast_client, problem):
    """Ein unbrauchbares laufendes Intervall sperrt Heute und Rest, nicht Morgen."""
    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    forecast = coordinator.data
    intervals = list(forecast.total_intervals)
    index = next(
        i
        for i, item in enumerate(intervals)
        if item.start == datetime(2026, 8, 23, 12, tzinfo=UTC)
    )
    if problem == "missing":
        coordinator.data = None
    else:
        if problem == "gap":
            del intervals[index]
        else:
            changes = {
                "incomplete": {"is_complete": False},
                "fallback": {"quality_flags": ("missing_gti",)},
                "nan": {"energy_kwh": float("nan")},
                "infinity": {"energy_kwh": float("inf")},
                "negative": {"energy_kwh": -1},
            }[problem]
            intervals[index] = replace(intervals[index], **changes)
        coordinator.data = replace(forecast, total_intervals=tuple(intervals))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    result = states(hass, entry)
    assert result["today"].state == result["remaining_today"].state == STATE_UNAVAILABLE
    assert (result["tomorrow"].state == STATE_UNAVAILABLE) is (problem == "missing")
    assert forecast_client.await_count == 1


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, "12"])
async def test_sax_invalid_daily_number_is_not_published(
    hass, forecast_client, invalid
):
    """Auch ein ungültiger vorberechneter Tageswert darf keine Zahl vortäuschen."""
    entry = await _load_plant(hass, forecast_client)
    coordinator = entry.runtime_data.coordinator
    coordinator.data = replace(
        coordinator.data, total=replace(coordinator.data.total, today=invalid)
    )
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(hass, entry)["today"].state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("zone", "day", "hours"),
    [
        ("Europe/Berlin", DAY, 24),
        ("Europe/Berlin", date(2026, 3, 29), 23),
        ("Europe/Berlin", date(2026, 10, 25), 25),
        ("Asia/Kathmandu", DAY, 24),
    ],
)
async def test_sax_midnight_and_dst_use_plant_zone(
    hass, freezer, forecast_client, zone, day, hours
):
    """Der neue lokale Tag behält seine UTC-Dauer; ein fehlendes Morgen bleibt leer."""
    timezone = ZoneInfo(zone)
    previous = day - timedelta(days=1)
    freezer.move_to(datetime.combine(previous, time(23, 45), timezone))
    entry = await _load_plant(hass, forecast_client, zone, previous)
    before = states(hass, entry)
    coordinator = entry.runtime_data.coordinator
    stamp = coordinator.last_update_success_time
    assert float(before["tomorrow"].state) == 15 * hours
    await tick(hass, freezer, datetime.combine(day, time.min, timezone))
    after = states(hass, entry)
    assert (
        float(after["today"].state)
        == float(after["remaining_today"].state)
        == 15 * hours
    )
    assert after["tomorrow"].state == STATE_UNAVAILABLE
    assert {p: s.entity_id for p, s in before.items()} == {
        p: s.entity_id for p, s in after.items()
    }
    assert coordinator.last_update_success_time == stamp
    assert forecast_client.await_count == 1


async def test_sax_identity_survives_rename_and_reload(hass, forecast_client):
    """Anlagenname, Entity-Name und Runtime-Neustart verändern die Zuordnung nicht."""
    entry = await _load_plant(hass, forecast_client)
    before = states(hass, entry)
    registry = er.async_get(hass)
    for state in before.values():
        registry.async_update_entity(state.entity_id, name="Neuer Anzeigename")
    hass.config_entries.async_update_entry(entry, title="Neue Anlagenbezeichnung")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = states(hass, entry)
    assert {p: s.entity_id for p, s in before.items()} == {
        p: s.entity_id for p, s in after.items()
    }
    assert all(state.state != STATE_UNAVAILABLE for state in after.values())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("2026-03-29T01:59:00+01:00", "2026-03-29T03:00:00+02:00"),
        ("2026-10-25T02:59:00+02:00", "2026-10-25T02:00:00+01:00"),
    ],
)
async def test_sax_remaining_crosses_dst_in_absolute_time(
    hass, freezer, forecast_client, before, after
):
    """Übersprungene und doppelte Ortsstunden verbrauchen nur eine reale Minute."""
    instant = datetime.fromisoformat(before)
    freezer.move_to(instant)
    entry = await _load_plant(hass, forecast_client, day=instant.date())
    initial = float(states(hass, entry)["remaining_today"].state)
    await tick(hass, freezer, datetime.fromisoformat(after))
    assert float(states(hass, entry)["remaining_today"].state) == initial - 0.25
    assert forecast_client.await_count == 1
