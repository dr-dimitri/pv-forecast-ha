"""Antwortvertrag, Berechtigungen und Lebenszyklus der lesenden Prognoseaktion."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.pv_forecast.const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    TotalForecastInterval,
)
from custom_components.pv_forecast.services import (
    SERVICE_GET_FORECAST,
    _serialize_forecast,
    async_setup_services,
)

from .helpers import persisted_roof, weather


@pytest.fixture(autouse=True)
def fixed_service_date(freezer) -> None:
    """Abrufzeit und lokales Prognosefenster reproduzierbar festlegen."""

    freezer.move_to("2026-08-23T12:00:00+00:00")


@pytest.fixture
async def loaded_forecast(hass):
    """Eine reale Anlage mit vollständigem, gemeinsam geclipptem Snapshot laden."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Anlage Berlin",
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={
            CONF_ROOFS: [persisted_roof("a"), persisted_roof("b", name="Garage")],
            CONF_INVERTER_MAX_POWER_KW: 15.0,
        },
    )
    entry.add_to_hass(hass)
    start = datetime(2026, 8, 22, 22, tzinfo=UTC)
    intervals = tuple(
        weather(end=start + timedelta(hours=hour)) for hour in range(1, 49)
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={"a": intervals, "b": intervals},
    ) as client_fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, entry.runtime_data.coordinator, client_fetch
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def _get_forecast(hass, entry_id: str, *, user_id: str | None = None):
    """Den öffentlichen HA-Aktionsweg einschließlich Antwortvalidierung verwenden."""

    return await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_FORECAST,
        {"config_entry_id": entry_id},
        blocking=True,
        return_response=True,
        context=Context(user_id=user_id),
    )


async def test_action_registered_without_entries_and_requires_selection(hass) -> None:
    """Die Aktion bleibt ohne Anlage sichtbar und wählt kein implizites Ziel."""

    async_setup_services(hass)
    assert hass.services.supports_response(DOMAIN, SERVICE_GET_FORECAST) is (
        SupportsResponse.ONLY
    )
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_FORECAST,
            {},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(ServiceValidationError) as error:
        await _get_forecast(hass, "missing")
    assert error.value.translation_key == "entry_not_found"


async def test_action_rejects_other_integration_and_unloaded_entry(hass) -> None:
    """Fremde und noch ungeladene Einträge liefern verständliche Aktionsfehler."""

    async_setup_services(hass)
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    unloaded = MockConfigEntry(domain=DOMAIN)
    unloaded.add_to_hass(hass)
    for entry, expected in (
        (other, "entry_not_found"),
        (unloaded, "entry_not_loaded"),
    ):
        with pytest.raises(ServiceValidationError) as error:
            await _get_forecast(hass, entry.entry_id)
        assert error.value.translation_key == expected


async def test_action_returns_shared_clipped_forecast_without_fetch(
    hass, loaded_forecast
) -> None:
    """Eine JSON-Antwort enthält denselben geclippten Datenstand wie die Tageswerte."""

    entry, coordinator, client_fetch = loaded_forecast
    original_data = coordinator.data
    original_time = coordinator.last_update_success_time

    result = await _get_forecast(hass, entry.entry_id)

    assert result["schema_version"] == 1
    assert result["timezone"] == "Europe/Berlin"
    assert result["forecast_start_date"] == "2026-08-23"
    assert result["fetched_at"] == "2026-08-23T12:00:00+00:00"
    assert result["model_issued_at"] is None
    assert result["last_update_success"] is True
    assert result["coverage"] == {
        "start": "2026-08-22T22:00:00+00:00",
        "end": "2026-08-24T22:00:00+00:00",
        "complete": True,
    }
    assert len(result["intervals"]) == 48
    assert result["intervals"][0] == {
        "start": "2026-08-22T22:00:00+00:00",
        "end": "2026-08-22T23:00:00+00:00",
        "energy_kwh": 15.0,
        "ac_power_kw": 15.0,
        "quality_flags": [],
        "is_complete": True,
    }
    assert sum(interval["energy_kwh"] for interval in result["intervals"]) == (
        coordinator.data.total.today + coordinator.data.total.tomorrow
    )
    assert json.loads(json.dumps(result)) == result
    assert coordinator.data is original_data
    assert coordinator.last_update_success_time == original_time
    assert client_fetch.await_count == 1


async def test_action_keeps_old_snapshot_date_fetch_time_and_failure(
    hass, freezer, loaded_forecast
) -> None:
    """Ein fehlgeschlagenes Update bleibt auch bei lesbaren älteren Daten erkennbar."""

    entry, coordinator, client_fetch = loaded_forecast
    coordinator.async_set_update_error(UpdateFailed("Open-Meteo ist nicht erreichbar"))
    freezer.move_to("2026-08-24T12:00:00+00:00")

    result = await _get_forecast(hass, entry.entry_id)

    assert result["forecast_start_date"] == "2026-08-23"
    assert result["fetched_at"] == "2026-08-23T12:00:00+00:00"
    assert result["last_update_success"] is False
    assert client_fetch.await_count == 1
    assert not coordinator.last_update_success


async def test_action_survives_unload_and_reload(hass, loaded_forecast) -> None:
    """Die globale Aktion bleibt registriert und liest nach Reload die neue Laufzeit."""

    entry, coordinator, client_fetch = loaded_forecast
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_FORECAST)
    with pytest.raises(ServiceValidationError) as error:
        await _get_forecast(hass, entry.entry_id)
    assert error.value.translation_key == "entry_not_loaded"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator is not coordinator
    assert (await _get_forecast(hass, entry.entry_id))["coverage"]["complete"]
    assert client_fetch.await_count == 2


async def test_action_requires_available_snapshot(hass, loaded_forecast) -> None:
    """Ein noch nicht vorhandener Snapshot ist kein leerer erfolgreicher Forecast."""

    entry, coordinator, _ = loaded_forecast
    coordinator.data = None
    with pytest.raises(ServiceValidationError) as error:
        await _get_forecast(hass, entry.entry_id)
    assert error.value.translation_key == "forecast_unavailable"


@pytest.mark.parametrize("owner", [True, False], ids=["eigentümer", "nur-lesend"])
async def test_action_allows_owner_and_read_only_user(
    hass, loaded_forecast, owner: bool
) -> None:
    """Auch normale Nutzer benötigen für die rein lesende Aktion keine Steuerrechte."""

    entry, _, client_fetch = loaded_forecast
    user = MockUser(is_owner=owner).add_to_hass(hass)
    if not owner:
        user.mock_policy({"entities": {"all": {"read": True}}})
    assert (await _get_forecast(hass, entry.entry_id, user_id=user.id))["intervals"]
    assert client_fetch.await_count == 1


@pytest.mark.parametrize(
    "permission",
    ["all_entry_entities", "one_entity", "other_entry", "control_only", "none"],
)
async def test_action_respects_restricted_entity_permissions(
    hass, loaded_forecast, permission: str
) -> None:
    """Berechtigungen auf eine andere Anlage oder einzelne Sensoren reichen nicht."""

    entry, _, client_fetch = loaded_forecast
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    user = MockUser().add_to_hass(hass)
    allowed = {}
    if permission == "all_entry_entities":
        allowed = {entity.entity_id: {"read": True} for entity in entities}
    elif permission == "one_entity":
        allowed = {entities[0].entity_id: {"read": True}}
    elif permission == "other_entry":
        other = MockConfigEntry(domain=DOMAIN)
        other.add_to_hass(hass)
        other_entity = er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, "other_today", config_entry=other
        )
        allowed = {other_entity.entity_id: {"read": True}}
    elif permission == "control_only":
        allowed = {entity.entity_id: {"control": True} for entity in entities}
    user.mock_policy({"entities": {"entity_ids": allowed}})

    if permission == "all_entry_entities":
        assert (await _get_forecast(hass, entry.entry_id, user_id=user.id))["intervals"]
    else:
        with pytest.raises(Unauthorized):
            await _get_forecast(hass, entry.entry_id, user_id=user.id)
    assert client_fetch.await_count == 1


async def test_action_rejects_unknown_and_inactive_user(hass, loaded_forecast) -> None:
    """Gelöschte und gesperrte Nutzer können keinen gespeicherten Snapshot auslesen."""

    entry, _, _ = loaded_forecast
    with pytest.raises(UnknownUser):
        await _get_forecast(hass, entry.entry_id, user_id="removed-user")
    user = MockUser(is_owner=True, is_active=False).add_to_hass(hass)
    with pytest.raises(Unauthorized):
        await _get_forecast(hass, entry.entry_id, user_id=user.id)


@pytest.mark.parametrize(
    ("timezone_name", "local_date", "hours"),
    [
        ("Europe/Berlin", date(2026, 3, 29), 47),
        ("Europe/Berlin", date(2026, 10, 25), 49),
        ("Asia/Kolkata", date(2026, 8, 23), 48),
    ],
    ids=["sommerzeit", "winterzeit", "halbstundenoffset"],
)
def test_coverage_uses_local_days_and_preserves_input_quality(
    timezone_name: str, local_date: date, hours: int
) -> None:
    """Abdeckung folgt den echten Tagesgrenzen; Fallbacks sind keine Zeitlücken."""

    start = datetime.combine(local_date, datetime.min.time(), ZoneInfo(timezone_name))
    start = start.astimezone(UTC)
    intervals = tuple(
        TotalForecastInterval(
            start=start + timedelta(hours=hour),
            end=start + timedelta(hours=hour + 1),
            energy_kwh=0,
            ac_power_kw=0,
            quality_flags=("gti_fallback", "temperature_fallback"),
        )
        for hour in range(hours)
    )
    forecast = ForecastResult(local_date, {}, DailyYield(0, 0), intervals)
    result = _serialize_forecast(forecast, timezone_name, None, True)
    assert result["coverage"]["complete"]
    assert result["fetched_at"] is None
    assert result["intervals"][0]["quality_flags"] == [
        "gti_fallback",
        "temperature_fallback",
    ]

    for incomplete in (
        (),
        intervals[1:],
        intervals[:-1],
        intervals[:1] + intervals[2:],
        (replace(intervals[0], is_complete=False), *intervals[1:]),
    ):
        response = _serialize_forecast(
            replace(forecast, total_intervals=incomplete), timezone_name, None, True
        )
        assert not response["coverage"]["complete"]
        if not incomplete:
            assert response["coverage"] == {
                "start": None,
                "end": None,
                "complete": False,
            }
