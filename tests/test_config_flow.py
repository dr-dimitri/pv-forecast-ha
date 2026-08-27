"""Vollständige Tests des mehrstufigen Config- und Options-Flows."""

from unittest.mock import patch

import pytest
import voluptuous_serialize
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import (
    OpenMeteoConnectionError,
    OpenMeteoDataError,
)
from custom_components.pv_forecast.config_flow import PvForecastConfigFlow
from custom_components.pv_forecast.const import (
    CONF_ADD_ANOTHER,
    CONF_AZIMUTH,
    CONF_CONFIRM_REMOVE,
    CONF_COUNTRY,
    CONF_INSTALLED_POWER_KWP,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LOCATION_NAME,
    CONF_LOCATION_SOURCE,
    CONF_LONGITUDE,
    CONF_LOSS_FACTOR,
    CONF_NAME,
    CONF_POSTAL_CODE,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_STREET,
    CONF_SYSTEM_EFFICIENCY,
    CONF_TILT,
    CONF_TIME_ZONE,
    DOMAIN,
    LOCATION_SOURCE_ADDRESS,
    LOCATION_SOURCE_HOME_ASSISTANT,
)
from custom_components.pv_forecast.geocoding import (
    AddressNotFoundError,
    GeocodingConnectionError,
    GeocodingDataError,
)
from custom_components.pv_forecast.models import GeocodedLocation

from .helpers import persisted_roof

LOCATION = {CONF_LATITUDE: 52.52, CONF_LONGITUDE: 13.41}
ROOF_FORM = {
    CONF_NAME: "Süddach",
    CONF_INSTALLED_POWER_KWP: 8.2,
    CONF_AZIMUTH: "south",
    CONF_TILT: 35,
    CONF_SYSTEM_EFFICIENCY: 90,
    CONF_ADD_ANOTHER: False,
}
ROOF_FORM_SINGLE = {
    key: value for key, value in ROOF_FORM.items() if key != CONF_ADD_ANOTHER
}
ADDRESS_FORM = {
    CONF_POSTAL_CODE: "10117",
    CONF_STREET: "Pariser Platz 1",
    CONF_COUNTRY: "DE",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["de", "en"])
async def test_german_config_flow_translations_are_loaded(hass, language: str) -> None:
    """Die deutschen UI-Texte gelten auch für eine englische Profilsprache."""

    config_translations = await async_get_translations(
        hass, language, "config", integrations={DOMAIN}
    )
    selector_translations = await async_get_translations(
        hass, language, "selector", integrations={DOMAIN}
    )
    assert (
        config_translations[
            "component.pv_forecast.config.step.user.data.location_source"
        ]
        == "Standortquelle"
    )
    assert (
        config_translations[
            "component.pv_forecast.config.step.address.data.postal_code"
        ]
        == "Postleitzahl"
    )
    assert (
        config_translations["component.pv_forecast.config.step.address.data.street"]
        == "Straße und Hausnummer"
    )
    assert (
        config_translations[
            "component.pv_forecast.config.step.roof.data.system_efficiency"
        ]
        == "Wirkungsgrad"
    )
    assert (
        config_translations[
            "component.pv_forecast.config.step.summary.menu_options.edit_roofs"
        ]
        == "Dachflächen ändern"
    )
    assert not any(".wirkungsgrad" in key for key in config_translations)
    assert (
        "**Dachflächen** (installierte Leistung · Ausrichtung · Neigung · "
        "Wirkungsgrad)"
        in config_translations["component.pv_forecast.config.step.summary.description"]
    )
    assert (
        selector_translations[
            "component.pv_forecast.selector.location_source.options.home_assistant"
        ]
        == "Standort aus Home Assistant übernehmen"
    )
    assert (
        selector_translations[
            "component.pv_forecast.selector.location_source.options.address"
        ]
        == "Andere Anschrift eingeben"
    )
    assert (
        selector_translations[
            "component.pv_forecast.selector.roof_direction.options.south"
        ]
        == "Süd"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "component.pv_forecast.config.step.summary.description",
        "component.pv_forecast.options.step.init.description",
        "component.pv_forecast.options.step.add_roof.description",
        "component.pv_forecast.options.step.edit_roof.description",
        "component.pv_forecast.options.step.remove_roof.description",
        "component.pv_forecast.options.step.system.description",
    ],
)
async def test_config_and_options_steps_embed_a_symbol_image(hass, key: str) -> None:
    """Hauptmenü, Aktionen und Abschlussdialog zeigen ein eingebettetes Symbolbild."""

    config_translations = await async_get_translations(
        hass, "de", "config", integrations={DOMAIN}
    )
    options_translations = await async_get_translations(
        hass, "de", "options", integrations={DOMAIN}
    )
    description = (config_translations | options_translations)[key]
    assert description.startswith("![Symbolbild:")
    assert "data:image/svg+xml;base64," in description


async def _advance_to_roof(hass):
    """Config Flow mit dem Home-Assistant-Standort bis zum Dach führen."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    _assert_form_is_serializable(result)
    assert result["step_id"] == "user"
    assert result["data_schema"]({})[CONF_LOCATION_SOURCE] == (
        LOCATION_SOURCE_HOME_ASSISTANT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT},
    )
    assert result["step_id"] == "roof"
    assert result["data_schema"]({})[CONF_SYSTEM_EFFICIENCY] == 90
    return result


async def _advance_to_system(hass):
    """Config Flow bis zum Verbindungstest führen."""

    result = await _advance_to_roof(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM
    )
    assert result["step_id"] == "system"
    return result


async def _advance_to_summary(hass):
    """Config Flow mit erfolgreichem Testabruf bis zum Abschluss führen."""

    result = await _advance_to_system(hass)
    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "summary"
    return result


@pytest.mark.asyncio
async def test_summary_is_german_for_english_profile(hass) -> None:
    """Dynamische Abschlusswerte bleiben bei englischer Profilsprache deutsch."""

    hass.config.language = "en"
    result = await _advance_to_summary(hass)

    assert "8.2 kWp · Süd · 35° · 90%" in result["description_placeholders"]["roofs"]
    assert result["description_placeholders"]["inverter"] == "nicht begrenzt"
    assert result["menu_options"] == {
        "finish": "Einrichtung abschließen",
        "edit_location": "Standort ändern",
        "edit_roofs": "Dachflächen ändern",
        "edit_system": "Wechselrichterleistung ändern",
    }


def _assert_form_is_serializable(result) -> None:
    """Formular genauso wie der Home-Assistant-HTTP-Endpunkt serialisieren."""

    assert result["type"] is FlowResultType.FORM
    assert (
        voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        is not None
    )


@pytest.mark.asyncio
async def test_all_config_and_options_forms_are_serializable(hass) -> None:
    """Kein Formular darf nicht serialisierbare Python-Validatoren enthalten."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    _assert_form_is_serializable(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT},
    )
    _assert_form_is_serializable(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM
    )
    _assert_form_is_serializable(result)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={
            CONF_ROOFS: [
                persisted_roof("first"),
                persisted_roof("second", name="Ostdach"),
            ]
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    for action in ("add_roof", "system"):
        step = await hass.config_entries.options.async_init(entry.entry_id)
        step = await hass.config_entries.options.async_configure(
            step["flow_id"], {"next_step_id": action}
        )
        _assert_form_is_serializable(step)

    for action in ("edit_roof", "remove_roof"):
        step = await hass.config_entries.options.async_init(entry.entry_id)
        step = await hass.config_entries.options.async_configure(
            step["flow_id"], {"next_step_id": action}
        )
        _assert_form_is_serializable(step)
        step = await hass.config_entries.options.async_configure(
            step["flow_id"], {CONF_ROOF_ID: "first"}
        )
        _assert_form_is_serializable(step)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_step"),
    [
        ("edit_location", "user"),
        ("edit_roofs", "roof"),
        ("edit_system", "system"),
    ],
)
async def test_summary_navigation_returns_to_every_section(
    hass, action: str, expected_step: str
) -> None:
    """Der Abschlussdialog bietet Rückwege zu jedem änderbaren Abschnitt."""

    result = await _advance_to_summary(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": action}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == expected_step
    _assert_form_is_serializable(result)
    if action == "edit_roofs":
        defaults = result["data_schema"]({})
        assert defaults[CONF_NAME] == ROOF_FORM[CONF_NAME]
        assert defaults[CONF_INSTALLED_POWER_KWP] == ROOF_FORM[CONF_INSTALLED_POWER_KWP]
        assert defaults[CONF_AZIMUTH] == ROOF_FORM[CONF_AZIMUTH]
        assert defaults[CONF_TILT] == ROOF_FORM[CONF_TILT]
        assert defaults[CONF_SYSTEM_EFFICIENCY] == ROOF_FORM[CONF_SYSTEM_EFFICIENCY]
        assert defaults[CONF_ADD_ANOTHER] is False


@pytest.mark.asyncio
async def test_successful_setup_with_multiple_roofs(hass) -> None:
    """Standort, mehrere Dächer und Wechselrichter werden gespeichert."""

    hass.config.language = "de"
    result = await _advance_to_roof(hass)
    first_roof = ROOF_FORM | {CONF_ADD_ANOTHER: True}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], first_roof
    )
    assert result["step_id"] == "roof"
    assert result["data_schema"]({})[CONF_NAME] == "Dachfläche 2"
    second_roof = ROOF_FORM | {CONF_NAME: "Ostdach", CONF_AZIMUTH: "east"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], second_roof
    )

    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ) as fetch:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_INVERTER_MAX_POWER_KW: 10}
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "summary"
    roof_summary = result["description_placeholders"]["roofs"]
    assert "Süddach" in roof_summary
    assert "8.2 kWp · Süd · 35° · 90%" in roof_summary
    assert "Ostdach" in roof_summary
    assert "8.2 kWp · Ost · 35° · 90%" in roof_summary
    assert result["description_placeholders"]["inverter"] == "10 kW"
    assert result["description_placeholders"]["location_source"] == "Home Assistant"
    assert result["description_placeholders"]["latitude"] == (
        f"{hass.config.latitude:.6f}"
    )
    assert result["description_placeholders"]["longitude"] == (
        f"{hass.config.longitude:.6f}"
    )
    assert result["menu_options"] == {
        "finish": "Einrichtung abschließen",
        "edit_location": "Standort ändern",
        "edit_roofs": "Dachflächen ändern",
        "edit_system": "Wechselrichterleistung ändern",
    }
    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ) as refresh:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TIME_ZONE] == hass.config.time_zone
    assert result["data"][CONF_LOCATION_SOURCE] == LOCATION_SOURCE_HOME_ASSISTANT
    assert result["options"][CONF_INVERTER_MAX_POWER_KW] == 10
    assert len(result["options"][CONF_ROOFS]) == 2
    assert result["options"][CONF_ROOFS][0][CONF_LOSS_FACTOR] == 10
    assert result["options"][CONF_ROOFS][0][CONF_ROOF_ID]
    assert result["options"][CONF_ROOFS][1][CONF_AZIMUTH] == 90
    assert fetch.await_count == 1
    assert refresh.await_count == 1
    await hass.async_block_till_done()
    entry = result["result"]
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 6
    assert {entity.domain for entity in entities} == {"sensor"}
    assert {entity.original_name for entity in entities} == {
        "Prognose heute",
        "Prognose morgen",
        "Süddach Prognose heute",
        "Süddach Prognose morgen",
        "Ostdach Prognose heute",
        "Ostdach Prognose morgen",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_editing_roofs_preserves_previously_entered_roofs(hass) -> None:
    """Der Rücksprung zu den Dachflächen verwirft keine bereits erfassten Werte."""

    result = await _advance_to_roof(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM | {CONF_ADD_ANOTHER: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        ROOF_FORM
        | {CONF_NAME: "Ostdach", CONF_AZIMUTH: "east", CONF_ADD_ANOTHER: True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM | {CONF_NAME: "Westdach", CONF_AZIMUTH: "west"}
    )
    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "summary"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "edit_roofs"}
    )
    assert result["step_id"] == "roof"
    defaults = result["data_schema"]({})
    assert defaults[CONF_NAME] == "Süddach"
    assert defaults[CONF_ADD_ANOTHER] is True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], defaults)

    defaults = result["data_schema"]({})
    assert defaults[CONF_NAME] == "Ostdach"
    assert defaults[CONF_AZIMUTH] == "east"
    assert defaults[CONF_ADD_ANOTHER] is True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], defaults)

    defaults = result["data_schema"]({})
    assert defaults[CONF_NAME] == "Westdach"
    assert defaults[CONF_AZIMUTH] == "west"
    assert defaults[CONF_ADD_ANOTHER] is False
    corrected = defaults | {CONF_TILT: 20}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], corrected
    )
    assert result["step_id"] == "system"

    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "summary"
    roof_summary = result["description_placeholders"]["roofs"]
    assert "Süddach:** 8.2 kWp · Süd · 35°" in roof_summary
    assert "Ostdach:** 8.2 kWp · Ost · 35°" in roof_summary
    assert "Westdach:** 8.2 kWp · West · 20°" in roof_summary

    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    roofs = result["options"][CONF_ROOFS]
    assert {roof[CONF_NAME] for roof in roofs} == {"Süddach", "Ostdach", "Westdach"}
    west = next(roof for roof in roofs if roof[CONF_NAME] == "Westdach")
    assert west[CONF_TILT] == 20


@pytest.mark.asyncio
async def test_address_is_geocoded_and_persisted(hass) -> None:
    """Eine Anschrift wird einmalig aufgelöst und nicht als Koordinate abgefragt."""

    hass.config.language = "de"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS}
    )
    _assert_form_is_serializable(result)
    with patch(
        "custom_components.pv_forecast.config_flow.NominatimClient.async_geocode",
        return_value=GeocodedLocation(52.5163, 13.3777, "10117 Berlin, Deutschland"),
    ) as geocode:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ADDRESS_FORM
        )

    assert result["step_id"] == "roof"
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._location[CONF_LOCATION_SOURCE] == LOCATION_SOURCE_ADDRESS
    assert flow._location[CONF_LOCATION_NAME] == "10117 Berlin, Deutschland"
    assert flow._location[CONF_STREET] == "Pariser Platz 1"
    geocode.assert_awaited_once_with("Pariser Platz 1", "10117", "DE", "de")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM
    )
    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"]["location_source"] == (
        "Adresseingabe (OpenStreetMap/Nominatim)"
    )
    assert result["description_placeholders"]["latitude"] == "52.516300"
    assert result["description_placeholders"]["longitude"] == "13.377700"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "edit_location"}
    )
    assert result["data_schema"]({})[CONF_LOCATION_SOURCE] == LOCATION_SOURCE_ADDRESS
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS}
    )
    address_defaults = result["data_schema"]({})
    assert address_defaults[CONF_POSTAL_CODE] == "10117"
    assert address_defaults[CONF_STREET] == "Pariser Platz 1"
    assert address_defaults[CONF_COUNTRY] == "DE"


@pytest.mark.asyncio
async def test_invalid_home_assistant_location_stays_in_visible_form(hass) -> None:
    """Ungültige HA-Koordinaten zeigen einen Textfehler statt einer leeren Seite."""

    hass.config.latitude = float("nan")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_home_location"}
    assert (
        voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )[0]["name"]
        == CONF_LOCATION_SOURCE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AddressNotFoundError("nicht gefunden"), "address_not_found"),
        (GeocodingConnectionError("offline"), "geocoding_unavailable"),
        (GeocodingDataError("kaputt"), "invalid_geocoding_response"),
    ],
)
async def test_address_errors_are_localized(hass, error, expected: str) -> None:
    """Adressfehler bleiben im Formular und erhalten verständliche Meldungen."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS}
    )
    with patch(
        "custom_components.pv_forecast.config_flow.NominatimClient.async_geocode",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ADDRESS_FORM
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_invalid_roof_parameters(hass) -> None:
    """Nicht positive Dachleistung wird im Dachformular abgewiesen."""

    result = await _advance_to_roof(hass)
    with pytest.raises(InvalidData) as error:
        await hass.config_entries.flow.async_configure(
            result["flow_id"], ROOF_FORM | {CONF_INSTALLED_POWER_KWP: -1}
        )
    assert CONF_INSTALLED_POWER_KWP in error.value.schema_errors


@pytest.mark.asyncio
async def test_duplicate_roof_names_are_rejected(hass) -> None:
    """Dachnamen müssen unabhängig von Großschreibung eindeutig sein."""

    result = await _advance_to_roof(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOF_FORM | {CONF_ADD_ANOTHER: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        ROOF_FORM | {CONF_NAME: " südDACH "},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "duplicate_roof_name"}


@pytest.mark.asyncio
async def test_non_finite_value_is_rejected(hass) -> None:
    """Nicht endlicher Wirkungsgrad wird hinter dem UI-Schema abgelehnt."""

    result = await _advance_to_roof(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        ROOF_FORM | {CONF_SYSTEM_EFFICIENCY: float("nan")},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_roof"}


@pytest.mark.asyncio
async def test_flow_internal_validation_errors_are_localized(hass) -> None:
    """Auch direkte, unvollständige Eingaben liefern definierte Flow-Fehler."""

    flow = PvForecastConfigFlow()
    flow.hass = hass
    result = await flow.async_step_roof({})
    assert result["errors"] == {"base": "invalid_roof"}
    result = await flow.async_step_roof(ROOF_FORM | {CONF_AZIMUTH: "ungültig"})
    assert result["errors"] == {"base": "invalid_roof"}
    result = await flow.async_step_system({CONF_INVERTER_MAX_POWER_KW: -1})
    assert result["errors"] == {"base": "invalid_inverter"}
    result = await flow.async_step_system({CONF_INVERTER_MAX_POWER_KW: "ungültig"})
    assert result["errors"] == {"base": "invalid_inverter"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OpenMeteoConnectionError("offline"), "cannot_connect"),
        (OpenMeteoDataError("kaputt"), "invalid_response"),
    ],
)
async def test_connection_test_errors_and_retry(hass, error, expected: str) -> None:
    """Erwartete API-Fehler sind verständlich und der Flow bleibt wiederholbar."""

    result = await _advance_to_system(hass)
    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    with patch(
        "custom_components.pv_forecast.config_flow.OpenMeteoClient.async_fetch_roofs",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "summary"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_duplicate_setup_is_aborted(hass) -> None:
    """Die Integration repräsentiert genau eine PV-Prognose-Konfiguration."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={CONF_ROOFS: [persisted_roof()]},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_options_flow_menu_offers_removal_of_last_roof(hass) -> None:
    """Auch die letzte verbliebene Dachfläche lässt sich entfernen."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={CONF_ROOFS: [persisted_roof("only")]},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "add_roof",
        "edit_roof",
        "remove_roof",
        "system",
    }
    assert "Süddach" in result["description_placeholders"]["roofs"]


@pytest.mark.asyncio
async def test_options_flow_menu_hides_edit_and_remove_without_roofs(hass) -> None:
    """Ohne verbliebene Dachfläche bietet das Menü nur noch Hinzufügen an."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={CONF_ROOFS: []},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"add_roof", "system"}
    assert result["description_placeholders"]["roofs"] == ""


@pytest.mark.asyncio
async def test_options_flow_add_roof_does_not_touch_existing_roofs(hass) -> None:
    """Eine neue Dachfläche lässt sich hinzufügen, ohne bestehende zu ändern."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={
            CONF_ROOFS: [persisted_roof("first")],
            CONF_INVERTER_MAX_POWER_KW: 9,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert set(result["menu_options"]) == {
        "add_roof",
        "edit_roof",
        "remove_roof",
        "system",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_roof"}
    )
    assert result["step_id"] == "add_roof"
    assert result["data_schema"]({})[CONF_NAME] == "Dachfläche 2"

    # Direkter Aufruf prüft zusätzlich die defensive Validierung hinter dem Schema.
    options_flow = hass.config_entries.options._progress[result["flow_id"]]
    invalid = await options_flow.async_step_add_roof({})
    assert invalid["errors"] == {"base": "invalid_roof"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], ROOF_FORM_SINGLE | {CONF_NAME: "Ostdach"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    roofs = result["data"][CONF_ROOFS]
    assert len(roofs) == 2
    assert roofs[0] == persisted_roof("first")
    assert roofs[1][CONF_NAME] == "Ostdach"
    assert roofs[1][CONF_ROOF_ID] != "first"
    assert result["data"][CONF_INVERTER_MAX_POWER_KW] == 9


@pytest.mark.asyncio
async def test_options_flow_edit_roof_preserves_other_roofs_and_stable_id(
    hass,
) -> None:
    """Das Bearbeiten einer Dachfläche behält ihre ID; andere bleiben unberührt."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={
            CONF_ROOFS: [
                persisted_roof("stable_id"),
                persisted_roof("other_id", name="Ostdach"),
            ],
            CONF_INVERTER_MAX_POWER_KW: 8,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_roof"}
    )
    assert result["step_id"] == "edit_roof"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ROOF_ID: "stable_id"}
    )
    assert result["step_id"] == "edit_roof_details"
    assert result["description_placeholders"]["roof_name"] == "Süddach"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        ROOF_FORM_SINGLE | {CONF_NAME: "Garage", CONF_SYSTEM_EFFICIENCY: 91},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    roofs = result["data"][CONF_ROOFS]
    assert len(roofs) == 2
    assert roofs[0][CONF_ROOF_ID] == "stable_id"
    assert roofs[0][CONF_NAME] == "Garage"
    assert roofs[0][CONF_LOSS_FACTOR] == 9
    assert roofs[1] == persisted_roof("other_id", name="Ostdach")
    assert result["data"][CONF_INVERTER_MAX_POWER_KW] == 8


@pytest.mark.asyncio
async def test_options_flow_rejects_duplicate_names_on_add_and_edit(hass) -> None:
    """Ein neuer oder umbenannter Dachname darf keinen bestehenden doppeln."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={
            CONF_ROOFS: [
                persisted_roof("first", name="Süddach"),
                persisted_roof("second", name="Ostdach"),
            ]
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], ROOF_FORM_SINGLE | {CONF_NAME: "süddach"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "duplicate_roof_name"}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ROOF_ID: "second"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], ROOF_FORM_SINGLE | {CONF_NAME: "Süddach"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "duplicate_roof_name"}

    # Umbenennen auf den eigenen, unveränderten Namen bleibt weiterhin erlaubt.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], ROOF_FORM_SINGLE | {CONF_NAME: "Ostdach"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_options_flow_removing_roof_requires_confirmation(hass) -> None:
    """Das Entfernen einer Dachfläche wirkt erst nach ausdrücklicher Bestätigung."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={
            CONF_ROOFS: [
                persisted_roof("first"),
                persisted_roof("second", name="Ostdach"),
            ]
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert set(result["menu_options"]) == {
        "add_roof",
        "edit_roof",
        "remove_roof",
        "system",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ROOF_ID: "second"}
    )
    assert result["step_id"] == "confirm_remove_roof"
    assert result["description_placeholders"]["roof_name"] == "Ostdach"

    # Ohne Bestätigung bleibt die Dachfläche erhalten.
    declined = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_REMOVE: False}
    )
    assert declined["type"] is FlowResultType.MENU
    assert declined["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        declined["flow_id"], {"next_step_id": "remove_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ROOF_ID: "second"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_REMOVE: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [roof[CONF_ROOF_ID] for roof in result["data"][CONF_ROOFS]] == ["first"]


@pytest.mark.asyncio
async def test_options_flow_removing_last_roof_empties_the_list(hass) -> None:
    """Auch die letzte Dachfläche lässt sich nach Bestätigung entfernen."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={CONF_ROOFS: [persisted_roof("only")]},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_roof"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ROOF_ID: "only"}
    )
    assert result["step_id"] == "confirm_remove_roof"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_REMOVE: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ROOFS] == []


@pytest.mark.asyncio
async def test_options_flow_updates_inverter_limit_independently(hass) -> None:
    """Das Wechselrichterlimit lässt sich ändern, ohne Dachflächen zu berühren."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PV-Ertragsprognose",
        unique_id=DOMAIN,
        data=LOCATION | {CONF_TIME_ZONE: "Europe/Berlin"},
        options={CONF_ROOFS: [persisted_roof("first")]},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "system"}
    )
    assert result["step_id"] == "system"

    # Direkter Aufruf prüft die Validierung hinter dem numerischen Selector.
    options_flow = hass.config_entries.options._progress[result["flow_id"]]
    invalid = await options_flow.async_step_system({CONF_INVERTER_MAX_POWER_KW: -1})
    assert invalid["errors"] == {"base": "invalid_inverter"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INVERTER_MAX_POWER_KW: 9}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ROOFS] == [persisted_roof("first")]
    assert result["data"][CONF_INVERTER_MAX_POWER_KW] == 9
