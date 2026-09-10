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


async def _get_forecast(
    hass, entry_id: str, *, user_id: str | None = None, **parameters
):
    """Den öffentlichen HA-Aktionsweg einschließlich Antwortvalidierung verwenden."""

    return await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_FORECAST,
        {"config_entry_id": entry_id, **parameters},
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


async def test_card_view_is_additive_and_preserves_default_forecast(
    hass, loaded_forecast
) -> None:
    """Eine Dachansicht erhält den bisherigen Gesamtvertrag und Abrufstand."""
    entry, coordinator, client_fetch = loaded_forecast
    default = await _get_forecast(hass, entry.entry_id)
    assert "view" not in default
    result = await _get_forecast(
        hass, entry.entry_id, include_view=True, day="tomorrow", roof_id="b"
    )
    selected = result.pop("view")
    assert result == default
    assert selected["view_version"] == 1
    assert selected["day"] == "tomorrow"
    assert selected["date"] == "2026-08-24"
    assert selected["plant_name"] == entry.title
    assert selected["roof_id"] == "b"
    assert selected["summary"] == {
        "today_kwh": 180,
        "tomorrow_kwh": 180,
        "remaining_today_kwh": 75,
    }
    assert all(item["energy_kwh"] == 7.5 for item in selected["intervals"])
    assert coordinator.data.total.today == 360
    assert client_fetch.await_count == 1


async def test_planning_uses_shared_forecast_and_is_json_serializable(
    hass, loaded_forecast
) -> None:
    """Die Automationsantwort enthält genau das reine Planungsergebnis ohne HTTP."""
    from custom_components.pv_forecast.planning import plan_solar_window

    entry, coordinator, client_fetch = loaded_forecast
    parameters = {
        "duration_minutes": 120,
        "earliest_start": "2026-08-23T12:00:00+00:00",
        "latest_end": "2026-08-23T18:00:00+00:00",
    }
    response = await _get_forecast(hass, entry.entry_id, planning=parameters)
    expected = plan_solar_window(
        coordinator.data,
        "Europe/Berlin",
        datetime(2026, 8, 23, 12, tzinfo=UTC),
        coordinator.last_update_success_time,
        coordinator.last_update_success,
        duration_minutes=120,
        earliest_start=datetime.fromisoformat(parameters["earliest_start"]),
        latest_end=datetime.fromisoformat(parameters["latest_end"]),
    )
    assert response["planning"] == expected
    assert response["planning"]["energy_kwh"] == 30
    assert json.loads(json.dumps(response)) == response
    assert client_fetch.await_count == 1


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"duration_minutes": True},
        {"duration_minutes": 1.5},
        {"earliest_start": "2026-08-23T12:00:00"},
        {"latest_end": "kein Zeitpunkt"},
    ],
)
async def test_planning_validates_explicit_bounds(hass, loaded_forecast, parameters):
    entry, _, client_fetch = loaded_forecast
    valid = {
        "duration_minutes": 120,
        "earliest_start": "2026-08-23T12:00:00+00:00",
        "latest_end": "2026-08-23T18:00:00+00:00",
    }
    with pytest.raises(vol.Invalid):
        await _get_forecast(
            hass, entry.entry_id, planning={**valid, **parameters} if parameters else {}
        )
    assert client_fetch.await_count == 1


@pytest.mark.parametrize("stale", [False, True])
async def test_blueprint_runs_real_read_action_and_only_requested_notification(
    hass, loaded_forecast, stale
) -> None:
    """Das importierbare Beispiel wird im nativen HA-Script-Runner ausgeführt."""
    from pathlib import Path

    from homeassistant.components.blueprint import Blueprint, BlueprintInputs
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
    from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA
    from homeassistant.helpers.script import Script
    from homeassistant.util.yaml import load_yaml

    entry, coordinator, client_fetch = loaded_forecast
    path = (
        Path(__file__).parents[1]
        / "blueprints/script/pv_forecast/solarzeitfenster.yaml"
    )
    blueprint = Blueprint(load_yaml(str(path)), schema=BLUEPRINT_SCHEMA)
    inputs = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": "pv_forecast/solarzeitfenster.yaml",
                "input": {
                    "plant": entry.entry_id,
                    "duration": 120,
                    "earliest": "2026-08-23T12:00:00+00:00",
                    "latest": "2026-08-23T18:00:00+00:00",
                },
            }
        },
    )
    inputs.validate()
    configuration = SCRIPT_ENTITY_SCHEMA(inputs.async_substitute())
    notifications = []
    hass.services.async_register(
        "persistent_notification",
        "create",
        lambda call: notifications.append(call.data),
    )
    if stale:
        coordinator.async_set_update_error(UpdateFailed("Offline"))
    script = Script(
        hass,
        configuration["sequence"],
        "Solarzeitfenster",
        "script",
        variables=configuration.get("variables"),
    )
    await script.async_run(context=Context())
    assert len(notifications) == 1
    if stale:
        assert notifications[0]["title"] == "Kein belastbares Solarzeitfenster"
    else:
        assert notifications[0]["title"] == "Solarzeitfenster"
        assert "30" in notifications[0]["message"]
        assert "12:00:00+00:00" in notifications[0]["message"]
    assert client_fetch.await_count == 1


async def test_card_view_rejects_unknown_roof_without_extra_fetch(
    hass, loaded_forecast
) -> None:
    """Eine entfernte Dachfläche liefert einen verständlichen Auswahlfehler."""
    entry, _, client_fetch = loaded_forecast
    with pytest.raises(ServiceValidationError) as error:
        await _get_forecast(hass, entry.entry_id, include_view=True, roof_id="removed")
    assert error.value.translation_key == "roof_not_found"
    assert client_fetch.await_count == 1


async def test_card_view_uses_existing_read_permission_without_control_rights(
    hass, loaded_forecast
) -> None:
    """Die Kartenansicht verwendet die vorhandenen Anlagen-Leserechte."""
    entry, _, _ = loaded_forecast
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"all": {"read": True}}})
    assert (
        await _get_forecast(hass, entry.entry_id, user_id=user.id, include_view=True)
    )["view"]
    user.mock_policy({"entities": {"all": {"control": True}}})
    with pytest.raises(Unauthorized):
        await _get_forecast(hass, entry.entry_id, user_id=user.id, include_view=True)


async def test_card_parameters_reject_unsupported_day(hass, loaded_forecast) -> None:
    """Die optionale Darstellung erweitert den fachlichen Prognosezeitraum nicht."""
    entry, _, _ = loaded_forecast
    with pytest.raises(vol.Invalid):
        await _get_forecast(hass, entry.entry_id, include_view=True, day="yesterday")


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


async def test_window_and_planning_share_read_only_snapshot(hass, loaded_forecast):
    """Beide Ergänzungen teilen Zeit, Rechte und Prognose ohne neue Abrufe."""
    entry, coordinator, client_fetch = loaded_forecast
    data, fetched = coordinator.data, coordinator.last_update_success_time
    calls = client_fetch.call_count
    parameters = {
        "start": "2026-08-23T12:15:00Z",
        "end": "2026-08-23T15:15:00Z",
        "step_minutes": 30,
    }
    result = await _get_forecast(
        hass,
        entry.entry_id,
        roof_id="a",
        include_view=True,
        window=parameters,
        planning={
            "duration_minutes": 60,
            "earliest_start": parameters["start"],
            "latest_end": parameters["end"],
        },
    )
    assert result["window"]["status"] == "available"
    assert result["window"]["scope"] == "total"
    assert result["window"]["as_of"] == result["planning"]["as_of"]
    assert result["window"]["energy_kwh"] == pytest.approx(45)
    assert client_fetch.call_count == calls
    assert coordinator.data is data
    assert coordinator.last_update_success_time == fetched
    coordinator.last_update_success = False
    result = await _get_forecast(hass, entry.entry_id, window=parameters)
    assert result["window"]["reason"] == "stale_forecast"
    assert result["intervals"]


@pytest.mark.parametrize(
    "window",
    [
        None,
        [],
        True,
        {},
        {"start": "2026-08-23T12:00:00", "end": "2026-08-23T13:00:00Z"},
        {
            "start": "2026-08-23T12:00:00Z",
            "end": "2026-08-23T13:00:00Z",
            "step_minutes": True,
        },
    ],
)
async def test_invalid_window_has_translated_error(hass, loaded_forecast, window):
    entry, _, _ = loaded_forecast
    with pytest.raises(ServiceValidationError) as err:
        await _get_forecast(hass, entry.entry_id, window=window)
    assert err.value.translation_key == "invalid_forecast_window"
