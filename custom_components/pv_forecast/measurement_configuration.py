"""Optionale, ausdrücklich bestätigte HA-Messquellen im Einrichtungsablauf."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .const import CONF_TIME_ZONE, DOMAIN
from .measurement_adapters import available_measurement_devices
from .measurement_helpers import (
    PENDING_HELPER,
    async_resolve_measurement_helpers,
    source_power_registry_id,
)
from .measurements import SourceConfig, normalize_reading_value

CONF_MEASUREMENT_SOURCES = "measurement_sources"

_KIND_LABELS = {
    "total": "Fortlaufender Energiezähler",
    "daily": "Täglich zurückgesetzter Energiezähler",
    "power": "Momentane Leistung (keine Energieintegration)",
}


def _source_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Vorhandene Energie- und Leistungssensoren mit expliziter Messart anbieten."""

    return vol.Schema(
        {
            vol.Required(
                "entity_id", default=defaults.get("entity_id", "")
            ): EntitySelector(
                EntitySelectorConfig(
                    filter={"domain": "sensor", "device_class": ["energy", "power"]}
                )
            ),
            vol.Required("kind", default=defaults.get("kind", "total")): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=key, label=value)
                        for key, value in _KIND_LABELS.items()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required("scope", default=defaults.get("scope", "")): TextSelector(),
            vol.Required(
                "derived_energy", default=defaults.get("derived_energy", False)
            ): BooleanSelector(),
            vol.Required(
                "max_interval_minutes", default=defaults.get("max_interval_minutes", 60)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=1440,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
        }
    )


def _validated_source(
    hass: HomeAssistant,
    values: dict[str, Any],
    source_id: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Metadaten und Identität prüfen; PV-Erzeugung ist danach zu bestätigen."""

    entity_id = str(values.get("entity_id", ""))
    state = hass.states.get(entity_id)
    registry_entry = er.async_get(hass).async_get(entity_id)
    kind = values.get("kind")
    if (
        state is None
        or not entity_id.startswith("sensor.")
        or kind not in _KIND_LABELS
        or (registry_entry is not None and registry_entry.platform == DOMAIN)
    ):
        raise ValueError("invalid_measurement_source")
    is_power = kind == "power"
    if (
        state.attributes.get("device_class") != ("power" if is_power else "energy")
        or state.attributes.get("unit_of_measurement")
        not in ({"W", "kW"} if is_power else {"Wh", "kWh"})
        or state.attributes.get("state_class")
        not in ({"measurement"} if is_power else {"total", "total_increasing"})
    ):
        raise ValueError("invalid_measurement_metadata")
    registry_id = registry_entry.id if registry_entry is not None else None
    if any(
        source["source_id"] != source_id
        and (
            source["entity_id"] == entity_id
            or (registry_id is not None and source.get("registry_id") == registry_id)
        )
        for source in sources
    ):
        raise ValueError("duplicate_measurement_source")
    upstream = source_power_registry_id(
        hass, {"entity_id": entity_id, "registry_id": registry_id}
    )
    if (
        not is_power
        and upstream is not None
        and any(
            source["source_id"] != source_id
            and (source.get("kind") != "power" or PENDING_HELPER in source)
            and (
                source_power_registry_id(hass, source) == upstream
                or source.get("registry_id") == upstream
            )
            for source in sources
        )
    ):
        raise ValueError("duplicate_measurement_source")
    derived = bool(values.get("derived_energy"))
    if registry_entry is not None and registry_entry.platform == "integration":
        derived = True
    try:
        return SourceConfig.from_dict(
            {
                **values,
                "source_id": source_id,
                "entity_id": entity_id,
                "registry_id": registry_id,
                "scope": str(values.get("scope", "")).strip(),
                "derived_energy": derived if not is_power else False,
                "confirmed_pv": True,
                "confirmed_disjoint": True,
            }
        ).to_dict()
    except (ValueError, TypeError) as err:
        raise ValueError("invalid_measurement_source") from err


class MeasurementFlowMixin:
    """Gemeinsame Messquellenschritte für Config Flow und Admin-Options-Flow."""

    hass: HomeAssistant
    _selected_measurement_source: str | None = None
    _pending_measurement_source: dict[str, Any] | None = None
    _measurement_remove: bool = False
    _pending_device: str | None = None
    _measurement_save_error = "measurement_helper_failed"

    async def _async_save_measurement_helpers(self) -> bool:
        """Erst beim Abschluss aus bestätigten Geräteentwürfen native Helfer machen."""
        try:
            sources = await async_resolve_measurement_helpers(
                self.hass, self._measurement_sources()
            )
        except (ValueError, TimeoutError, HomeAssistantError) as err:
            self._measurement_save_error = (
                str(err)
                if str(err)
                in ("duplicate_measurement_source", "measurement_device_unavailable")
                else "measurement_helper_failed"
            )
            return False
        self._measurement_options()[CONF_MEASUREMENT_SOURCES] = sources
        return True

    async def async_step_measurement_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Einen fehlgeschlagenen Abschluss erklären und erneut versuchen lassen."""
        if user_input is not None:
            if self._measurement_entry() is None:
                return await self.async_step_finish()
            return await self.async_step_measurements_done()
        return self.async_show_form(
            step_id="measurement_save",
            data_schema=vol.Schema({}),
            errors={"base": self._measurement_save_error},
        )

    def _measurement_options(self) -> dict[str, Any]:
        """Veränderbare Optionen des jeweiligen Ablaufs bereitstellen."""

        raise NotImplementedError

    def _measurement_entry(self) -> ConfigEntry | None:
        """Beim ersten Setup existiert noch kein Eintrag mit Messdaten."""

        return None

    def _measurement_timezone(self) -> str:
        """Gespeicherte Anlagenzeitzone für die Vorschau bereitstellen."""

        entry = self._measurement_entry()
        return str(entry.data[CONF_TIME_ZONE]) if entry else self.hass.config.time_zone

    def _measurement_sources(self) -> list[dict[str, Any]]:
        """Kopien mit aktuellen Entity-Namen über die Registry-Identität lesen."""

        sources = [
            dict(source)
            for source in self._measurement_options().get(CONF_MEASUREMENT_SOURCES, [])
        ]
        registry = er.async_get(self.hass)
        for source in sources:
            if source.get("registry_id") and (
                registered := registry.async_get(source["registry_id"])
            ):
                source["entity_id"] = registered.entity_id
        return sources

    def _measurement_selection_schema(self) -> vol.Schema:
        """Eine konfigurierte Quelle anhand ihrer Messgrenze auswählen."""

        return vol.Schema(
            {
                vol.Required("source_id"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=source["source_id"],
                                label=f"{source['scope']} · {source['entity_id']}",
                            )
                            for source in self._measurement_sources()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )

    async def async_step_measurements(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionale Quellen und gezielte Verwaltungsaktionen anzeigen."""

        sources = self._measurement_sources()
        menu = ["add_measurement"]
        if sources:
            menu.extend(["edit_measurement", "remove_measurement"])
            if self._measurement_entry() is not None:
                menu.append("delete_measurement_data")
        menu.append("measurements_done")
        automatic = await self._measurement_text("measurement_automatic_energy")
        lines = []
        for source in sources:
            kind = (
                automatic if PENDING_HELPER in source else _KIND_LABELS[source["kind"]]
            )
            lines.append(f"- **{source['scope']}**: {source['entity_id']} · {kind}")
        summary = (
            "\n".join(lines)
            or "Keine Messquelle gewählt. Die Prognose ist vollständig nutzbar."
        )
        return self.async_show_menu(
            step_id="measurements",
            menu_options=menu,
            description_placeholders={"sources": summary},
        )

    async def async_step_add_measurement(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine weitere, zunächst unbestätigte Messquelle auswählen."""

        self._selected_measurement_source = None
        self._pending_device = None
        devices = available_measurement_devices(self.hass)
        errors = {}
        if user_input is not None:
            selected = user_input.get("device")
            if selected == "manual":
                return await self.async_step_measurement_details()
            device = devices.get(selected)
            if device is None:
                errors["base"] = "measurement_device_unavailable"
            elif any(
                source.get("registry_id") == device.registry_id
                or source_power_registry_id(self.hass, source) == device.registry_id
                for source in self._measurement_sources()
            ):
                errors["base"] = "duplicate_measurement_source"
            else:
                self._pending_device = selected
                return await self.async_step_measurement_device()
        return self.async_show_form(
            step_id="add_measurement",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=device.value, label=device.name)
                                for device in devices.values()
                            ]
                            + [
                                SelectOptionDict(
                                    value="manual",
                                    label=await self._measurement_text(
                                        "measurement_manual"
                                    ),
                                )
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def _measurement_text(self, key: str) -> str:
        """Dynamische Bezeichnungen aus den HA-Sprachressourcen lesen."""
        from .config_flow import _async_ui_translations

        return (await _async_ui_translations(self.hass))[f"common.{key}"]

    async def async_step_measurement_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Die fachliche Messgrenze mit wenigen verständlichen Angaben bestätigen."""
        device = available_measurement_devices(self.hass).get(self._pending_device)
        if device is None:
            return await self.async_step_add_measurement(
                {"device": self._pending_device}
            )
        errors = {}
        if user_input is not None:
            if not all(
                user_input.get(key) is True
                for key in (
                    "confirmed_no_battery",
                    "confirmed_pv",
                    "confirmed_disjoint",
                )
            ):
                errors["base"] = "measurement_confirmation_required"
            else:
                sources = self._measurement_sources()
                if any(
                    source.get("registry_id") == device.registry_id
                    or source_power_registry_id(self.hass, source) == device.registry_id
                    for source in sources
                ):
                    errors["base"] = "duplicate_measurement_source"
                else:
                    source = _validated_source(
                        self.hass,
                        {
                            "entity_id": device.entity_id,
                            "kind": device.adapter.kind,
                            "scope": await self._measurement_text(
                                "measurement_whole_plant"
                            ),
                            "max_interval_minutes": 5,
                            "derived_energy": False,
                        },
                        uuid4().hex,
                        sources,
                    )
                    if device.adapter.kind == "power":
                        source[PENDING_HELPER] = device.value
                    self._measurement_options()[CONF_MEASUREMENT_SOURCES] = [
                        *sources,
                        source,
                    ]
                    return await self.async_step_measurements()
        state = self.hass.states.get(device.entity_id)
        return self.async_show_form(
            step_id="measurement_device",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "confirmed_no_battery", default=False
                    ): BooleanSelector(),
                    vol.Required("confirmed_pv", default=False): BooleanSelector(),
                    vol.Required(
                        "confirmed_disjoint", default=False
                    ): BooleanSelector(),
                }
            ),
            description_placeholders={
                "device": device.name,
                "reading": f"{state.state} {state.attributes['unit_of_measurement']}",
            },
        )

    async def async_step_edit_measurement(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Eine bestehende Quelle gezielt bearbeiten."""

        if user_input is not None:
            self._selected_measurement_source = str(user_input["source_id"])
            return await self.async_step_measurement_details()
        return self.async_show_form(
            step_id="edit_measurement", data_schema=self._measurement_selection_schema()
        )

    async def async_step_measurement_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Metadaten validieren und anschließend die konkrete Quelle vorzeigen."""

        sources = self._measurement_sources()
        existing = next(
            (
                source
                for source in sources
                if source["source_id"] == self._selected_measurement_source
            ),
            {},
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                provenance = (
                    {
                        key: existing[key]
                        for key in (
                            "upstream_entity_id",
                            "upstream_registry_id",
                            "helper_entry_id",
                        )
                        if key in existing
                    }
                    if user_input.get("entity_id") == existing.get("entity_id")
                    else {}
                )
                self._pending_measurement_source = _validated_source(
                    self.hass,
                    {**user_input, **provenance},
                    existing.get("source_id", uuid4().hex),
                    sources,
                )
            except ValueError as err:
                errors["base"] = str(err)
            else:
                if (
                    PENDING_HELPER in existing
                    and user_input.get("entity_id") == existing.get("entity_id")
                    and user_input.get("kind") == "power"
                ):
                    self._pending_measurement_source[PENDING_HELPER] = existing[
                        PENDING_HELPER
                    ]
                return await self.async_step_confirm_measurement()
        return self.async_show_form(
            step_id="measurement_details",
            data_schema=_source_schema(user_input or existing),
            errors=errors,
        )

    async def async_step_confirm_measurement(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """AC-PV-Messgrenze und überschneidungsfreie Energiequellen bestätigen."""

        source = self._pending_measurement_source
        assert source is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input.get("confirmed_pv") is not True
                or user_input.get("confirmed_disjoint") is not True
            ):
                errors["base"] = "measurement_confirmation_required"
            else:
                sources = [
                    item
                    for item in self._measurement_sources()
                    if item["source_id"] != source["source_id"]
                ]
                sources.append(source)
                self._measurement_options()[CONF_MEASUREMENT_SOURCES] = sources
                self._pending_measurement_source = None
                return await self.async_step_measurements()
        state = self.hass.states.get(source["entity_id"])
        value, _ = normalize_reading_value(
            state.state if state else None,
            state.attributes.get("unit_of_measurement") if state else None,
            source["kind"],
        )
        unit = "kW" if source["kind"] == "power" else "kWh"
        timestamp = state.last_reported if state and value is not None else None
        entry = self._measurement_entry()
        manager = getattr(getattr(entry, "runtime_data", None), "measurements", None)
        existing = next(
            (
                item
                for item in (
                    entry.options.get(CONF_MEASUREMENT_SOURCES, []) if entry else []
                )
                if item["source_id"] == source["source_id"]
            ),
            {},
        )
        same_source = bool(existing) and (
            SourceConfig.from_dict(existing).measurement_identity
            == SourceConfig.from_dict(source).measurement_identity
        )
        if manager is not None and same_source:
            # Die Erfassung kennt auch ungültige Sprünge und geänderte Metadaten.
            # Eine aktuell numerische HA-Zahl ist allein noch kein gültiger Stand.
            preview = manager.preview(source["source_id"])
            value = preview.get("last_valid_value")
            timestamp = (
                dt_util.parse_datetime(preview["timestamp"])
                if preview.get("timestamp")
                else None
            )
        tz = dt_util.get_time_zone(self._measurement_timezone())
        reading = (
            f"{value:g} {unit}"
            if value is not None
            else "Kein gültiger Messwert vorhanden"
        )
        if timestamp is not None and tz is not None:
            reading += f" ({timestamp.astimezone(tz).isoformat()})"
        return self.async_show_form(
            step_id="confirm_measurement",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("confirmed_pv", default=False): BooleanSelector(),
                    vol.Required(
                        "confirmed_disjoint", default=False
                    ): BooleanSelector(),
                }
            ),
            description_placeholders={
                "entity": state.name if state else source["entity_id"],
                "entity_id": source["entity_id"],
                "unit": (
                    str(state.attributes.get("unit_of_measurement", "—"))
                    if state
                    else "—"
                ),
                "kind": _KIND_LABELS[source["kind"]],
                "scope": source["scope"],
                "reading": reading,
                "derived": (
                    "Ja: vorhandener Helfer; Lücken bleiben unvollständig"
                    if source["derived_energy"]
                    else "Nein"
                ),
            },
        )

    async def async_step_remove_measurement(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Quelle auswählen, deren Konfiguration und Daten entfernt werden."""

        self._measurement_remove = True
        return await self._async_select_measurement_deletion(
            "remove_measurement", user_input
        )

    async def async_step_delete_measurement_data(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nur lokale Daten einer weiterhin konfigurierten Quelle auswählen."""

        self._measurement_remove = False
        return await self._async_select_measurement_deletion(
            "delete_measurement_data", user_input
        )

    async def _async_select_measurement_deletion(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Die Löschung vorab auf genau eine Quelle begrenzen."""

        if user_input is not None:
            self._selected_measurement_source = str(user_input["source_id"])
            return await self.async_step_confirm_measurement_delete()
        return self.async_show_form(
            step_id=step_id, data_schema=self._measurement_selection_schema()
        )

    async def async_step_confirm_measurement_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gezieltes Löschen erst nach ausdrücklicher Bestätigung ausführen."""

        sources = self._measurement_sources()
        source = next(
            item
            for item in sources
            if item["source_id"] == self._selected_measurement_source
        )
        if user_input is not None:
            if user_input.get("confirm_delete") is True:
                if (entry := self._measurement_entry()) is not None:
                    # Der native Options-Endpunkt verlangt Administrationsrechte.
                    from .measurement_runtime import (
                        async_delete_measurement_source_data,
                    )

                    await async_delete_measurement_source_data(
                        self.hass, entry, source["source_id"]
                    )
                if self._measurement_remove:
                    self._measurement_options()[CONF_MEASUREMENT_SOURCES] = [
                        item
                        for item in sources
                        if item["source_id"] != source["source_id"]
                    ]
            return await self.async_step_measurements()
        return self.async_show_form(
            step_id="confirm_measurement_delete",
            data_schema=vol.Schema(
                {vol.Required("confirm_delete", default=False): BooleanSelector()}
            ),
            description_placeholders={
                "source": f"{source['scope']} · {source['entity_id']}",
                "action": (
                    "Die Zuordnung wird beim Abschluss entfernt; "
                    "ihre lokalen Messdaten werden sofort gelöscht."
                    if self._measurement_remove
                    else "Die lokalen Messdaten werden sofort gelöscht. "
                    "Die Quelle bleibt zugeordnet und beginnt ein neues Segment."
                ),
            },
        )
