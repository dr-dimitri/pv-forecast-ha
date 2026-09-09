"""Geräteprofile für die geführte Zuordnung bereits installierter HA-Sensoren."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .measurements import MeasurementKind, normalize_reading_value


@dataclass(frozen=True, slots=True)
class MeasurementAdapter:
    """Ein Profil beschreibt die stabile Erkennung und die gelieferte Messart."""

    key: str
    name: str
    integration: str
    unique_id_suffix: str
    kind: MeasurementKind = "power"


# Weitere Hersteller ergänzen hier ein geprüftes Profil. Anzeigenamen und
# veränderliche Entity-IDs dienen ausdrücklich nicht zur Sensorerkennung.
MEASUREMENT_ADAPTERS = (
    MeasurementAdapter("ksem", "KOSTAL KSEM", "ksem", "_obis_40974"),
)


@dataclass(frozen=True, slots=True)
class MeasurementDevice:
    """Konkrete, aktuell nutzbare Quelle eines installierten Geräteprofils."""

    adapter: MeasurementAdapter
    registry_id: str
    entity_id: str
    name: str

    @property
    def value(self) -> str:
        return f"{self.adapter.key}:{self.registry_id}"


def available_measurement_devices(hass: HomeAssistant) -> dict[str, MeasurementDevice]:
    """Nur aktive, eindeutige Registry-Quellen mit passenden Live-Metadaten anbieten."""

    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    result = {}
    for registered in registry.entities.values():
        if registered.disabled or registered.domain != "sensor":
            continue
        entry = hass.config_entries.async_get_entry(registered.config_entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            continue
        state = hass.states.get(registered.entity_id)
        if state is None or state.attributes.get("restored"):
            continue
        for adapter in MEASUREMENT_ADAPTERS:
            power = adapter.kind == "power"
            if (
                registered.platform != adapter.integration
                or entry.domain != adapter.integration
                or not registered.unique_id.endswith(adapter.unique_id_suffix)
                or state.attributes.get("device_class")
                != ("power" if power else "energy")
                or state.attributes.get("state_class")
                not in ({"measurement"} if power else {"total", "total_increasing"})
                or normalize_reading_value(
                    state.state,
                    state.attributes.get("unit_of_measurement"),
                    adapter.kind,
                )[0]
                is None
            ):
                continue
            device = (
                devices.async_get(registered.device_id)
                if registered.device_id
                else None
            )
            name = (device.name_by_user or device.name) if device else entry.title
            candidate = MeasurementDevice(
                adapter,
                registered.id,
                registered.entity_id,
                f"{adapter.name} · {name or entry.title}",
            )
            result[candidate.value] = candidate
    return result
