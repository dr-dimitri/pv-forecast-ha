"""Die vereinfachte native Navigation einschließlich Abbruch und Überspringen."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.helpers.translation import async_get_translations

from .test_measurement_adapters import ksem
from .test_measurement_flow import _entry


async def test_four_native_groups_and_return_preserve_options(hass):
    """Alle Aktionen bleiben über angebotene Menüs erreichbar; Zurück schreibt nicht."""
    entry = _entry(hass)
    original = deepcopy(dict(entry.options))
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    assert result["menu_options"] == [
        "plant_options",
        "measurements",
        "dashboard",
        "advanced_options",
    ]
    translations = await async_get_translations(hass, "de", "options", {"pv_forecast"})
    assert [
        translations[f"component.pv_forecast.options.step.init.menu_options.{key}"]
        for key in result["menu_options"]
    ] == ["Anlage", "PV-Erzeugung", "Dashboard", "Erweiterte Funktionen"]
    for group, expected in [
        ("plant_options", {"add_roof", "edit_roof", "remove_roof", "system", "init"}),
        (
            "advanced_options",
            {
                "history",
                "calibration",
                "forecast_horizon",
                "inverter_groups",
                "horizon_profile",
                "init",
            },
        ),
    ]:
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": group}
        )
        assert set(result["menu_options"]) == expected
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "init"}
        )
        assert dict(entry.options) == original
    manager.async_abort(result["flow_id"])
    assert dict(entry.options) == original


@pytest.mark.parametrize("has_device", [False, True])
async def test_device_help_skip_and_abort_have_no_helper_effect(hass, has_device):
    """Geräteliste erklärt beide Wege; Überspringen/Schließen legt nichts an."""
    entry = _entry(hass)
    if has_device:
        ksem(hass)
    manager = hass.config_entries.options
    before = len(hass.config_entries.async_entries())
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "measurements"}
    )
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "add_measurement"}
    )
    assert (
        "Bereits eingerichtet" if has_device else "zuerst dessen Integration"
    ) in result["description_placeholders"]["devices"]
    options = next(iter(result["data_schema"].schema.values())).config["options"]
    assert {item["value"] for item in options} >= {"manual", "skip"}
    if has_device:
        assert any("KSEM Garage" in item["label"] for item in options)
    with patch(
        "custom_components.pv_forecast.measurement_configuration.async_resolve_measurement_helpers"
    ) as helpers:
        result = await manager.async_configure(result["flow_id"], {"device": "skip"})
        assert result["step_id"] == "measurements"
        manager.async_abort(result["flow_id"])
        helpers.assert_not_called()
    assert len(hass.config_entries.async_entries()) == before
    assert entry.options["measurement_sources"] == []
