"""UI-basierte Einrichtung und Bearbeitung der PV-Ertragsprognose."""

from __future__ import annotations

import logging
import math
from typing import Any, Literal, override
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    CountrySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import OpenMeteoClient, OpenMeteoConnectionError, OpenMeteoDataError
from .calculations import InvalidConfigurationError, validate_coordinates
from .configuration import roof_from_dict
from .const import (
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
    DEFAULT_SYSTEM_EFFICIENCY_PERCENT,
    DIRECTION_TO_COMPASS_AZIMUTH,
    DOMAIN,
    LOCATION_SOURCE_ADDRESS,
    LOCATION_SOURCE_HOME_ASSISTANT,
)
from .geocoding import (
    AddressNotFoundError,
    GeocodingConnectionError,
    GeocodingDataError,
    NominatimClient,
)

_LOGGER = logging.getLogger(__name__)

_DIRECTION_LABELS: dict[str, str] = {
    "north": "Nord",
    "north_east": "Nordost",
    "east": "Ost",
    "south_east": "Südost",
    "south": "Süd",
    "south_west": "Südwest",
    "west": "West",
    "north_west": "Nordwest",
}

_SUMMARY_MENU_LABELS: dict[str, str] = {
    "finish": "Einrichtung abschließen",
    "edit_location": "Standort ändern",
    "edit_roofs": "Dachflächen ändern",
    "edit_system": "Wechselrichterleistung ändern",
}


class DuplicateRoofNameError(InvalidConfigurationError):
    """Eine Dachbezeichnung wird innerhalb der Anlage mehrfach verwendet."""


def _number_selector(
    *,
    minimum: float,
    maximum: float | None = None,
    step: float | Literal["any"] = 0.1,
    unit: str | None = None,
    mode: NumberSelectorMode = NumberSelectorMode.BOX,
) -> NumberSelector:
    """Einheitlichen numerischen Box-Selector erstellen."""

    config = NumberSelectorConfig(
        min=minimum,
        step=step,
        mode=mode,
    )
    if maximum is not None:
        config["max"] = maximum
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _address_schema(
    country: str | None, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Schema für eine vom Benutzer eingegebene Anschrift."""

    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_POSTAL_CODE, default=values.get(CONF_POSTAL_CODE, "")
            ): TextSelector(),
            vol.Required(
                CONF_STREET, default=values.get(CONF_STREET, "")
            ): TextSelector(),
            vol.Required(
                CONF_COUNTRY, default=values.get(CONF_COUNTRY, country or "DE")
            ): CountrySelector(),
        }
    )


def _location_source_schema(
    default: str = LOCATION_SOURCE_HOME_ASSISTANT,
) -> vol.Schema:
    """Schema für die verständliche Auswahl der Standortquelle."""

    return vol.Schema(
        {
            vol.Required(
                CONF_LOCATION_SOURCE,
                default=default,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        LOCATION_SOURCE_HOME_ASSISTANT,
                        LOCATION_SOURCE_ADDRESS,
                    ],
                    mode=SelectSelectorMode.LIST,
                    translation_key="location_source",
                )
            )
        }
    )


def _roof_schema(
    defaults: dict[str, Any] | None = None, *, include_add_another: bool = True
) -> vol.Schema:
    """Schema für eine Dachfläche mit optionalen Vorschlagswerten."""

    values = defaults or {}
    direction = _direction_for_azimuth(float(values.get(CONF_AZIMUTH, 180.0)))
    if CONF_SYSTEM_EFFICIENCY in values:
        efficiency = float(values[CONF_SYSTEM_EFFICIENCY])
    elif CONF_LOSS_FACTOR in values:
        efficiency = 100 - float(values[CONF_LOSS_FACTOR])
    else:
        efficiency = DEFAULT_SYSTEM_EFFICIENCY_PERCENT
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_NAME, default=values.get(CONF_NAME, "Dachfläche 1")
        ): TextSelector(),
        vol.Required(
            CONF_INSTALLED_POWER_KWP,
            default=values.get(CONF_INSTALLED_POWER_KWP, 5.0),
        ): _number_selector(minimum=0.01, step=0.01, unit="kWp"),
        vol.Required(CONF_AZIMUTH, default=direction): SelectSelector(
            SelectSelectorConfig(
                options=list(DIRECTION_TO_COMPASS_AZIMUTH),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="roof_direction",
            )
        ),
        vol.Required(CONF_TILT, default=values.get(CONF_TILT, 35.0)): _number_selector(
            minimum=0, maximum=90, step=1, unit="°"
        ),
        vol.Required(
            CONF_SYSTEM_EFFICIENCY,
            default=efficiency,
        ): _number_selector(
            minimum=0,
            maximum=100,
            step=1,
            unit="%",
            mode=NumberSelectorMode.SLIDER,
        ),
    }
    if include_add_another:
        schema[
            vol.Required(
                CONF_ADD_ANOTHER,
                default=bool(values.get(CONF_ADD_ANOTHER, False)),
            )
        ] = BooleanSelector()
    return vol.Schema(schema)


def _roof_selection_schema(roofs: list[dict[str, Any]]) -> vol.Schema:
    """Schema zur Auswahl einer bestehenden Dachfläche über ihren Namen."""

    return vol.Schema(
        {
            vol.Required(CONF_ROOF_ID): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=str(roof[CONF_ROOF_ID]), label=str(roof[CONF_NAME])
                        )
                        for roof in roofs
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _system_schema(inverter_limit: float | None = None) -> vol.Schema:
    """Schema für das optionale anlagenweite Wechselrichterlimit."""

    marker = vol.Optional(
        CONF_INVERTER_MAX_POWER_KW,
        description={"suggested_value": inverter_limit},
    )
    return vol.Schema({marker: _number_selector(minimum=0.01, step=0.01, unit="kW")})


def _inverter_limit_from_input(value: Any) -> float | None:
    """Optionales Wechselrichterlimit außerhalb des UI-Schemas validieren."""

    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise InvalidConfigurationError(
            "Wechselrichterleistung muss eine positive endliche Zahl sein"
        )
    return numeric


def _direction_for_azimuth(azimuth: float) -> str:
    """Gespeicherten Kompasswinkel auf eine der acht UI-Richtungen abbilden."""

    return min(
        DIRECTION_TO_COMPASS_AZIMUTH,
        key=lambda direction: abs(
            ((DIRECTION_TO_COMPASS_AZIMUTH[direction] - azimuth + 180) % 360) - 180
        ),
    )


def _localized_direction(azimuth: float) -> str:
    """Gespeicherte Ausrichtung deutsch für die Zusammenfassung ausgeben."""

    direction = _direction_for_azimuth(azimuth)
    return _DIRECTION_LABELS[direction]


def _persisted_roof(user_input: dict[str, Any], roof_id: str) -> dict[str, Any]:
    """UI-Werte in die persistierte Dachkonfiguration umwandeln."""

    direction = str(user_input[CONF_AZIMUTH])
    if direction not in DIRECTION_TO_COMPASS_AZIMUTH:
        raise InvalidConfigurationError("Ungültige Himmelsrichtung")
    persisted = {
        CONF_ROOF_ID: roof_id,
        CONF_NAME: str(user_input[CONF_NAME]).strip(),
        CONF_INSTALLED_POWER_KWP: float(user_input[CONF_INSTALLED_POWER_KWP]),
        CONF_AZIMUTH: DIRECTION_TO_COMPASS_AZIMUTH[direction],
        CONF_TILT: float(user_input[CONF_TILT]),
        CONF_LOSS_FACTOR: 100 - float(user_input[CONF_SYSTEM_EFFICIENCY]),
    }
    roof_from_dict(persisted)
    return persisted


def _roof_summary(roofs: list[dict[str, Any]]) -> str:
    """Dachflächen als Aufzählung für Zusammenfassung und Optionsmenü formatieren."""

    return "\n".join(
        (
            f"- **{roof[CONF_NAME]}:** {roof[CONF_INSTALLED_POWER_KWP]:g} kWp · "
            f"{_localized_direction(float(roof[CONF_AZIMUTH]))} · "
            f"{roof[CONF_TILT]:g}° · "
            f"{100 - roof[CONF_LOSS_FACTOR]:g}%"
        )
        for roof in roofs
    )


def _inverter_summary(inverter_limit: float | None) -> str:
    """Wechselrichterlimit für die Anzeige formatieren."""

    return (
        f"{float(inverter_limit):g} kW"
        if inverter_limit is not None
        else "nicht begrenzt"
    )


def _ensure_unique_roof_name(
    roof: dict[str, Any], existing_roofs: list[dict[str, Any]]
) -> None:
    """Doppelte sichtbare Dachnamen unabhängig von Großschreibung ablehnen."""

    normalized_name = str(roof[CONF_NAME]).casefold()
    if any(
        str(existing[CONF_NAME]).strip().casefold() == normalized_name
        for existing in existing_roofs
    ):
        raise DuplicateRoofNameError("Dachnamen müssen eindeutig sein")


class PvForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow für genau eine PV-Prognose-Konfiguration."""

    VERSION = 1

    def __init__(self) -> None:
        """Zwischenzustand des mehrstufigen Flows initialisieren."""

        self._location: dict[str, Any] = {}
        self._roofs: list[dict[str, Any]] = []
        self._roof_index = 0
        self._options: dict[str, Any] = {}

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> PvForecastOptionsFlow:
        """Options Flow bereitstellen."""

        return PvForecastOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Standortquelle in einem lokalisierten Formular wählen."""

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            location_source = user_input.get(CONF_LOCATION_SOURCE)
            if location_source == LOCATION_SOURCE_ADDRESS:
                return await self.async_step_address()
            elif location_source == LOCATION_SOURCE_HOME_ASSISTANT:
                try:
                    validate_coordinates(
                        self.hass.config.latitude, self.hass.config.longitude
                    )
                except InvalidConfigurationError:
                    errors["base"] = "invalid_home_location"
                else:
                    self._location = {
                        CONF_LATITUDE: self.hass.config.latitude,
                        CONF_LONGITUDE: self.hass.config.longitude,
                        CONF_TIME_ZONE: self.hass.config.time_zone,
                        CONF_LOCATION_SOURCE: LOCATION_SOURCE_HOME_ASSISTANT,
                        CONF_LOCATION_NAME: self.hass.config.location_name,
                        CONF_COUNTRY: self.hass.config.country,
                    }
                    return await self._async_location_complete()
            else:
                errors["base"] = "invalid_location_source"

        return self.async_show_form(
            step_id="user",
            data_schema=_location_source_schema(
                str(
                    self._location.get(
                        CONF_LOCATION_SOURCE, LOCATION_SOURCE_HOME_ASSISTANT
                    )
                )
            ),
            errors=errors,
            description_placeholders={
                "location_name": self.hass.config.location_name,
                "country": self.hass.config.country or "—",
            },
        )

    async def async_step_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine Anschrift einmalig in Koordinaten auflösen."""

        errors: dict[str, str] = {}
        if user_input is not None:
            postal_code = str(user_input.get(CONF_POSTAL_CODE, "")).strip()
            street = str(user_input.get(CONF_STREET, "")).strip()
            country = str(user_input.get(CONF_COUNTRY, "")).strip().upper()
            if not postal_code or not street or len(country) != 2:
                errors["base"] = "invalid_address"
            else:
                client = NominatimClient(async_get_clientsession(self.hass))
                try:
                    location = await client.async_geocode(
                        street,
                        postal_code,
                        country,
                        self.hass.config.language or "de",
                    )
                except AddressNotFoundError:
                    errors["base"] = "address_not_found"
                except GeocodingConnectionError:
                    errors["base"] = "geocoding_unavailable"
                except GeocodingDataError:
                    errors["base"] = "invalid_geocoding_response"
                except Exception:  # pragma: no cover - defensive HA flow boundary
                    _LOGGER.exception("Unerwarteter Fehler bei der Adressauflösung")
                    errors["base"] = "unknown"
                else:
                    self._location = {
                        CONF_LATITUDE: location.latitude,
                        CONF_LONGITUDE: location.longitude,
                        CONF_TIME_ZONE: self.hass.config.time_zone,
                        CONF_LOCATION_SOURCE: LOCATION_SOURCE_ADDRESS,
                        CONF_LOCATION_NAME: location.display_name,
                        CONF_POSTAL_CODE: postal_code,
                        CONF_STREET: street,
                        CONF_COUNTRY: country,
                    }
                    return await self._async_location_complete()

        return self.async_show_form(
            step_id="address",
            data_schema=_address_schema(
                self.hass.config.country, user_input or self._location
            ),
            errors=errors,
        )

    async def _async_location_complete(self) -> ConfigFlowResult:
        """Nach einer Standortänderung vorhandene Dächer erneut prüfen."""

        if self._roofs:
            return await self.async_step_system()
        return await self.async_step_roof()

    async def async_step_roof(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dachflächen nacheinander erfassen oder bereits erfasste bearbeiten."""

        errors: dict[str, str] = {}
        existing = (
            self._roofs[self._roof_index]
            if self._roof_index < len(self._roofs)
            else None
        )
        if user_input is not None:
            try:
                roof_id = (
                    str(existing[CONF_ROOF_ID]) if existing is not None else uuid4().hex
                )
                roof = _persisted_roof(user_input, roof_id)
                other_roofs = [
                    other
                    for index, other in enumerate(self._roofs)
                    if index != self._roof_index
                ]
                _ensure_unique_roof_name(roof, other_roofs)
                if existing is not None:
                    self._roofs[self._roof_index] = roof
                else:
                    self._roofs.append(roof)
            except DuplicateRoofNameError:
                errors["base"] = "duplicate_roof_name"
            except (KeyError, TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_roof"
            else:
                if bool(user_input.get(CONF_ADD_ANOTHER)):
                    self._roof_index += 1
                    return await self.async_step_roof()
                self._roof_index = 0
                return await self.async_step_system()

        defaults = (
            dict(existing)
            if existing is not None
            else {CONF_NAME: f"Dachfläche {len(self._roofs) + 1}"}
        )
        if existing is not None:
            defaults[CONF_ADD_ANOTHER] = self._roof_index + 1 < len(self._roofs)
        return self.async_show_form(
            step_id="roof",
            data_schema=_roof_schema(defaults),
            errors=errors,
        )

    async def async_step_system(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wechselrichterlimit erfassen und Open-Meteo vorab testen."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                inverter_limit = _inverter_limit_from_input(
                    user_input.get(CONF_INVERTER_MAX_POWER_KW)
                )
            except (TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_inverter"
            else:
                client = OpenMeteoClient(async_get_clientsession(self.hass))
                try:
                    roofs = tuple(roof_from_dict(roof) for roof in self._roofs)
                    await client.async_fetch_roofs(
                        self._location[CONF_LATITUDE],
                        self._location[CONF_LONGITUDE],
                        self._location[CONF_TIME_ZONE],
                        roofs,
                    )
                except OpenMeteoConnectionError:
                    errors["base"] = "cannot_connect"
                except OpenMeteoDataError:
                    errors["base"] = "invalid_response"
                except Exception:  # pragma: no cover - defensive HA flow boundary
                    _LOGGER.exception(
                        "Unerwarteter Fehler beim Open-Meteo-Verbindungstest"
                    )
                    errors["base"] = "unknown"
                else:
                    options: dict[str, Any] = {CONF_ROOFS: self._roofs}
                    if inverter_limit is not None:
                        options[CONF_INVERTER_MAX_POWER_KW] = inverter_limit
                    self._options = options
                    return await self.async_step_summary()

        return self.async_show_form(
            step_id="system",
            data_schema=_system_schema(self._options.get(CONF_INVERTER_MAX_POWER_KW)),
            errors=errors,
        )

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Geprüfte Konfiguration vor dem Anlegen zusammenfassen."""

        inverter_limit = self._options.get(CONF_INVERTER_MAX_POWER_KW)
        location_source = "Adresseingabe (OpenStreetMap/Nominatim)"
        if self._location[CONF_LOCATION_SOURCE] == LOCATION_SOURCE_HOME_ASSISTANT:
            location_source = "Home Assistant"
        return self.async_show_menu(
            step_id="summary",
            menu_options=_SUMMARY_MENU_LABELS,
            description_placeholders={
                "location": str(self._location[CONF_LOCATION_NAME]),
                "location_source": location_source,
                "latitude": f"{float(self._location[CONF_LATITUDE]):.6f}",
                "longitude": f"{float(self._location[CONF_LONGITUDE]):.6f}",
                "roofs": _roof_summary(self._roofs),
                "inverter": _inverter_summary(inverter_limit),
            },
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Die im Abschlussdialog bestätigte Konfiguration anlegen."""

        return self.async_create_entry(
            title="PV-Ertragsprognose",
            data=self._location,
            options=self._options,
        )

    async def async_step_edit_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Aus dem Abschlussdialog zur Standortauswahl zurückkehren."""

        return await self.async_step_user()

    async def async_step_edit_roofs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Aus dem Abschlussdialog zu den bereits erfassten Dachflächen zurückkehren."""

        self._roof_index = 0
        return await self.async_step_roof()

    async def async_step_edit_system(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Aus dem Abschlussdialog zum Wechselrichterlimit zurückkehren."""

        return await self.async_step_system()


class PvForecastOptionsFlow(OptionsFlow):
    """Menübasierter Options Flow zum gezielten Bearbeiten einzelner Dachflächen.

    Jede Aktion (hinzufügen, bearbeiten, entfernen, Wechselrichterlimit) wirkt
    für sich allein und lässt die übrigen Dachflächen unverändert. Das
    Entfernen erfordert eine ausdrückliche Bestätigung.
    """

    def __init__(self) -> None:
        """Options-Flow-Zwischenzustand initialisieren."""

        self._selected_roof_id: str | None = None

    def _roofs(self) -> list[dict[str, Any]]:
        """Aktuell gespeicherte Dachflächen als veränderbare Kopien lesen."""

        return [dict(roof) for roof in self.config_entry.options[CONF_ROOFS]]

    def _finish(
        self, roofs: list[dict[str, Any]], inverter_limit: float | None
    ) -> ConfigFlowResult:
        """Aktualisierte Dachflächen und Wechselrichterlimit speichern."""

        options: dict[str, Any] = {CONF_ROOFS: roofs}
        if inverter_limit is not None:
            options[CONF_INVERTER_MAX_POWER_KW] = inverter_limit
        return self.async_create_entry(title="", data=options)

    def _finish_with_unchanged_inverter(
        self, roofs: list[dict[str, Any]]
    ) -> ConfigFlowResult:
        """Dachflächen speichern, ohne das bestehende Wechselrichterlimit anzufassen."""

        current_limit = self.config_entry.options.get(CONF_INVERTER_MAX_POWER_KW)
        return self._finish(
            roofs, float(current_limit) if current_limit is not None else None
        )

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menü mit den vorhandenen Dachflächen und den verfügbaren Aktionen."""

        roofs = self._roofs()
        inverter_limit = self.config_entry.options.get(CONF_INVERTER_MAX_POWER_KW)
        menu_options = ["add_roof"]
        if roofs:
            menu_options.extend(["edit_roof", "remove_roof"])
        menu_options.append("system")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={
                "roofs": _roof_summary(roofs),
                "inverter": _inverter_summary(
                    float(inverter_limit) if inverter_limit is not None else None
                ),
            },
        )

    async def async_step_add_roof(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine neue Dachfläche hinzufügen, ohne die übrigen zu verändern."""

        errors: dict[str, str] = {}
        roofs = self._roofs()
        if user_input is not None:
            try:
                roof = _persisted_roof(user_input, uuid4().hex)
                _ensure_unique_roof_name(roof, roofs)
            except DuplicateRoofNameError:
                errors["base"] = "duplicate_roof_name"
            except (KeyError, TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_roof"
            else:
                roofs.append(roof)
                return self._finish_with_unchanged_inverter(roofs)

        return self.async_show_form(
            step_id="add_roof",
            data_schema=_roof_schema(
                {CONF_NAME: f"Dachfläche {len(roofs) + 1}"},
                include_add_another=False,
            ),
            errors=errors,
        )

    async def async_step_edit_roof(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Zu bearbeitende Dachfläche auswählen."""

        if user_input is not None:
            self._selected_roof_id = str(user_input[CONF_ROOF_ID])
            return await self.async_step_edit_roof_details()

        return self.async_show_form(
            step_id="edit_roof",
            data_schema=_roof_selection_schema(self._roofs()),
        )

    async def async_step_edit_roof_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Die ausgewählte Dachfläche bearbeiten; ihre ID bleibt stabil."""

        errors: dict[str, str] = {}
        roofs = self._roofs()
        index = next(
            index
            for index, roof in enumerate(roofs)
            if roof[CONF_ROOF_ID] == self._selected_roof_id
        )
        existing = roofs[index]
        if user_input is not None:
            try:
                roof = _persisted_roof(user_input, str(existing[CONF_ROOF_ID]))
                other_roofs = [
                    other
                    for other_index, other in enumerate(roofs)
                    if other_index != index
                ]
                _ensure_unique_roof_name(roof, other_roofs)
            except DuplicateRoofNameError:
                errors["base"] = "duplicate_roof_name"
            except (KeyError, TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_roof"
            else:
                roofs[index] = roof
                return self._finish_with_unchanged_inverter(roofs)

        return self.async_show_form(
            step_id="edit_roof_details",
            data_schema=_roof_schema(existing, include_add_another=False),
            errors=errors,
            description_placeholders={"roof_name": str(existing[CONF_NAME])},
        )

    async def async_step_remove_roof(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Zu entfernende Dachfläche auswählen."""

        if user_input is not None:
            self._selected_roof_id = str(user_input[CONF_ROOF_ID])
            return await self.async_step_confirm_remove_roof()

        return self.async_show_form(
            step_id="remove_roof",
            data_schema=_roof_selection_schema(self._roofs()),
        )

    async def async_step_confirm_remove_roof(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Das Entfernen der ausgewählten Dachfläche ausdrücklich bestätigen."""

        roofs = self._roofs()
        roof = next(
            candidate
            for candidate in roofs
            if candidate[CONF_ROOF_ID] == self._selected_roof_id
        )
        if user_input is not None:
            if bool(user_input.get(CONF_CONFIRM_REMOVE)):
                remaining = [
                    candidate
                    for candidate in roofs
                    if candidate[CONF_ROOF_ID] != self._selected_roof_id
                ]
                return self._finish_with_unchanged_inverter(remaining)
            return await self.async_step_init()

        return self.async_show_form(
            step_id="confirm_remove_roof",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRM_REMOVE, default=False): BooleanSelector()}
            ),
            description_placeholders={"roof_name": str(roof[CONF_NAME])},
        )

    async def async_step_system(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Anlagenweites Wechselrichterlimit bearbeiten."""

        errors: dict[str, str] = {}
        current_limit = self.config_entry.options.get(CONF_INVERTER_MAX_POWER_KW)
        if user_input is not None:
            try:
                inverter_limit = _inverter_limit_from_input(
                    user_input.get(CONF_INVERTER_MAX_POWER_KW)
                )
            except (TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_inverter"
            else:
                return self._finish(self._roofs(), inverter_limit)

        return self.async_show_form(
            step_id="system",
            data_schema=_system_schema(
                float(current_limit) if current_limit is not None else None
            ),
            errors=errors,
        )
