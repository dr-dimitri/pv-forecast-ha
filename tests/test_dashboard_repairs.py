"""Inhaltsänderungen und bewusste Aktualisierung über den nativen HA-Reparaturpfad."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components import frontend, repairs
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.dashboard import (
    CONF_DASHBOARD_REVISION,
    DashboardManager,
    dashboard_issue_id,
)
from custom_components.pv_forecast.frontend import async_get_card_revision

from .test_dashboard import entry_for, frontend_ready
from .test_forecast_horizon import HorizonSession


@pytest.fixture
def module_file(tmp_path):
    """Ein lokales Testmodul verändert sich unabhängig von einer Versionsnummer."""
    path = tmp_path / "card.js"
    path.write_text("export const beispiel = 1;", encoding="utf-8")
    with patch("custom_components.pv_forecast.frontend.CARD_FILE", path):
        yield path


def issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, dashboard_issue_id(entry.entry_id)
    )


async def ready_manager(hass):
    await frontend_ready(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = entry_for(hass, enabled=True)
    manager = DashboardManager(hass, entry)
    entry.runtime_data = SimpleNamespace(dashboard=manager)
    await manager.async_sync()
    return entry, manager


async def start_repair(hass, entry):
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs.repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": dashboard_issue_id(entry.entry_id)}
    )
    return manager, result


async def test_first_setup_and_unchanged_reload_establish_a_silent_baseline(
    hass, module_file
):
    entry, manager = await ready_manager(hass)
    baseline = entry.options[CONF_DASHBOARD_REVISION]
    assert baseline == await async_get_card_revision(hass)
    assert len(baseline) == 64
    assert issue(hass, entry) is None
    manager.async_stop()
    restarted = DashboardManager(hass, entry)
    await restarted.async_sync()
    assert restarted.revision == baseline
    assert issue(hass, entry) is None
    assert entry.options[CONF_DASHBOARD_REVISION] == baseline
    restarted.async_stop()


async def test_changed_bytes_create_one_issue_preserving_ignore_until_next_change(
    hass, module_file
):
    entry, manager = await ready_manager(hass)
    original = entry.options[CONF_DASHBOARD_REVISION]
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await manager.async_sync()
    first = issue(hass, entry)
    assert first.is_fixable and first.is_persistent
    assert first.data["revision"] != original
    assert entry.options[CONF_DASHBOARD_REVISION] == original
    ir.async_ignore_issue(hass, DOMAIN, first.issue_id, True)
    ignored = issue(hass, entry)
    await manager.async_sync()
    assert issue(hass, entry).created == first.created
    assert issue(hass, entry).dismissed_version == ignored.dismissed_version
    # Persistierte Meldungsdaten enthalten die Revision auch nach einem HA-Neustart.
    assert ignored.to_json()["data"]["revision"] == manager.revision
    manager.async_stop()
    manager = DashboardManager(hass, entry)
    await manager.async_sync()
    assert issue(hass, entry).dismissed_version == ignored.dismissed_version
    module_file.write_text("export const beispiel = 3;", encoding="utf-8")
    await manager.async_sync()
    assert issue(hass, entry).dismissed_version is None
    assert issue(hass, entry).data["revision"] != first.data["revision"]
    manager.async_stop()


async def test_native_repair_updates_only_dashboard_and_supplies_a_reload_link(
    hass, freezer, module_file
):
    freezer.move_to("2026-09-09T10:00:00Z")
    await frontend_ready(hass)
    entry = entry_for(hass, enabled=True)
    session = HorizonSession()
    with patch(
        "custom_components.pv_forecast.runtime.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    forecast = runtime.coordinator.data
    fetched = runtime.coordinator.last_update_success_time
    calls = session.calls
    original_options = dict(entry.options)
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await runtime.dashboard.async_sync()
    flow, result = await start_repair(hass, entry)
    assert result["step_id"] == "confirm"
    assert entry.options == original_options
    result = await flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["reason"] == "updated"
    revision = await async_get_card_revision(hass)
    assert entry.options == original_options | {CONF_DASHBOARD_REVISION: revision}
    assert result["description_placeholders"]["dashboard_url"] == (
        f"/{runtime.dashboard.path}?pv_revision={revision}"
    )
    assert issue(hass, entry) is None
    panel = hass.data[frontend.DATA_PANELS][runtime.dashboard.path]
    assert panel.config["_panel_custom"]["module_url"].endswith(f"?v={revision}")
    assert entry.runtime_data is runtime
    assert runtime.coordinator.data is forecast
    assert runtime.coordinator.last_update_success_time == fetched
    assert session.calls == calls
    assert (entry.version, entry.minor_version) == (1, 1)
    module_file.write_text("export const beispiel = 3;", encoding="utf-8")
    await runtime.dashboard.async_sync()
    assert issue(hass, entry) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    assert issue(hass, entry) is None


async def test_failed_registration_keeps_old_revision_and_repair(hass, module_file):
    entry, manager = await ready_manager(hass)
    old_revision = entry.options[CONF_DASHBOARD_REVISION]
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await manager.async_sync()
    flow, result = await start_repair(hass, entry)
    frontend.async_register_built_in_panel(
        hass, "test", frontend_url_path=manager.path, update=True
    )
    foreign = hass.data[frontend.DATA_PANELS][manager.path]
    result = await flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "dashboard_unavailable"}
    assert entry.options[CONF_DASHBOARD_REVISION] == old_revision
    assert issue(hass, entry) is not None
    assert hass.data[frontend.DATA_PANELS][manager.path] is foreign
    manager.async_stop()


async def test_changed_module_during_dialog_does_not_acknowledge_unseen_version(
    hass, module_file
):
    entry, manager = await ready_manager(hass)
    baseline = entry.options[CONF_DASHBOARD_REVISION]
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await manager.async_sync()
    flow, result = await start_repair(hass, entry)
    module_file.write_text("export const beispiel = 3;", encoding="utf-8")
    result = await flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "changed"
    assert entry.options[CONF_DASHBOARD_REVISION] == baseline
    assert issue(hass, entry).data["revision"] == await async_get_card_revision(hass)
    manager.async_stop()


async def test_disabled_removed_and_unloaded_entries_are_checked_again(
    hass, module_file
):
    entry, manager = await ready_manager(hass)
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await manager.async_sync()
    flow, result = await start_repair(hass, entry)
    manager.async_stop()
    result = await flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "not_loaded"
    assert issue(hass, entry) is not None
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"dashboard_enabled": False}
    )
    _, result = await start_repair(hass, entry)
    assert result["reason"] == "no_longer_needed"
    assert issue(hass, entry) is None


async def test_two_plants_keep_independent_notifications_and_acknowledgements(
    hass, module_file
):
    a, first = await ready_manager(hass)
    b, second = await ready_manager(hass)
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await first.async_sync()
    await second.async_sync()
    await first.async_apply_update(first.revision)
    assert issue(hass, a) is None
    assert issue(hass, b) is not None
    hass.config_entries.async_update_entry(
        b, options=dict(b.options) | {"dashboard_enabled": False}
    )
    await second.async_sync()
    assert issue(hass, b) is None
    first.async_stop()
    second.async_stop()


async def test_repair_translations_include_action_and_internal_reload_link(hass):
    for language in ("de", "en"):
        texts = await async_get_translations(hass, language, "issues", {DOMAIN})
        prefix = "component.pv_forecast.issues.dashboard_update."
        assert (
            texts[prefix + "fix_flow.step.confirm.submit"] == "Dashboard aktualisieren"
        )
        assert (
            "[Dashboard öffnen und neu laden]({dashboard_url})"
            in texts[prefix + "fix_flow.abort.updated"]
        )


@pytest.mark.parametrize("admin", [False, True])
async def test_native_http_repair_requires_admin(
    hass, hass_client, hass_read_only_access_token, module_file, admin
):
    """Der tatsächliche HA-Endpunkt erlaubt Änderungen nur für Administratoren."""
    entry, manager = await ready_manager(hass)
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await manager.async_sync()
    assert await async_setup_component(hass, "repairs", {})
    client = (
        await hass_client() if admin else await hass_client(hass_read_only_access_token)
    )
    response = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": dashboard_issue_id(entry.entry_id)},
    )
    assert response.status == (200 if admin else 401)
    if admin:
        result = await response.json()
        assert result["step_id"] == "confirm"
        response = await client.post(
            f"/api/repairs/issues/fix/{result['flow_id']}", json={}
        )
        assert response.status == 200
        assert (await response.json())["reason"] == "updated"
        assert issue(hass, entry) is None
    else:
        assert issue(hass, entry) is not None
    manager.async_stop()


async def test_unreadable_module_keeps_unconfirmed_revision(hass, module_file):
    entry, manager = await ready_manager(hass)
    original = entry.options[CONF_DASHBOARD_REVISION]
    module_file.write_text("export const beispiel = 2;", encoding="utf-8")
    await manager.async_sync()
    module_file.unlink()
    await manager.async_sync()
    assert manager.status == "error"
    assert entry.options[CONF_DASHBOARD_REVISION] == original
    assert issue(hass, entry) is not None
    manager.async_stop()
