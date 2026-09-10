"""Reale AC-Gruppen ohne Änderung unabhängiger Anlagenwerte konfigurieren."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.configuration import inverter_groups_from_options
from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.history_runtime import _configuration_id

from .helpers import configure_options, persisted_roof
from .test_config_flow import _assert_form_is_serializable


def _entry(hass, groups=None):
    options = {
        "roofs": [
            persisted_roof("a", name="Süddach"),
            persisted_roof("b", name="Garage"),
        ],
        "inverter_max_power_kw": 12.3456789,
        "history_enabled": True,
        "calibration_mode": "off",
    }
    if groups is not None:
        options["inverter_groups"] = groups
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 52, "longitude": 13, "time_zone": "Europe/Berlin"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def _group(group_id="group", roofs=None):
    return {
        "id": group_id,
        "name": "Wechselrichter",
        "max_power_kw": 7.123456789,
        "roof_ids": roofs or ["a"],
    }


async def _menu(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_options"}
    )
    assert "inverter_groups" in result["menu_options"]
    return await configure_options(
        hass, result["flow_id"], {"next_step_id": "inverter_groups"}
    )


async def test_add_group_keeps_roofs_other_options_and_exact_power(hass):
    entry = _entry(hass)
    original = deepcopy(dict(entry.options))
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs"
    ) as fetch:
        result = await _menu(hass, entry)
        assert result["menu_options"] == ["add_inverter_group", "init"]
        result = await configure_options(
            hass, result["flow_id"], {"next_step_id": "add_inverter_group"}
        )
        _assert_form_is_serializable(result)
        assert result["data_schema"]({})["name"] == "Wechselrichter 1"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "name": "Gemeinsamer Wechselrichter",
                "max_power_kw": 7.123456789,
                "roof_ids": ["a", "b"],
            },
        )
        fetch.assert_not_awaited()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {
        key: value for key, value in result["data"].items() if key != "inverter_groups"
    } == original
    group = result["data"]["inverter_groups"][0]
    assert group["max_power_kw"] == 7.123456789
    assert group["roof_ids"] == ["a", "b"]
    assert group["id"]
    assert len(inverter_groups_from_options(result["data"])) == 1
    assert (entry.version, entry.minor_version) == (1, 1)


async def test_edit_group_keeps_group_id_and_exact_defaults(hass):
    group = _group()
    entry = _entry(hass, [group])
    original = deepcopy(dict(entry.options))
    result = await _menu(hass, entry)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "edit_inverter_group"}
    )
    _assert_form_is_serializable(result)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "group"}
    )
    _assert_form_is_serializable(result)
    defaults = result["data_schema"]({})
    assert defaults["max_power_kw"] == group["max_power_kw"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], defaults | {"name": "Neuer Name"}
    )
    expected = original | {"inverter_groups": [group | {"name": "Neuer Name"}]}
    assert result["data"] == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"max_power_kw": 0},
        {"roof_ids": []},
        {"name": " "},
        {"max_power_kw": float("nan")},
    ],
)
async def test_invalid_group_stays_in_form_without_changing_options(hass, changes):
    entry = _entry(hass)
    original = deepcopy(dict(entry.options))
    result = await _menu(hass, entry)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "add_inverter_group"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "Wechselrichter", "max_power_kw": 5, "roof_ids": ["a"]} | changes,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_inverter_group"}
    assert dict(entry.options) == original


async def test_already_assigned_roof_cannot_be_selected_for_another_group(hass):
    entry = _entry(hass, [_group()])
    result = await _menu(hass, entry)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "add_inverter_group"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"name": "Zweiter", "max_power_kw": 5, "roof_ids": ["a"]}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "Wechselrichter", "max_power_kw": 5, "roof_ids": ["b"]},
    )
    assert result["errors"] == {"base": "invalid_inverter_group"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Zweiter", "max_power_kw": 5, "roof_ids": ["b"]}
    )
    assert len(result["data"]["inverter_groups"]) == 2


async def test_remove_group_requires_confirmation_and_keeps_roofs(hass):
    entry = _entry(hass, [_group()])
    original = deepcopy(dict(entry.options))
    result = await _menu(hass, entry)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "remove_inverter_group"}
    )
    _assert_form_is_serializable(result)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "group"}
    )
    _assert_form_is_serializable(result)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_remove": False}
    )
    assert result["type"] is FlowResultType.MENU
    assert dict(entry.options) == original
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "remove_inverter_group"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "group"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_remove": True}
    )
    assert result["data"] == original | {"inverter_groups": []}


@pytest.mark.parametrize("assigned", [["a"], ["a", "b"]])
async def test_removing_roof_explicitly_handles_affected_group(hass, assigned):
    group = _group(roofs=assigned)
    entry = _entry(hass, [group])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "remove_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "a"}
    )
    explanation = result["description_placeholders"]["inverter_group_changes"]
    assert "Wechselrichter" in explanation
    assert ("leere AC-Gruppe" in explanation) == (len(assigned) == 1)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_remove": True}
    )
    expected = [] if len(assigned) == 1 else [group | {"roof_ids": ["b"]}]
    assert result["data"]["inverter_groups"] == expected
    assert result["data"]["roofs"] == [persisted_roof("b", name="Garage")]
    inverter_groups_from_options(result["data"])


async def test_opening_group_options_keeps_original_configuration_fingerprint(hass):
    """Eine Bestandsanlage bleibt ohne bewusste Gruppenzuordnung exakt unverändert."""

    entry = _entry(hass)
    before = deepcopy(dict(entry.options))
    fingerprint = _configuration_id(entry)
    result = await _menu(hass, entry)
    await configure_options(hass, result["flow_id"], {"next_step_id": "init"})
    assert dict(entry.options) == before
    assert "inverter_groups" not in entry.options
    assert _configuration_id(entry) == fingerprint


async def test_roof_removal_does_not_remove_a_new_unconfirmed_group(hass):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "remove_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "a"}
    )
    assert result["description_placeholders"]["inverter_group_changes"] == ""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "inverter_groups": [_group()]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm_remove": True}
    )
    assert result["reason"] == "reconfigure_entry_changed"
    assert entry.options["inverter_groups"] == [_group()]
    assert len(entry.options["roofs"]) == 2
