"""Veraltete Optionsdialoge dürfen neuere Einstellungen und Daten nicht verändern."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.measurement_helpers import (
    _wait_for_helper,
    async_resolve_measurement_helpers,
)
from custom_components.pv_forecast.measurements import SourceConfig

from .helpers import configure_options, persisted_roof
from .test_measurement_adapters import CONFIRM_DEVICE, draft, ksem


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "Europe/Berlin"},
        options={
            "roofs": [persisted_roof("roof")],
            "inverter_max_power_kw": 8,
            "history_enabled": True,
            "measurement_sources": [
                SourceConfig("solar", "sensor.pv", "total", "Gesamte AC-PV").to_dict()
            ],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _choose(hass, result, step):
    return await configure_options(hass, result["flow_id"], {"next_step_id": step})


async def _menu(hass, entry, menu):
    return await _choose(
        hass, await hass.config_entries.options.async_init(entry.entry_id), menu
    )


@pytest.mark.parametrize("menu", ["history", "measurements", "calibration"])
async def test_older_dialog_cannot_revert_a_saved_inverter_limit(hass, menu):
    """Zwei echte HA-Optionsflows werden beim älteren Abschluss sicher getrennt."""
    entry = _entry(hass)
    older = await _menu(hass, entry, menu)
    newer = await _menu(hass, entry, "system")
    assert (
        len(hass.config_entries.options.async_progress_by_handler(entry.entry_id)) == 2
    )
    await hass.config_entries.options.async_configure(
        newer["flow_id"], {"inverter_max_power_kw": 5}
    )
    saved = deepcopy(dict(entry.options))
    result = await _choose(hass, older, f"{menu}_done")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_entry_changed"
    assert dict(entry.options) == saved
    assert entry.options["inverter_max_power_kw"] == 5


@pytest.mark.parametrize("change", ["roof", "source", "location"])
async def test_older_archive_draft_preserves_newer_configuration(hass, change):
    """Dach-, Quellen- und Standortänderungen entziehen alten Entwürfen die Freigabe."""
    entry = _entry(hass)
    result = await _menu(hass, entry, "history")
    if change == "location":
        hass.config_entries.async_update_entry(
            entry, data=dict(entry.data) | {"time_zone": "UTC"}
        )
    else:
        changes = (
            {"roofs": [persisted_roof("new-roof")]}
            if change == "roof"
            else {"measurement_sources": []}
        )
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | changes
        )
    saved_data, saved_options = dict(entry.data), deepcopy(dict(entry.options))
    result = await _choose(hass, result, "history_done")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_entry_changed"
    assert dict(entry.data) == saved_data
    assert dict(entry.options) == saved_options


async def test_open_roof_editor_does_not_overwrite_newer_values(hass):
    """Auch ein einstufiger Editor schützt die vor dem Ausfüllen gelesenen Werte."""
    entry = _entry(hass)
    editor = await _menu(hass, entry, "edit_roof")
    editor = await hass.config_entries.options.async_configure(
        editor["flow_id"], {"id": "roof"}
    )
    user_input = editor["data_schema"]({})
    newer = await _menu(hass, entry, "system")
    await hass.config_entries.options.async_configure(
        newer["flow_id"], {"inverter_max_power_kw": 5}
    )
    result = await hass.config_entries.options.async_configure(
        editor["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_entry_changed"
    assert entry.options["inverter_max_power_kw"] == 5


@pytest.mark.parametrize("previous_revision", [None, "previous"])
async def test_internal_dashboard_revision_does_not_conflict_or_get_reverted(
    hass, previous_revision
):
    """Eine Dashboardreparatur bleibt beim Speichern von Archivoptionen erhalten."""
    entry = _entry(hass)
    if previous_revision is not None:
        hass.config_entries.async_update_entry(
            entry,
            options=dict(entry.options) | {"dashboard_revision": previous_revision},
        )
    result = await _menu(hass, entry, "history")
    result = await _choose(hass, result, "history_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"history_enabled": False}
    )
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"dashboard_revision": "current"}
    )
    result = await _choose(hass, result, "history_done")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["dashboard_revision"] == "current"
    assert entry.options["history_enabled"] is False


@pytest.mark.parametrize(
    "menu,action,confirmation,operation",
    [
        ("history", "delete_history", {"confirm_delete": True}, "history"),
        ("calibration", "reset_calibration", {"confirm_reset": True}, "calibration"),
        (
            "measurements",
            "delete_measurement_data",
            {"confirm_delete": True},
            "measurement",
        ),
        (
            "history",
            "underperformance_control",
            {"action": "clear", "confirm": True},
            "observation",
        ),
    ],
)
async def test_conflict_prevents_immediate_data_changes(
    hass, menu, action, confirmation, operation
):
    """Ein Standortwechsel stoppt bestätigte Löschungen aus dem vorherigen Dialog."""
    entry = _entry(hass)
    observation = AsyncMock()
    entry.runtime_data = SimpleNamespace(
        history=SimpleNamespace(loaded=True, async_observation_control=observation)
    )
    result = await _choose(hass, await _menu(hass, entry, menu), action)
    if operation == "measurement":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"source_id": "solar"}
        )
    hass.config_entries.async_update_entry(
        entry, data=dict(entry.data) | {"latitude": 53}
    )
    with (
        patch(
            "custom_components.pv_forecast.history_runtime.async_delete_history_data"
        ) as history,
        patch(
            "custom_components.pv_forecast.calibration_runtime.async_delete_calibration_data"
        ) as calibration,
        patch(
            "custom_components.pv_forecast.measurement_runtime.async_delete_measurement_source_data"
        ) as measurement,
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], confirmation
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_entry_changed"
        for mock in (history, calibration, measurement, observation):
            mock.assert_not_called()


async def _guided_source(hass, entry, sensor):
    result = await _choose(
        hass, await _menu(hass, entry, "measurements"), "add_measurement"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": f"ksem:{sensor.id}"}
    )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], CONFIRM_DEVICE
    )


async def test_conflict_while_waiting_for_helper_lock_creates_no_helper(hass):
    """Nach Warten auf einen anderen Abschluss wird die Konfiguration erneut geprüft."""
    entry = _entry(hass)
    _, sensor = ksem(hass)
    result = await _guided_source(hass, entry, sensor)
    lock = asyncio.Lock()
    hass.data[f"{DOMAIN}_measurement_helper_lock"] = lock
    await lock.acquire()
    save = hass.async_create_task(_choose(hass, result, "measurements_done"))
    await asyncio.sleep(0)
    assert not save.done()
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"inverter_max_power_kw": 5}
    )
    lock.release()
    result = await save
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_entry_changed"
    assert not hass.config_entries.async_entries("integration")
    assert entry.options["inverter_max_power_kw"] == 5


@pytest.mark.parametrize("existing_helper", [False, True])
async def test_conflict_during_helper_setup_rolls_back_only_new_helpers(
    hass, existing_helper
):
    """Ein Konflikt nach dem asynchronen Setup lässt keine neuen Helfer zurück."""
    entry = _entry(hass)
    _, sensor = ksem(hass)
    if existing_helper:
        await async_resolve_measurement_helpers(hass, [draft(sensor)])
    result = await _guided_source(hass, entry, sensor)

    async def finish_then_change(hass, helper):
        registered = await _wait_for_helper(hass, helper)
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"inverter_max_power_kw": 5}
        )
        return registered

    with patch(
        "custom_components.pv_forecast.measurement_helpers._wait_for_helper",
        side_effect=finish_then_change,
    ):
        result = await _choose(hass, result, "measurements_done")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_entry_changed"
    assert len(hass.config_entries.async_entries("integration")) == int(existing_helper)
    assert entry.options["inverter_max_power_kw"] == 5
    assert len(entry.options["measurement_sources"]) == 1


async def test_cancelled_helper_setup_rolls_back_new_helper(hass):
    """Abgebrochenes Speichern hinterlässt weder Helfer noch Quellenänderung."""
    entry = _entry(hass)
    original_options = deepcopy(dict(entry.options))
    _, sensor = ksem(hass)
    result = await _guided_source(hass, entry, sensor)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_after_setup(hass, helper):
        registered = await _wait_for_helper(hass, helper)
        waiting.set()
        await release.wait()
        return registered

    with patch(
        "custom_components.pv_forecast.measurement_helpers._wait_for_helper",
        side_effect=wait_after_setup,
    ):
        save = hass.async_create_task(_choose(hass, result, "measurements_done"))
        await waiting.wait()
        save.cancel()
        with pytest.raises(asyncio.CancelledError):
            await save
    assert not hass.config_entries.async_entries("integration")
    assert dict(entry.options) == original_options
