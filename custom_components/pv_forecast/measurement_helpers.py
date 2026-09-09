"""Native HA-Energiehelfer beim bewussten Abschluss der Geräteauswahl einrichten."""

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .measurements import SourceConfig

PENDING_HELPER = "pending_energy_helper"


def helper_matches(hass: HomeAssistant, entry: ConfigEntry, registry_id: str) -> bool:
    """Nur unveränderte, ereignisbasierte Integral-Helfer derselben Quelle verwenden."""

    registered = er.async_get(hass).async_get(registry_id)
    if registered is None or entry.domain != "integration":
        return False
    options = entry.options
    state = hass.states.get(registered.entity_id)
    if state is None:
        return False
    prefix = "k" if state.attributes.get("unit_of_measurement") == "W" else None
    return (
        options.get("source") in (registered.id, registered.entity_id)
        and options.get("method") == "trapezoidal"
        and options.get("unit_time") == "h"
        and options.get("unit_prefix")
        in ((None, "none") if prefix is None else (prefix,))
        and not any((options.get("max_sub_interval") or {}).values())
    )


async def _wait_for_helper(hass: HomeAssistant, entry: ConfigEntry) -> er.RegistryEntry:
    """Den nativen asynchronen Setup-Abschluss ohne Polling abwarten."""

    done = asyncio.Event()

    @callback
    def changed() -> None:
        if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
            done.set()

    cancel = entry.async_on_state_change(changed)
    try:
        if entry.state is not ConfigEntryState.LOADED:
            async with asyncio.timeout(20):
                await done.wait()
        if entry.state is not ConfigEntryState.LOADED:
            raise ValueError("measurement_helper_failed")
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "sensor", "integration", entry.entry_id
        )
        registered = registry.async_get(entity_id) if entity_id else None
        if registered is None or registered.disabled:
            raise ValueError("measurement_helper_failed")
        return registered
    finally:
        cancel()


async def async_resolve_measurement_helpers(
    hass: HomeAssistant,
    sources: list[dict[str, Any]],
    *,
    check_current: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Gleichzeitige Abschlüsse dürfen keine doppelten Helfer anlegen."""
    lock = hass.data.setdefault(f"{DOMAIN}_measurement_helper_lock", asyncio.Lock())
    async with lock:
        return await _async_resolve_helpers(hass, sources, check_current)


async def _async_resolve_helpers(
    hass: HomeAssistant,
    sources: list[dict[str, Any]],
    check_current: Callable[[], None] | None,
) -> list[dict[str, Any]]:
    """Entwürfe auflösen und bei Fehlern nur neu erzeugte Helfer zurückrollen."""

    from .measurement_adapters import available_measurement_devices

    created: list[str] = []
    resolved = []
    try:
        if check_current is not None:
            check_current()
        for source in sources:
            if PENDING_HELPER not in source:
                resolved.append(dict(source))
                continue
            device = available_measurement_devices(hass).get(source[PENDING_HELPER])
            if device is None or device.registry_id != source["registry_id"]:
                raise ValueError("measurement_device_unavailable")
            helper = next(
                (
                    entry
                    for entry in hass.config_entries.async_entries("integration")
                    if entry.state is ConfigEntryState.LOADED
                    and helper_matches(hass, entry, device.registry_id)
                    and er.async_get(hass).async_get_entity_id(
                        "sensor", "integration", entry.entry_id
                    )
                ),
                None,
            )
            if helper is None:
                state = hass.states.get(device.entity_id)
                translations = await async_get_translations(
                    hass, "de", "common", integrations={DOMAIN}
                )
                if check_current is not None:
                    check_current()
                options = {
                    "name": translations[
                        f"component.{DOMAIN}.common.measurement_helper_name"
                    ].format(device=device.name),
                    "source": device.registry_id,
                    "method": "trapezoidal",
                    "unit_time": "h",
                    "round": 4,
                }
                if state.attributes["unit_of_measurement"] == "W":
                    options["unit_prefix"] = "k"
                result = await hass.config_entries.flow.async_init(
                    "integration", context={"source": SOURCE_USER}, data=options
                )
                if result["type"] is not FlowResultType.CREATE_ENTRY:
                    if result.get("flow_id"):
                        hass.config_entries.flow.async_abort(result["flow_id"])
                    raise ValueError("measurement_helper_failed")
                helper = result["result"]
                created.append(helper.entry_id)
            registered = await _wait_for_helper(hass, helper)
            if check_current is not None:
                check_current()
            if any(
                item.get("registry_id") == registered.id for item in resolved
            ) or any(
                item.get("registry_id") == registered.id
                for item in sources
                if PENDING_HELPER not in item
            ):
                raise ValueError("duplicate_measurement_source")
            resolved.append(
                SourceConfig.from_dict(
                    {
                        **source,
                        "entity_id": registered.entity_id,
                        "registry_id": registered.id,
                        "kind": "total",
                        "derived_energy": True,
                        "upstream_entity_id": device.entity_id,
                        "upstream_registry_id": device.registry_id,
                        "helper_entry_id": helper.entry_id,
                    }
                ).to_dict()
            )
        return resolved
    except BaseException:
        for entry_id in reversed(created):
            await hass.config_entries.async_remove(entry_id)
        raise


def source_power_registry_id(hass: HomeAssistant, source: dict[str, Any]) -> str | None:
    """Auch manuell zugeordnete Integral-Helfer derselben Leistung erkennen."""

    if source.get("upstream_registry_id"):
        return source["upstream_registry_id"]
    registry = er.async_get(hass)
    registered = registry.async_get(source.get("registry_id") or source["entity_id"])
    if registered is None or registered.platform != "integration":
        return None
    entry = hass.config_entries.async_get_entry(registered.config_entry_id)
    if entry is None or entry.domain != "integration":
        return None
    reference = entry.options.get("source")
    upstream = registry.async_get(reference) if isinstance(reference, str) else None
    return upstream.id if upstream else None
