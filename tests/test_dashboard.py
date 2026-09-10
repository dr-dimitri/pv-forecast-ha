"""Bewusste Dashboard-Einrichtung ohne Lovelace-Dateien oder zusätzliche Abrufe."""

from asyncio import CancelledError
from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components import frontend
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.dashboard import DashboardManager, dashboard_path
from custom_components.pv_forecast.history_runtime import _configuration_id

from .helpers import configure_options, persisted_roof
from .test_config_flow import _advance_to_summary
from .test_forecast_horizon import HorizonSession


def entry_for(hass, *, enabled=False, title="PV · Zuhause", entry_id=None):
    entry = MockConfigEntry(
        **({"entry_id": entry_id} if entry_id else {}),
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={
            "latitude": 50,
            "longitude": 0,
            "time_zone": "UTC",
            "location_name": "Zuhause",
        },
        options={
            "roofs": [persisted_roof()],
            "dashboard_enabled": enabled,
            "dashboard_title": title,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def frontend_ready(hass):
    await async_get_integration(hass, "frontend")
    hass.config.components.add("frontend")
    hass.http = Mock(async_register_static_paths=AsyncMock())


async def test_default_does_not_install_frontend_or_touch_other_panels(hass):
    manager = DashboardManager(hass, entry_for(hass))
    await manager.async_sync()
    assert manager.status == "off"
    assert manager._cancel_listener is None
    assert hass.http is None
    assert "frontend" not in hass.config.components
    assert not hass.data.get(frontend.DATA_PANELS)
    manager.async_stop()


async def test_multiple_entries_register_once_update_and_remove_only_own_panel(hass):
    await frontend_ready(hass)
    a = entry_for(hass, enabled=True, entry_id="entry-a")
    b = entry_for(hass, enabled=True, entry_id="entry-b", title="PV · Ferienhaus")
    first, second = DashboardManager(hass, a), DashboardManager(hass, b)
    frontend.async_register_built_in_panel(hass, "test", frontend_url_path="fremd")
    foreign = hass.data[frontend.DATA_PANELS]["fremd"]
    await first.async_sync()
    await first.async_sync()
    await second.async_sync()
    panels = hass.data[frontend.DATA_PANELS]
    assert len(panels) == 3
    assert panels[first.path].sidebar_title == "PV · Zuhause"
    assert panels[first.path].config["config_entry_id"] == a.entry_id
    assert (
        panels[first.path].config["_panel_custom"]["module_url"]
        == f"/pv_forecast/pv-forecast-card.js?v={first.revision}"
    )
    assert panels[second.path].config["config_entry_id"] == b.entry_id
    hass.config_entries.async_update_entry(
        a, options=dict(a.options) | {"dashboard_title": "Neuer Titel"}
    )
    assert first.only_dashboard_options_changed()
    await first.async_sync()
    assert panels[first.path].sidebar_title == "Neuer Titel"
    assert first.path == dashboard_path(a.entry_id)
    first.async_stop()
    assert first.path not in panels
    assert panels["fremd"] is foreign
    assert second.path in panels
    second.async_stop()
    assert list(panels) == ["fremd"]
    # Ein Reload verwendet denselben Pfad ohne zusätzlich persistiertes Dashboard.
    restarted = DashboardManager(hass, a)
    await restarted.async_sync()
    assert list(panels) == ["fremd", first.path]
    restarted.async_stop()


async def test_late_frontend_and_unload_leave_no_pending_panel_listener(hass):
    first = DashboardManager(hass, entry_for(hass, enabled=True))
    await first.async_sync()
    assert first.status == "waiting"
    assert first._cancel_listener is not None
    first.async_stop()
    await frontend_ready(hass)
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "frontend"})
    await hass.async_block_till_done()
    assert first.path not in hass.data.get(frontend.DATA_PANELS, {})
    assert first._cancel_listener is None
    # Ein noch aktiver Eintrag dagegen wird nach dem Frontendstart verfügbar.
    hass.config.components.remove("frontend")
    second = DashboardManager(hass, entry_for(hass, enabled=True))
    await second.async_sync()
    await frontend_ready(hass)
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "frontend"})
    await hass.async_block_till_done()
    assert second.status == "ready"
    assert second._cancel_listener is None
    second.async_stop()


async def test_collisions_and_later_foreign_replacement_are_never_overwritten(hass):
    await frontend_ready(hass)
    manager = DashboardManager(hass, entry_for(hass, enabled=True))
    frontend.async_register_built_in_panel(hass, "test", frontend_url_path=manager.path)
    foreign = hass.data[frontend.DATA_PANELS][manager.path]
    await manager.async_sync()
    assert manager.status == "conflict"
    manager.async_stop()
    assert hass.data[frontend.DATA_PANELS][manager.path] is foreign
    frontend.async_remove_panel(hass, manager.path)
    active = DashboardManager(hass, manager.entry)
    await active.async_sync()
    frontend.async_register_built_in_panel(
        hass, "test", frontend_url_path=manager.path, update=True
    )
    replacement = hass.data[frontend.DATA_PANELS][manager.path]
    active.async_stop()
    assert hass.data[frontend.DATA_PANELS][manager.path] is replacement


async def test_panel_failure_does_not_fail_forecast_or_register_retries(hass):
    await frontend_ready(hass)
    manager = DashboardManager(hass, entry_for(hass, enabled=True))
    with patch(
        "homeassistant.components.panel_custom.async_register_panel",
        side_effect=ValueError("Test"),
    ):
        await manager.async_sync()
    assert manager.status == "error"
    assert manager._cancel_listener is None
    assert manager.path not in hass.data.get(frontend.DATA_PANELS, {})
    manager.async_stop()


async def test_stop_during_translation_load_does_not_register_panel(hass):
    await frontend_ready(hass)
    manager = DashboardManager(hass, entry_for(hass, enabled=True))

    async def load(*args, **kwargs):
        manager.async_stop()
        return {}

    with patch(
        "custom_components.pv_forecast.dashboard.async_get_translations",
        side_effect=load,
    ):
        await manager.async_sync()
    assert manager.path not in hass.data.get(frontend.DATA_PANELS, {})


async def test_config_flow_preview_creates_no_panel_until_setup_is_finished(hass):
    result = await _advance_to_summary(hass)
    assert "dashboard" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "dashboard"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"dashboard_enabled": True, "dashboard_title": " "}
    )
    assert result["errors"]["base"] == "invalid_dashboard"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"dashboard_enabled": True, "dashboard_title": "Meine PV"}
    )
    assert result["step_id"] == "summary"
    assert (
        result["description_placeholders"]["dashboard"]
        == "Nach Abschluss in der Seitenleiste anzeigen"
    )
    assert not hass.data.get(frontend.DATA_PANELS)
    with patch("custom_components.pv_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    assert result["options"]["dashboard_enabled"] is True
    assert result["options"]["dashboard_title"] == "Meine PV"
    assert result["options"]["roofs"]


async def test_options_enable_rename_disable_without_weather_reload_or_lost_options(
    hass, freezer
):
    freezer.move_to("2026-09-09T10:00:00Z")
    await frontend_ready(hass)
    entry = entry_for(hass)
    session = HorizonSession()
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    original_runtime = entry.runtime_data
    fetched = original_runtime.coordinator.last_update_success_time
    original_data = original_runtime.coordinator.data
    original_configuration_id = _configuration_id(entry)
    calls = session.calls
    for enabled, title in (
        (True, "PV · Aktiv"),
        (True, "Neuer Titel"),
        (False, "Neuer Titel"),
        (True, "Wieder aktiv"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await configure_options(
            hass, result["flow_id"], {"next_step_id": "dashboard"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"dashboard_enabled": enabled, "dashboard_title": title}
        )
        await hass.async_block_till_done()
        assert result["data"]["roofs"] == [persisted_roof()]
        assert entry.runtime_data is original_runtime
        assert original_runtime.coordinator.data is original_data
        assert original_runtime.coordinator.last_update_success_time == fetched
        assert session.calls == calls
        assert _configuration_id(entry) == original_configuration_id
        assert (
            dashboard_path(entry.entry_id) in hass.data.get(frontend.DATA_PANELS, {})
        ) == enabled
        assert (entry.version, entry.minor_version) == (1, 1)
    # Fachliche Änderungen dürfen den bisherigen Reload nicht versehentlich umgehen.
    options = deepcopy(dict(entry.options))
    options["roofs"][0]["installed_power_kwp"] = 20
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        hass.config_entries.async_update_entry(entry, options=options)
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert dashboard_path(entry.entry_id) not in hass.data[frontend.DATA_PANELS]
    assert original_runtime.dashboard.status == "off"
    assert original_runtime.dashboard._cancel_listener is None


async def test_removing_active_entry_removes_its_dashboard(hass, freezer):
    freezer.move_to("2026-09-09T10:00:00Z")
    await frontend_ready(hass)
    entry = entry_for(hass, enabled=True)
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=HorizonSession(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    manager = entry.runtime_data.dashboard
    assert manager.path in hass.data[frontend.DATA_PANELS]
    await hass.config_entries.async_remove(entry.entry_id)
    assert manager.path not in hass.data[frontend.DATA_PANELS]
    assert manager._cancel_listener is None
    assert manager.status == "off"


@pytest.mark.parametrize("failure", [RuntimeError, CancelledError])
async def test_setup_failure_cleans_up_an_already_registered_panel(
    hass, freezer, failure
):
    freezer.move_to("2026-09-09T10:00:00Z")
    await frontend_ready(hass)
    entry = entry_for(hass, enabled=True)
    sync = DashboardManager.async_sync

    async def fail_after_registration(manager):
        await sync(manager)
        assert manager.path in hass.data[frontend.DATA_PANELS]
        raise failure("Testabbruch")

    with (
        patch(
            "custom_components.pv_forecast.runtime.async_get_clientsession",
            return_value=HorizonSession(),
        ),
        patch.object(DashboardManager, "async_sync", fail_after_registration),
    ):
        # HA übersetzt auch einen Setup-Abbruch in einen fehlgeschlagenen Eintrag.
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert dashboard_path(entry.entry_id) not in hass.data[frontend.DATA_PANELS]
    assert entry.runtime_data.dashboard._cancel_listener is None
    assert entry.runtime_data.dashboard.status == "off"


async def test_pending_dashboard_options_keep_a_real_location_change_on_reload(hass):
    entry = entry_for(hass)
    manager = DashboardManager(hass, entry)
    assert manager.only_dashboard_options_changed()
    hass.config_entries.async_update_entry(
        entry, data=dict(entry.data) | {"latitude": 20}
    )
    assert not manager.only_dashboard_options_changed()


async def test_dashboard_texts_are_complete_for_config_and_options(hass):
    for category, step in (("config", "summary"), ("options", "init")):
        texts = await async_get_translations(
            hass, "de", category, integrations={DOMAIN}
        )
        assert texts[
            f"component.{DOMAIN}.{category}.step.{step}.menu_options.dashboard"
        ] == ("PV-Dashboard einrichten" if category == "config" else "Dashboard")
        assert (
            "Ressourcenregistrierung und YAML entfallen"
            in texts[f"component.{DOMAIN}.{category}.step.dashboard.description"]
        )
