"""Issue #172: bewusste Optionen und kompatibler öffentlicher Status."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType

from .test_calibration_flow import _entry


async def open_morning(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_options"}
    )
    assert "morning" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "morning"}
    )


@pytest.mark.parametrize("mode", ["off", "observe", "auto"])
async def test_modes_preserve_unrelated_options_and_require_explicit_save(hass, mode):
    entry = _entry(hass)
    original = deepcopy(dict(entry.options))
    result = await open_morning(hass, entry)
    assert dict(entry.options) == original
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "morning_settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"morning_mode": mode, "morning_threshold_kw": 0.75}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        **original,
        "morning_mode": mode,
        "morning_threshold_kw": 0.75,
    }


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"history": False}, "calibration_history_required"),
        ({"sources": []}, "calibration_energy_required"),
    ],
)
async def test_missing_prerequisites_do_not_silently_enable_archive_or_source(
    hass, kwargs, error
):
    entry = _entry(hass, **kwargs)
    result = await open_morning(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "morning_settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"morning_mode": "auto", "morning_threshold_kw": 0.5}
    )
    assert result["errors"] == {"base": error}
    assert "morning_mode" not in entry.options


async def test_native_status_is_read_only_and_has_no_fabricated_numbers(hass):
    entry = _entry(hass)
    result = await open_morning(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "morning_status"}
    )
    assert result["type"] is FlowResultType.FORM
    placeholders = result["description_placeholders"]
    assert placeholders["status"] == "Ausgeschaltet"
    assert placeholders["baseline_time"] == "—"
    assert placeholders["training"] == "0"


async def test_reset_is_explicit_and_does_not_reset_global_calibration(hass):
    entry = _entry(hass)
    manager = SimpleNamespace(async_reset=AsyncMock())
    entry.runtime_data = SimpleNamespace(morning=manager)
    result = await open_morning(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "morning_reset"}
    )
    manager.async_reset.assert_not_called()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    manager.async_reset.assert_awaited_once()
    assert result["step_id"] == "morning"
