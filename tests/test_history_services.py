"""Archivaktionen, bewusste Exporte und die Grenzen historischer Leserechte."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.history_services import (
    SERVICE_EXPORT_HISTORY,
    SERVICE_GET_HISTORY,
    async_setup_history_services,
)

from .helpers import persisted_roof, weather


@pytest.fixture
async def archived_entry(hass, freezer):
    """Eine rechtzeitige Tagesprognose und einen genau begrenzten Isttag erfassen."""

    freezer.move_to("2026-09-08T15:50:00Z")
    source = er.async_get(hass).async_get_or_create(
        "sensor", "test", "archive_solar_counter"
    )
    attributes = {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
    }
    hass.states.async_set(source.entity_id, "0", attributes)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "Europe/Berlin"},
        options={
            "roofs": [persisted_roof("a")],
            "history_enabled": True,
            "measurement_sources": [
                {
                    "source_id": "actual-a",
                    "entity_id": source.entity_id,
                    "registry_id": source.id,
                    "kind": "total",
                    "scope": "AC-Gesamterzeugung",
                    "confirmed_pv": True,
                    "confirmed_disjoint": True,
                    "max_interval_minutes": 1440,
                }
            ],
        },
    )
    entry.add_to_hass(hass)
    start = datetime(2026, 9, 7, 22, tzinfo=UTC)
    intervals = tuple(
        weather(end=start + timedelta(hours=hour)) for hour in range(1, 49)
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={"a": intervals},
    ) as fetch:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        freezer.move_to("2026-09-08T22:00:00Z")
        hass.states.async_set(source.entity_id, "0", attributes)
        await hass.async_block_till_done()
        freezer.move_to("2026-09-09T22:00:00Z")
        hass.states.async_set(source.entity_id, "10", attributes)
        await hass.async_block_till_done()
        freezer.move_to("2026-09-09T22:01:00Z")
        entry.runtime_data.coordinator.async_update_listeners()
        await hass.async_block_till_done()
        yield entry, source, fetch
        if hass.config_entries.async_get_entry(entry.entry_id) is not None:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def _history(
    hass, entry_id, *, user_id=None, service=SERVICE_GET_HISTORY, **data
):
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"config_entry_id": entry_id, **data},
        blocking=True,
        return_response=True,
        context=Context(user_id=user_id),
    )


async def test_history_actions_require_selection_and_explicit_export_format(hass):
    """Archivdaten werden nur für die ausdrücklich ausgewählte Anlage ausgegeben."""

    async_setup_history_services(hass)
    for service in (SERVICE_GET_HISTORY, SERVICE_EXPORT_HISTORY):
        assert hass.services.supports_response(DOMAIN, service) is SupportsResponse.ONLY
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, service, {}, blocking=True, return_response=True
            )
    with pytest.raises(vol.Invalid):
        await _history(hass, "missing", service=SERVICE_EXPORT_HISTORY)
    with pytest.raises(ServiceValidationError) as error:
        await _history(hass, "missing")
    assert error.value.translation_key == "entry_not_found"


@pytest.mark.parametrize("days", [0, 1, 7.5, 365, "alle"])
async def test_history_action_rejects_unsupported_windows(hass, days):
    """Die angegebenen Bewertungsfenster werden nicht still gerundet oder erweitert."""

    async_setup_history_services(hass)
    with pytest.raises(vol.Invalid):
        await _history(hass, "missing", days=days)


async def test_history_read_is_json_serializable_and_does_not_fetch(
    hass, archived_entry
):
    """Ein Archivaufruf nutzt ausschließlich gespeicherte Prognosen und Messungen."""

    entry, _, fetch = archived_entry
    coordinator = entry.runtime_data.coordinator
    old_forecast = coordinator.data
    old_timestamp = coordinator.last_update_success_time
    fetch_count = fetch.await_count
    manager = entry.runtime_data.history
    with patch.object(manager, "snapshot", wraps=manager.snapshot) as snapshot:
        result = await _history(hass, entry.entry_id, days="7", include_records=True)
    assert result["schema_version"] == 1
    assert result["horizons"]["daily_previous_18"]["count_valid"] == 1
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert snapshot.call_args.args == (
        7,
        datetime(2026, 9, 9, 22, 1, tzinfo=UTC),
    )
    assert snapshot.call_args.kwargs == {"include_records": True}
    assert coordinator.data is old_forecast
    assert coordinator.last_update_success_time == old_timestamp
    assert fetch.await_count == fetch_count


@pytest.mark.parametrize("output_format", ["json", "csv"])
async def test_explicit_export_keeps_source_data_and_uses_no_network(
    hass, archived_entry, output_format
):
    """Bewusste JSON-/CSV-Exporte lassen die archivierten Daten unverändert."""

    entry, _, fetch = archived_entry
    fetch_count = fetch.await_count
    before = await _history(hass, entry.entry_id, days=7, include_records=True)
    result = await _history(
        hass,
        entry.entry_id,
        service=SERVICE_EXPORT_HISTORY,
        days=7,
        format=output_format,
    )
    assert result["filename"] == f"pv-forecast-archiv-7-tage.{output_format}"
    if output_format == "json":
        assert result["mime_type"] == "application/json"
        assert json.loads(result["content"]) == before
    else:
        assert result["mime_type"] == "text/csv"
        rows = list(csv.DictReader(io.StringIO(result["content"])))
        assert len(rows) == len(before["records"])
        assert any(row["horizon"] == "daily_previous_18" for row in rows)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert await _history(hass, entry.entry_id, days=7, include_records=True) == before
    assert fetch.await_count == fetch_count


async def test_history_requires_read_permission_on_actual_source(hass, archived_entry):
    """Prognose-Entities erlauben keinen indirekten Zugriff auf den Ertragszähler."""

    entry, source, _ = archived_entry
    user = MockUser().add_to_hass(hass)
    allowed = {
        entity.entity_id: {"read": True}
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    user.mock_policy({"entities": {"entity_ids": allowed}})
    with pytest.raises(Unauthorized):
        await _history(hass, entry.entry_id, include_records=True, user_id=user.id)
    allowed[source.entity_id] = {"read": True}
    user.mock_policy({"entities": {"entity_ids": allowed}})
    assert await _history(hass, entry.entry_id, user_id=user.id)


async def test_history_actions_remain_registered_after_unload(hass, archived_entry):
    """Entladene Archive werden kontrolliert zurückgewiesen."""

    entry, _, _ = archived_entry
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_HISTORY)
    with pytest.raises(ServiceValidationError) as error:
        await _history(hass, entry.entry_id)
    assert error.value.translation_key == "entry_not_loaded"


async def test_current_targets_are_explicit_and_use_existing_permissions(
    hass, archived_entry
):
    """Die neue Kartenansicht ergänzt die Antwort nur bei bewusster Anforderung."""

    entry, source, fetch = archived_entry
    previous = await _history(hass, entry.entry_id, days=7)
    assert "current_targets" not in previous
    fetch_count = fetch.await_count
    result = await _history(hass, entry.entry_id, days=7, current_targets=True)
    targets = result.pop("current_targets")
    assert result == previous
    assert targets["view_version"] == 1
    assert targets["timezone"] == "Europe/Berlin"
    assert targets["horizon"] == "hourly_1h"
    assert fetch.await_count == fetch_count
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {source.entity_id: {"read": True}}}})
    with pytest.raises(Unauthorized):
        await _history(hass, entry.entry_id, current_targets=True, user_id=user.id)
