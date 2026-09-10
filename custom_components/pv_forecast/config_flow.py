"""UI-basierte Einrichtung und Bearbeitung der PV-Ertragsprognose."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, override
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
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
from homeassistant.helpers.translation import async_get_translations

from .api import OpenMeteoConnectionError, OpenMeteoDataError
from .calculations import InvalidConfigurationError, validate_coordinates
from .calibration_configuration import CalibrationFlowMixin
from .configuration import location_fingerprint, roof_from_dict, roofs_from_options
from .const import (
    CONF_ADD_ANOTHER,
    CONF_AZIMUTH,
    CONF_CONFIRM_REMOVE,
    CONF_COUNTRY,
    CONF_CUSTOM_AZIMUTH,
    CONF_GROUP_ROOF_IDS,
    CONF_INSTALLED_POWER_KWP,
    CONF_INVERTER_GROUPS,
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
    ROOF_DIRECTION_CUSTOM,
)
from .dashboard import CONF_DASHBOARD_ENABLED, CONF_DASHBOARD_REVISION
from .dashboard_configuration import DashboardFlowMixin
from .geocoding import (
    AddressNotFoundError,
    GeocodingConnectionError,
    GeocodingDataError,
    NominatimClient,
)
from .history_configuration import HistoryFlowMixin
from .horizon import (
    CONF_FORECAST_DAYS,
    forecast_days_from_options,
    validate_forecast_days,
)
from .inverter_configuration import InverterGroupFlowMixin, groups_for_remaining_roofs
from .measurement_configuration import MeasurementFlowMixin
from .reconfiguration import ReconfigurationChangedError, async_prepare_location_change
from .runtime import async_get_open_meteo_client
from .shading import CONF_HORIZON_PROFILES
from .shading_configuration import ShadingFlowMixin

_LOGGER = logging.getLogger(__name__)


async def _async_ui_translations(hass: HomeAssistant) -> dict[str, str]:
    """Dynamische UI-Texte aus den deutschen HA-Sprachressourcen lesen."""

    resources = await asyncio.gather(
        *(
            async_get_translations(hass, "de", category, integrations={DOMAIN})
            for category in ("common", "selector", "title")
        )
    )
    prefix = f"component.{DOMAIN}."
    return {
        key.removeprefix(prefix): value
        for resource in resources
        for key, value in resource.items()
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
    azimuth = values.get(CONF_AZIMUTH, 180.0)
    direction = (
        azimuth if isinstance(azimuth, str) else _direction_for_azimuth(float(azimuth))
    )
    custom_azimuth = values.get(
        CONF_CUSTOM_AZIMUTH, azimuth if not isinstance(azimuth, str) else None
    )
    if CONF_SYSTEM_EFFICIENCY in values:
        efficiency = float(values[CONF_SYSTEM_EFFICIENCY])
    elif CONF_LOSS_FACTOR in values:
        efficiency = 100 - float(values[CONF_LOSS_FACTOR])
    else:
        efficiency = DEFAULT_SYSTEM_EFFICIENCY_PERCENT
    schema: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=values.get(CONF_NAME, "")): TextSelector(),
        vol.Required(
            CONF_INSTALLED_POWER_KWP,
            default=values.get(CONF_INSTALLED_POWER_KWP, 5.0),
        ): _number_selector(minimum=0.01, step=0.01, unit="kWp"),
        vol.Required(CONF_AZIMUTH, default=direction): SelectSelector(
            SelectSelectorConfig(
                options=[*DIRECTION_TO_COMPASS_AZIMUTH, ROOF_DIRECTION_CUSTOM],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="roof_direction",
            )
        ),
        vol.Optional(
            CONF_CUSTOM_AZIMUTH,
            **({"default": custom_azimuth} if custom_azimuth is not None else {}),
        ): _number_selector(minimum=0, maximum=360, step="any", unit="°"),
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
    """Nur exakte Himmelsrichtungen vorbelegen; freie Winkel bleiben erhalten."""

    return next(
        (
            direction
            for direction, value in DIRECTION_TO_COMPASS_AZIMUTH.items()
            if value == azimuth
        ),
        ROOF_DIRECTION_CUSTOM,
    )


def _localized_direction(azimuth: float, translations: dict[str, str]) -> str:
    """Gespeicherte Ausrichtung deutsch für die Zusammenfassung ausgeben."""

    direction = _direction_for_azimuth(azimuth)
    if direction == ROOF_DIRECTION_CUSTOM:
        return f"{azimuth}°"
    return translations[f"selector.roof_direction.options.{direction}"]


def _persisted_roof(user_input: dict[str, Any], roof_id: str) -> dict[str, Any]:
    """UI-Werte in die persistierte Dachkonfiguration umwandeln."""

    direction = str(user_input[CONF_AZIMUTH])
    if direction == ROOF_DIRECTION_CUSTOM:
        azimuth = float(user_input[CONF_CUSTOM_AZIMUTH])
    elif direction in DIRECTION_TO_COMPASS_AZIMUTH:
        azimuth = DIRECTION_TO_COMPASS_AZIMUTH[direction]
    else:
        raise InvalidConfigurationError("Ungültige Himmelsrichtung")
    persisted = {
        CONF_ROOF_ID: roof_id,
        CONF_NAME: str(user_input[CONF_NAME]).strip(),
        CONF_INSTALLED_POWER_KWP: float(user_input[CONF_INSTALLED_POWER_KWP]),
        CONF_AZIMUTH: azimuth,
        CONF_TILT: float(user_input[CONF_TILT]),
        CONF_LOSS_FACTOR: 100 - float(user_input[CONF_SYSTEM_EFFICIENCY]),
    }
    roof_from_dict(persisted)
    return persisted


def _roof_summary(roofs: list[dict[str, Any]], translations: dict[str, str]) -> str:
    """Dachflächen als Aufzählung für Zusammenfassung und Optionsmenü formatieren."""

    return "\n".join(
        (
            f"- **{roof[CONF_NAME]}:** {roof[CONF_INSTALLED_POWER_KWP]:g} kWp · "
            f"{_localized_direction(float(roof[CONF_AZIMUTH]), translations)} · "
            f"{roof[CONF_TILT]:g}° · "
            f"{100 - roof[CONF_LOSS_FACTOR]:g}%"
        )
        for roof in roofs
    )


def _inverter_summary(
    inverter_limit: float | None, translations: dict[str, str]
) -> str:
    """Wechselrichterlimit für die Anzeige formatieren."""

    return (
        f"{float(inverter_limit):g} kW"
        if inverter_limit is not None
        else translations["common.inverter_unlimited"]
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


def _optional_summary(options: dict[str, Any], translations: dict[str, str]) -> str:
    """Bereits gewählte freiwillige Funktionen aus den HA-Texten zusammenfassen."""
    return translations["common.optional_summary"].format(
        sources=len(options.get("measurement_sources", [])),
        history=translations[
            (
                "common.option_on"
                if options.get("history_enabled")
                else "common.option_off"
            )
        ],
        dashboard=translations[
            (
                "common.option_on"
                if options.get(CONF_DASHBOARD_ENABLED)
                else "common.option_off"
            )
        ],
    )


class PvForecastConfigFlow(
    DashboardFlowMixin,
    HistoryFlowMixin,
    MeasurementFlowMixin,
    ConfigFlow,
    domain=DOMAIN,
):
    """Config Flow für unabhängig konfigurierte logische PV-Anlagen."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Zwischenzustand des mehrstufigen Flows initialisieren."""

        self._location: dict[str, Any] = {}
        self._plant_unique_id = uuid4().hex
        self._confirmed_neighbors: set[str] = set()
        self._confirmed_site: tuple[float, float] | None = None
        self._roofs: list[dict[str, Any]] = []
        self._roof_index = 0
        self._options: dict[str, Any] = {}
        self._reconfigure_entry: ConfigEntry | None = None
        self._reconfigure_original_data: dict[str, Any] = {}
        self._reconfigure_original_options: dict[str, Any] = {}
        self._reconfigure_tested = False

    def _measurement_options(self) -> dict[str, Any]:
        """Messquellen zusammen mit den übrigen Einrichtungseingaben halten."""

        return self._options

    def _measurement_timezone(self) -> str:
        """Den bereits bestätigten Anlagenstandort für Messvorschauen verwenden."""

        return str(self._location[CONF_TIME_ZONE])

    async def async_step_measurements_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nach der optionalen Zuordnung zum Abschlussdialog zurückkehren."""

        return await self.async_step_summary()

    async def async_step_history_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nach der optionalen Archivierung zum Abschlussdialog zurückkehren."""

        return await self.async_step_summary()

    def _dashboard_context(self) -> tuple[dict[str, Any], str, str]:
        name = str(
            self._location.get(
                "plant_name", self._location.get(CONF_LOCATION_NAME, "PV")
            )
        )
        return self._options, name, "after_setup"

    async def _async_dashboard_done(self, options: dict[str, Any]) -> ConfigFlowResult:
        self._options = options
        return await self.async_step_summary()

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

        if self._reconfigure_entry is None:
            await self.async_set_unique_id(self._plant_unique_id)
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

    def _site_key(self) -> tuple[float, float]:
        """Gerundete Koordinaten nur als Hinweis auf mögliche Duplikate verwenden."""
        return tuple(
            round(float(self._location[key]), 4)
            for key in (CONF_LATITUDE, CONF_LONGITUDE)
        )

    def _neighbors(self) -> list[ConfigEntry]:
        return [
            entry
            for entry in self._async_current_entries()
            if tuple(
                round(float(entry.data[key]), 4)
                for key in (CONF_LATITUDE, CONF_LONGITUDE)
            )
            == self._site_key()
        ]

    def _plant_name_taken(self, name: str) -> bool:
        """Auch unmittelbar vor Abschluss inzwischen vergebene Namen erkennen."""
        return any(
            name.strip().casefold()
            in {
                str(
                    entry.data.get(
                        "plant_name",
                        entry.data.get(CONF_LOCATION_NAME, entry.title),
                    )
                )
                .strip()
                .casefold(),
                entry.title.strip().casefold(),
            }
            for entry in self._async_current_entries()
        )

    def _needs_plant_confirmation(self) -> bool:
        return bool(self._async_current_entries()) and (
            not self._location.get("plant_name")
            or self._plant_name_taken(str(self._location.get("plant_name", "")))
            or self._confirmed_site != self._site_key()
            or not {entry.entry_id for entry in self._neighbors()}
            <= self._confirmed_neighbors
        )

    async def _async_location_complete(self) -> ConfigFlowResult:
        """Neue Standorte gegen vorhandene und laufende Einrichtungen prüfen."""
        if self._reconfigure_entry is not None:
            if "plant_name" in self._reconfigure_entry.data:
                self._location["plant_name"] = self._reconfigure_entry.data[
                    "plant_name"
                ]
            self._reconfigure_tested = False
            return await self.async_step_reconfigure_confirm()
        self.context["pv_site"] = self._site_key()
        if any(
            flow["flow_id"] != self.flow_id
            and flow["context"].get("pv_site") == self._site_key()
            for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        ):
            return self.async_abort(reason="already_in_progress")
        if self._needs_plant_confirmation():
            return await self.async_step_plant()
        return await self._async_plant_complete()

    async def _async_plant_complete(self) -> ConfigFlowResult:
        if self._roofs:
            return await self.async_step_system()
        return await self.async_step_roof()

    async def async_step_plant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Weitere Anlagen benennen und am selben Ort ausdrücklich unterscheiden."""
        neighbors = self._neighbors()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get("plant_name")
            if neighbors and user_input.get("confirm_separate_plant") is not True:
                return self.async_abort(reason="already_configured")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                errors["base"] = "invalid_plant_name"
            elif self._plant_name_taken(name):
                errors["base"] = "duplicate_plant_name"
            else:
                self._location["plant_name"] = name.strip()
                self._confirmed_site = self._site_key()
                self._confirmed_neighbors = {entry.entry_id for entry in neighbors}
                return await self._async_plant_complete()
        schema = {
            vol.Required(
                "plant_name",
                default=self._location.get(
                    "plant_name", self._location[CONF_LOCATION_NAME]
                ),
            ): TextSelector()
        }
        if neighbors:
            schema[vol.Required("confirm_separate_plant", default=False)] = (
                BooleanSelector()
            )
        return self.async_show_form(
            step_id="plant",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "existing": ", ".join(
                    entry.title for entry in self._async_current_entries()
                )
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Den Standort bewusst bearbeiten und unabhängige Optionen bewahren."""

        if self._reconfigure_entry is None:
            entry = self._get_reconfigure_entry()
            await self.async_set_unique_id(entry.unique_id)
            self._reconfigure_entry = entry
            self._reconfigure_original_data = deepcopy(dict(entry.data))
            self._reconfigure_original_options = deepcopy(dict(entry.options))
            self._location = dict(entry.data)
            self._roofs = [dict(roof) for roof in entry.options[CONF_ROOFS]]
        return await self.async_step_user(user_input)

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Geprüften Standort bestätigen und bestehende Mess-/Archivgrenzen bewahren."""

        entry = self._reconfigure_entry
        assert entry is not None
        if (
            dict(entry.data) != self._reconfigure_original_data
            or dict(entry.options) != self._reconfigure_original_options
        ):
            return self.async_abort(reason="reconfigure_entry_changed")
        errors: dict[str, str] = {}
        if not self._reconfigure_tested:
            client = async_get_open_meteo_client(self.hass)
            try:
                if not self._roofs:
                    raise InvalidConfigurationError(
                        "Eine Dachfläche ist für den Verbindungstest erforderlich"
                    )
                if CONF_TIME_ZONE not in self._location:
                    self._location[CONF_TIME_ZONE] = (
                        await client.async_resolve_timezone(
                            self._location[CONF_LATITUDE],
                            self._location[CONF_LONGITUDE],
                        )
                    )
                await client.async_fetch_roofs(
                    self._location[CONF_LATITUDE],
                    self._location[CONF_LONGITUDE],
                    self._location[CONF_TIME_ZONE],
                    roofs_from_options(dict(self._options) | {CONF_ROOFS: self._roofs}),
                )
                self._reconfigure_tested = True
            except OpenMeteoConnectionError:
                errors["base"] = "cannot_connect"
            except OpenMeteoDataError:
                errors["base"] = "invalid_response"
            except InvalidConfigurationError:
                errors["base"] = "invalid_roof"
        if user_input is not None and self._reconfigure_tested:
            physical_changed = location_fingerprint(
                self._location
            ) != location_fingerprint(entry.data)
            try:
                if physical_changed:
                    await async_prepare_location_change(self.hass, entry)
            except ReconfigurationChangedError:
                return self.async_abort(reason="reconfigure_entry_changed")
            except (
                HomeAssistantError,
                NotImplementedError,
                ValueError,
                KeyError,
                TypeError,
                OSError,
            ):
                _LOGGER.exception(
                    "Standortwechsel scheitert an der bestehenden Datengrundlage"
                )
                errors["base"] = "reconfigure_storage_unavailable"
            else:
                if (
                    dict(entry.data) != self._reconfigure_original_data
                    or dict(entry.options) != self._reconfigure_original_options
                ):
                    if physical_changed:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_abort(reason="reconfigure_entry_changed")
                # Ein physischer Wechsel hat die bisherigen Update-Listener beim
                # Entladen beendet. Sonst plant der vorhandene Listener den Reload.
                has_reload_listener = bool(entry.update_listeners)
                unchanged = dict(entry.data) == self._location
                result = self.async_update_and_abort(entry, data=self._location)
                if not has_reload_listener or unchanged:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return result
        translations = await _async_ui_translations(self.hass)
        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "plant_name": str(
                    self._location.get("plant_name", self._location[CONF_LOCATION_NAME])
                ),
                "location": str(self._location[CONF_LOCATION_NAME]),
                "latitude": str(self._location[CONF_LATITUDE]),
                "longitude": str(self._location[CONF_LONGITUDE]),
                "timezone": str(
                    self._location.get(
                        CONF_TIME_ZONE, translations["common.timezone_pending"]
                    )
                ),
            },
        )

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
                errors[CONF_NAME] = "duplicate_roof_name"
            except (KeyError, TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_roof"
            else:
                if bool(user_input.get(CONF_ADD_ANOTHER)):
                    self._roof_index += 1
                    return await self.async_step_roof()
                self._roof_index = 0
                return await self.async_step_system()

        translations = await _async_ui_translations(self.hass)
        defaults = (
            dict(existing)
            if existing is not None
            else {
                CONF_NAME: translations["common.default_roof_name"].format(
                    number=len(self._roofs) + 1
                )
            }
        )
        if existing is not None:
            defaults[CONF_ADD_ANOTHER] = self._roof_index + 1 < len(self._roofs)
        return self.async_show_form(
            step_id="roof",
            data_schema=_roof_schema(defaults | (user_input or {})),
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
                errors[CONF_INVERTER_MAX_POWER_KW] = "invalid_inverter"
            else:
                client = async_get_open_meteo_client(self.hass)
                try:
                    roofs = roofs_from_options(
                        dict(self._options) | {CONF_ROOFS: self._roofs}
                    )
                    if CONF_TIME_ZONE not in self._location:
                        self._location[CONF_TIME_ZONE] = (
                            await client.async_resolve_timezone(
                                self._location[CONF_LATITUDE],
                                self._location[CONF_LONGITUDE],
                            )
                        )
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
                    options: dict[str, Any] = dict(self._options)
                    options[CONF_ROOFS] = self._roofs
                    options.pop(CONF_INVERTER_MAX_POWER_KW, None)
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
        translations = await _async_ui_translations(self.hass)
        location_source = translations[
            f"common.location_source_{self._location[CONF_LOCATION_SOURCE]}"
        ]
        return self.async_show_menu(
            step_id="summary",
            menu_options=[
                "finish",
                "edit_location",
                "edit_roofs",
                "edit_system",
                "measurements",
                "history",
                "dashboard",
            ],
            description_placeholders={
                "dashboard": translations[
                    (
                        "common.dashboard_selected"
                        if self._options.get(CONF_DASHBOARD_ENABLED) is True
                        else "common.dashboard_off"
                    )
                ],
                "plant_name": str(
                    self._location.get("plant_name", self._location[CONF_LOCATION_NAME])
                ),
                "location": str(self._location[CONF_LOCATION_NAME]),
                "location_source": location_source,
                "latitude": f"{float(self._location[CONF_LATITUDE]):.6f}",
                "longitude": f"{float(self._location[CONF_LONGITUDE]):.6f}",
                "timezone": str(self._location[CONF_TIME_ZONE]),
                "roofs": _roof_summary(self._roofs, translations),
                "optional": _optional_summary(self._options, translations),
                "inverter": _inverter_summary(inverter_limit, translations),
            },
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Die im Abschlussdialog bestätigte Konfiguration anlegen."""

        translations = await _async_ui_translations(self.hass)
        if self._needs_plant_confirmation():
            return await self.async_step_plant()
        if (
            self._options.get("measurement_sources")
            and not await self._async_save_measurement_helpers()
        ):
            return await self.async_step_measurement_save()
        name = self._location.get("plant_name", self._location[CONF_LOCATION_NAME])
        return self.async_create_entry(
            title=f"{translations['title']} · {name}",
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


class PvForecastOptionsFlow(
    DashboardFlowMixin,
    ShadingFlowMixin,
    InverterGroupFlowMixin,
    CalibrationFlowMixin,
    HistoryFlowMixin,
    MeasurementFlowMixin,
    OptionsFlow,
):
    """Menübasierter Options Flow zum gezielten Bearbeiten einzelner Dachflächen.

    Jede Aktion (hinzufügen, bearbeiten, entfernen, Wechselrichterlimit) wirkt
    für sich allein und lässt die übrigen Dachflächen unverändert. Das
    Entfernen erfordert eine ausdrückliche Bestätigung.
    """

    def __init__(self) -> None:
        """Options-Flow-Zwischenzustand initialisieren."""

        self._selected_roof_id: str | None = None
        self._roof_removal_groups: list[dict[str, Any]] | None = None
        self._measurement_draft: dict[str, Any] | None = None
        self._original_data: dict[str, Any] | None = None
        self._original_options: dict[str, Any] | None = None

    def _check_options_unchanged(self) -> None:
        """Veraltete Entwürfe vor Änderungen und vor dem Speichern zurückweisen."""

        entry = self.config_entry
        options = {
            key: value
            for key, value in entry.options.items()
            if key != CONF_DASHBOARD_REVISION
        }
        if self._original_data is None:
            self._original_data = deepcopy(dict(entry.data))
            self._original_options = deepcopy(options)
        elif (
            self._original_data != dict(entry.data) or self._original_options != options
        ):
            raise AbortFlow("reconfigure_entry_changed")

    @callback
    @override
    def async_create_entry(
        self,
        *,
        title: str | None = None,
        data: Mapping[str, Any],
        description: str | None = None,
        description_placeholders: Mapping[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Nur einen aktuellen Entwurf mit der neuesten internen Fassung speichern."""

        self._check_options_unchanged()
        options = dict(data)
        options.pop(CONF_DASHBOARD_REVISION, None)
        if CONF_DASHBOARD_REVISION in self.config_entry.options:
            options[CONF_DASHBOARD_REVISION] = self.config_entry.options[
                CONF_DASHBOARD_REVISION
            ]
        return super().async_create_entry(
            title=title,
            data=options,
            description=description,
            description_placeholders=description_placeholders,
        )

    def _dashboard_context(self) -> tuple[dict[str, Any], str, str]:
        self._check_options_unchanged()
        entry = self.config_entry
        manager = getattr(getattr(entry, "runtime_data", None), "dashboard", None)
        return (
            dict(entry.options),
            str(
                entry.data.get(
                    "plant_name", entry.data.get(CONF_LOCATION_NAME, entry.title)
                )
            ),
            manager.status if manager is not None else "after_setup",
        )

    async def _async_dashboard_done(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=options)

    def _measurement_options(self) -> dict[str, Any]:
        """Alle unabhängigen Optionen beim Bearbeiten der Quellen bewahren."""

        self._check_options_unchanged()
        if self._measurement_draft is None:
            self._measurement_draft = deepcopy(dict(self.config_entry.options))
        return self._measurement_draft

    def _measurement_entry(self) -> ConfigEntry:
        """Vorhandenen Eintrag für Vorschau und gezielte Datenlöschung liefern."""

        return self.config_entry

    async def async_step_measurements_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Die bestätigten Messquellen speichern und den Options Flow abschließen."""

        if not await self._async_save_measurement_helpers():
            return await self.async_step_measurement_save()
        return self.async_create_entry(title="", data=self._measurement_options())

    async def async_step_history_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Archivoptionen mit allen unabhängigen Einstellungen speichern."""

        return self.async_create_entry(title="", data=self._history_options())

    def _roofs(self) -> list[dict[str, Any]]:
        """Aktuell gespeicherte Dachflächen als veränderbare Kopien lesen."""

        self._check_options_unchanged()
        return [dict(roof) for roof in self.config_entry.options[CONF_ROOFS]]

    async def async_step_confirm_measurement_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Messdaten nur unter der im Dialog bestätigten Konfiguration löschen."""

        self._check_options_unchanged()
        return await super().async_step_confirm_measurement_delete(user_input)

    async def async_step_delete_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Archivlöschung bei einem inzwischen veralteten Dialog verhindern."""

        self._check_options_unchanged()
        return await super().async_step_delete_history(user_input)

    async def async_step_reset_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Lernzustand nur für die unverändert bestätigte Anlage zurücksetzen."""

        self._check_options_unchanged()
        return await super().async_step_reset_calibration(user_input)

    async def async_step_underperformance_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Hinweisaktionen an einen noch aktuellen Optionsdialog binden."""

        self._check_options_unchanged()
        return await super().async_step_underperformance_control(user_input)

    def _finish(
        self, roofs: list[dict[str, Any]], inverter_limit: float | None
    ) -> ConfigFlowResult:
        """Aktualisierte Dachflächen und Wechselrichterlimit speichern."""

        options: dict[str, Any] = dict(self.config_entry.options)
        options[CONF_ROOFS] = roofs
        if CONF_HORIZON_PROFILES in options:
            options[CONF_HORIZON_PROFILES] = {
                key: value
                for key, value in options[CONF_HORIZON_PROFILES].items()
                if key in {roof[CONF_ROOF_ID] for roof in roofs}
            }
        if CONF_INVERTER_GROUPS in options:
            options[CONF_INVERTER_GROUPS] = groups_for_remaining_roofs(
                options[CONF_INVERTER_GROUPS],
                {str(roof[CONF_ROOF_ID]) for roof in roofs},
            )
        options.pop(CONF_INVERTER_MAX_POWER_KW, None)
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
        menu_options = [
            "plant_options",
            "measurements",
            "dashboard",
            "advanced_options",
        ]
        translations = await _async_ui_translations(self.hass)
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={
                "roofs": _roof_summary(roofs, translations),
                "optional": _optional_summary(
                    dict(self.config_entry.options), translations
                ),
                "inverter": _inverter_summary(
                    float(inverter_limit) if inverter_limit is not None else None,
                    translations,
                ),
            },
        )

    async def async_step_plant_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Häufige Aufgaben an Dachflächen und Anlagenlimit bündeln."""
        roofs = self._roofs()
        return self.async_show_menu(
            step_id="plant_options",
            menu_options=["add_roof"]
            + (["edit_roof", "remove_roof"] if roofs else [])
            + ["system", "init"],
        )

    async def async_step_advanced_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionale Modelle und Vergleiche getrennt von der Grundeinrichtung zeigen."""
        roofs = self._roofs()
        return self.async_show_menu(
            step_id="advanced_options",
            menu_options=[
                "history",
                "calibration",
                "forecast_horizon",
                "inverter_groups",
            ]
            + (["horizon_profile"] if roofs else [])
            + ["init"],
        )

    async def async_step_forecast_horizon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Den optionalen Horizont ohne Änderung unabhängiger Optionen speichern."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                days = validate_forecast_days(user_input.get(CONF_FORECAST_DAYS))
            except ValueError:
                errors[CONF_FORECAST_DAYS] = "invalid_forecast_days"
            else:
                return self.async_create_entry(
                    title="",
                    data=dict(self.config_entry.options) | {CONF_FORECAST_DAYS: days},
                )
        return self.async_show_form(
            step_id="forecast_horizon",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FORECAST_DAYS,
                        default=forecast_days_from_options(self.config_entry.options),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=2, max=7, step=1, mode=NumberSelectorMode.BOX
                        )
                    )
                }
            ),
            errors=errors,
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
                errors[CONF_NAME] = "duplicate_roof_name"
            except (KeyError, TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_roof"
            else:
                roofs.append(roof)
                return self._finish_with_unchanged_inverter(roofs)

        translations = await _async_ui_translations(self.hass)
        return self.async_show_form(
            step_id="add_roof",
            data_schema=_roof_schema(
                {
                    CONF_NAME: translations["common.default_roof_name"].format(
                        number=len(roofs) + 1
                    )
                }
                | (user_input or {}),
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
                errors[CONF_NAME] = "duplicate_roof_name"
            except (KeyError, TypeError, ValueError, InvalidConfigurationError):
                errors["base"] = "invalid_roof"
            else:
                roofs[index] = roof
                return self._finish_with_unchanged_inverter(roofs)

        return self.async_show_form(
            step_id="edit_roof_details",
            data_schema=_roof_schema(
                existing | (user_input or {}), include_add_another=False
            ),
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

        errors: dict[str, str] = {}
        roofs = self._roofs()
        roof = next(
            candidate
            for candidate in roofs
            if candidate[CONF_ROOF_ID] == self._selected_roof_id
        )
        if user_input is not None:
            if bool(user_input.get(CONF_CONFIRM_REMOVE)):
                if len(roofs) <= 1:
                    errors["base"] = "last_roof_required"
                elif self._roof_removal_groups != self._inverter_groups():
                    return self.async_abort(reason="reconfigure_entry_changed")
                else:
                    remaining = [
                        candidate
                        for candidate in roofs
                        if candidate[CONF_ROOF_ID] != self._selected_roof_id
                    ]
                    return self._finish_with_unchanged_inverter(remaining)
            else:
                return await self.async_step_init()

        texts = await self._async_inverter_texts()
        self._roof_removal_groups = deepcopy(self._inverter_groups())
        changes = []
        for group in self._inverter_groups():
            if self._selected_roof_id in group[CONF_GROUP_ROOF_IDS]:
                key = (
                    "inverter_group_remove_empty"
                    if len(group[CONF_GROUP_ROOF_IDS]) == 1
                    else "inverter_group_remove_roof"
                )
                changes.append(texts[key].format(name=group[CONF_NAME]))
        return self.async_show_form(
            step_id="confirm_remove_roof",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRM_REMOVE, default=False): BooleanSelector()}
            ),
            description_placeholders={
                "roof_name": str(roof[CONF_NAME]),
                "inverter_group_changes": "\n\n".join(changes),
            },
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
                errors[CONF_INVERTER_MAX_POWER_KW] = "invalid_inverter"
            else:
                return self._finish(self._roofs(), inverter_limit)

        return self.async_show_form(
            step_id="system",
            data_schema=_system_schema(
                float(current_limit) if current_limit is not None else None
            ),
            errors=errors,
        )
