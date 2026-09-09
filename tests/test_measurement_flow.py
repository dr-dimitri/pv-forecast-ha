"""Optionale Zuordnung echter PV-Messquellen über die native HA-Oberfläche."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous_serialize
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.const import (
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ROOFS,
    CONF_TIME_ZONE,
    DOMAIN,
)
from custom_components.pv_forecast.measurement_configuration import (
    CONF_MEASUREMENT_SOURCES,
    _validated_source,
)

from .helpers import persisted_roof
from .test_config_flow import ROOF_FORM

ENERGY_ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "Wh",
    "friendly_name": "PV-Produktion Garage",
}
DETAILS = {
    "entity_id": "sensor.pv_garage",
    "kind": "total",
    "scope": "Wechselrichter Garage, AC ohne Batterie",
    "derived_energy": False,
    "max_interval_minutes": 60,
}
CONFIRM = {"confirmed_pv": True, "confirmed_disjoint": True}


def _entry(hass, sources=()):
    """Bestehende Anlage mit unveränderlichen, unabhängigen Optionen anlegen."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        version=1,
        minor_version=1,
        data={
            CONF_LATITUDE: 52.52,
            CONF_LONGITUDE: 13.41,
            CONF_TIME_ZONE: "Europe/Berlin",
        },
        options={
            CONF_ROOFS: [persisted_roof()],
            CONF_INVERTER_MAX_POWER_KW: 8,
            CONF_MEASUREMENT_SOURCES: list(sources),
        },
    )
    entry.add_to_hass(hass)
    return entry


def _source(hass, *, entity_id="sensor.pv_garage", source_id="source-a"):
    """Eine über Metadaten geprüfte Quelle als Ausgangsbestand bereitstellen."""

    hass.states.async_set(entity_id, "12000", ENERGY_ATTRS)
    return _validated_source(hass, DETAILS | {"entity_id": entity_id}, source_id, [])


async def _options(hass, entry):
    """Zum gemeinsamen Messquellenmenü des Options Flows gehen."""

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "measurements" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "measurements"}
    )


async def _choose(hass, result, step_id):
    """Eine vorhandene Menüaktion auslösen."""

    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


@pytest.mark.asyncio
async def test_setup_measurement_preview_confirmation_and_independent_edit(hass):
    """Ein Anfängerpfad bewahrt Quellen auch nach erneuter Systembearbeitung."""

    hass.states.async_set("sensor.pv_garage", "12000", ENERGY_ATTRS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location_source": "home_assistant"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM
    )
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "measurements"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "add_measurement"}
        )
        assert voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], DETAILS
        )
        assert result["step_id"] == "confirm_measurement"
        assert result["description_placeholders"]["entity"] == "PV-Produktion Garage"
        assert result["description_placeholders"]["unit"] == "Wh"
        assert result["description_placeholders"]["reading"].startswith("12 kWh")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirmed_pv": True, "confirmed_disjoint": False}
        )
        assert result["errors"] == {"base": "measurement_confirmation_required"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONFIRM
        )
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "measurements_done"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "edit_system"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_INVERTER_MAX_POWER_KW: 5}
        )
    with patch("custom_components.pv_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    source = result["options"][CONF_MEASUREMENT_SOURCES][0]
    assert source["entity_id"] == "sensor.pv_garage"
    assert source["scope"] == DETAILS["scope"]
    assert source["confirmed_pv"] and source["confirmed_disjoint"]
    assert result["options"][CONF_INVERTER_MAX_POWER_KW] == 5
    assert result["version"] == 1 and result["minor_version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,attrs,expected",
    [
        ("daily", ENERGY_ATTRS, "Täglich zurückgesetzter Energiezähler"),
        (
            "power",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
            },
            "Momentane Leistung (keine Energieintegration)",
        ),
    ],
)
async def test_options_adds_daily_or_power_source_without_changing_roofs(
    hass, kind, attrs, expected
):
    """Messart bleibt explizit; jede Auswahl durchläuft die konkrete Vorschau."""

    hass.states.async_set("sensor.pv_garage", "1000", attrs)
    entry = _entry(hass)
    original = dict(entry.options)
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], DETAILS | {"kind": kind}
    )
    assert result["description_placeholders"]["kind"] == expected
    assert result["description_placeholders"]["reading"].startswith("1 kW")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], CONFIRM
    )
    result = await _choose(hass, result, "measurements_done")
    assert result["data"][CONF_ROOFS] == original[CONF_ROOFS]
    assert result["data"][CONF_INVERTER_MAX_POWER_KW] == 8
    assert result["data"][CONF_MEASUREMENT_SOURCES][0]["kind"] == kind


@pytest.mark.asyncio
async def test_options_rejects_duplicate_sources_and_preserves_stable_id(hass):
    """Dieselbe Quelle wird nie doppelt summiert; Bearbeitung erhält source_id."""

    source = _source(hass)
    entry = _entry(hass, [source])
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], DETAILS
    )
    assert result["errors"] == {"base": "duplicate_measurement_source"}
    result = await _options(hass, entry)
    result = await _choose(hass, result, "edit_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source_id": source["source_id"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], DETAILS | {"scope": "Nur Wechselrichter am Wohnhaus"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], CONFIRM
    )
    result = await _choose(hass, result, "measurements_done")
    saved = result["data"][CONF_MEASUREMENT_SOURCES][0]
    assert saved["source_id"] == source["source_id"]
    assert saved["scope"] == "Nur Wechselrichter am Wohnhaus"


@pytest.mark.asyncio
async def test_registry_rename_and_integral_helper_are_visible(hass):
    """Registry-Identität bewahrt Zuordnung und erkennt bestehende Integral-Helfer."""

    registry = er.async_get(hass)
    registered = registry.async_get_or_create(
        "sensor", "integration", "pv-energy", suggested_object_id="pv_garage"
    )
    source = _source(hass, entity_id=registered.entity_id)
    assert source["derived_energy"] is True
    renamed = registry.async_update_entity(
        registered.entity_id, new_entity_id="sensor.pv_neu"
    )
    hass.states.async_set(renamed.entity_id, "14000", ENERGY_ATTRS)
    entry = _entry(hass, [source])
    result = await _choose(hass, await _options(hass, entry), "edit_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source_id": source["source_id"]}
    )
    assert (
        result["data_schema"](
            {
                "entity_id": "sensor.pv_neu",
                **{k: v for k, v in DETAILS.items() if k != "entity_id"},
            }
        )["entity_id"]
        == "sensor.pv_neu"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], DETAILS | {"entity_id": "sensor.pv_neu"}
    )
    assert result["description_placeholders"]["derived"].startswith("Ja")
    with pytest.raises(ValueError, match="duplicate_measurement_source"):
        _validated_source(
            hass, DETAILS | {"entity_id": renamed.entity_id}, "second", [source]
        )


@pytest.mark.parametrize(
    "state_attrs",
    [
        ENERGY_ATTRS | {"unit_of_measurement": "MWh"},
        ENERGY_ATTRS | {"device_class": "power"},
        ENERGY_ATTRS | {"state_class": "measurement"},
        ENERGY_ATTRS | {"state_class": None},
    ],
)
@pytest.mark.asyncio
async def test_wrong_metadata_is_rejected(hass, state_attrs):
    """Name und Zahl allein genügen nicht zur Zuordnung eines Ertragszählers."""

    hass.states.async_set("sensor.pv_garage", "42", state_attrs)
    with pytest.raises(ValueError, match="invalid_measurement_metadata"):
        _validated_source(hass, DETAILS, "source-a", [])


@pytest.mark.asyncio
async def test_own_forecasts_missing_entity_and_empty_scope_are_rejected(hass):
    """Eigene Prognosen und unklare Messgrenzen sind keine echten Messquellen."""

    with pytest.raises(ValueError, match="invalid_measurement_source"):
        _validated_source(hass, DETAILS, "source-a", [])
    hass.states.async_set("sensor.pv_garage", "42", ENERGY_ATTRS)
    with pytest.raises(ValueError, match="invalid_measurement_source"):
        _validated_source(hass, DETAILS | {"scope": "   "}, "source-a", [])
    registered = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "fake_forecast", suggested_object_id="own_forecast"
    )
    hass.states.async_set(registered.entity_id, "42", ENERGY_ATTRS)
    with pytest.raises(ValueError, match="invalid_measurement_source"):
        _validated_source(
            hass, DETAILS | {"entity_id": registered.entity_id}, "source-a", []
        )


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.asyncio
async def test_targeted_deletion_requires_confirmation(hass, remove):
    """Die UI löscht genau eine Messkopie; Entfernen betrifft auch die Zuordnung."""

    source = _source(hass)
    other = _source(hass, entity_id="sensor.pv_house", source_id="source-b")
    entry = _entry(hass, [source, other])
    result = await _choose(
        hass,
        await _options(hass, entry),
        "remove_measurement" if remove else "delete_measurement_data",
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source_id": source["source_id"]}
    )
    with patch(
        "custom_components.pv_forecast.measurement_runtime.async_delete_measurement_source_data",
        new_callable=AsyncMock,
    ) as delete:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm_delete": False}
        )
        delete.assert_not_called()
        result = await _choose(
            hass, result, "remove_measurement" if remove else "delete_measurement_data"
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"source_id": source["source_id"]}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm_delete": True}
        )
        delete.assert_awaited_once_with(hass, entry, source["source_id"])
    result = await _choose(hass, result, "measurements_done")
    assert result["data"][CONF_MEASUREMENT_SOURCES] == (
        [other] if remove else [source, other]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("current_state", ["unavailable", "99999999"])
async def test_last_valid_preview_is_not_reused_for_replacement(hass, current_state):
    """Ein fehlender Wert darf nur für dieselbe Quelle als alter Lesewert erscheinen."""

    source = _source(hass)
    entry = _entry(hass, [source])
    manager = Mock()
    manager.preview.return_value = {
        "last_valid_value": 11,
        "timestamp": "2026-09-09T08:00:00+00:00",
    }
    entry.runtime_data = SimpleNamespace(measurements=manager)
    hass.states.async_set("sensor.pv_garage", current_state, ENERGY_ATTRS)
    result = await _choose(hass, await _options(hass, entry), "edit_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"source_id": source["source_id"]}
    )
    flow = hass.config_entries.options._progress[result["flow_id"]]
    preview = await flow.async_step_measurement_details(DETAILS)
    assert (
        preview["description_placeholders"]["reading"]
        == "11 kWh (2026-09-09T10:00:00+02:00)"
    )
    manager.preview.reset_mock()
    hass.states.async_set("sensor.pv_replacement", "unknown", ENERGY_ATTRS)
    preview = await flow.async_step_measurement_details(
        DETAILS | {"entity_id": "sensor.pv_replacement"}
    )
    assert (
        preview["description_placeholders"]["reading"]
        == "Kein gültiger Messwert vorhanden"
    )
    manager.preview.assert_not_called()
    await flow.async_step_confirm_measurement(CONFIRM)
    # Eine weitere Bearbeitung desselben Entwurfs verwendet keine Daten des
    # weiterhin laufenden, erst nach Flow-Abschluss ersetzten Vorgängers.
    preview = await flow.async_step_measurement_details(
        DETAILS | {"entity_id": "sensor.pv_replacement"}
    )
    assert (
        preview["description_placeholders"]["reading"]
        == "Kein gültiger Messwert vorhanden"
    )
    manager.preview.assert_not_called()


@pytest.mark.asyncio
async def test_roof_and_inverter_edit_keep_measurement_options(hass):
    """Die bereits vorhandenen Options-Aktionen bewahren die neuen Quellen."""

    source = _source(hass)
    entry = _entry(hass, [source])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _choose(hass, result, "system")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["data"][CONF_MEASUREMENT_SOURCES] == [source]
    assert CONF_INVERTER_MAX_POWER_KW not in result["data"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _choose(hass, result, "add_roof")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            k: v
            for k, v in (ROOF_FORM | {"name": "Garagendach"}).items()
            if k != "add_another"
        },
    )
    assert result["data"][CONF_MEASUREMENT_SOURCES] == [source]
