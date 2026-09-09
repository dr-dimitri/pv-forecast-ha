"""Geführte KSEM-Auswahl und echte native HA-Integral-Helfer ohne Gerätezugriff."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.measurement_adapters import (
    available_measurement_devices,
)
from custom_components.pv_forecast.measurement_helpers import (
    PENDING_HELPER,
    async_resolve_measurement_helpers,
)
from custom_components.pv_forecast.measurement_runtime import MeasurementManager
from custom_components.pv_forecast.measurements import SourceConfig

from .test_measurement_flow import _entry, _options


async def _choose(hass, result, step_id):
    """Menüauswahl ohne automatische manuelle Sensorauswahl."""
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


POWER = {
    "unit_of_measurement": "W",
    "device_class": "power",
    "state_class": "measurement",
}
CONFIRM_DEVICE = {
    "confirmed_no_battery": True,
    "confirmed_pv": True,
    "confirmed_disjoint": True,
}


def ksem(hass, *, serial="1234", unit="W"):
    """Ein vorhandenes KSEM mit namensunabhängigem AC-Register bereitstellen."""
    entry = MockConfigEntry(domain="ksem", title="KSEM Garage")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("ksem", serial)},
        name="KSEM Garage",
    )
    sensor = er.async_get(hass).async_get_or_create(
        "sensor",
        "ksem",
        f"{serial}_obis_40974",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=f"pv_ac_{serial}",
    )
    hass.states.async_set(
        sensor.entity_id,
        1000 if unit == "W" else 1,
        {**POWER, "unit_of_measurement": unit},
    )
    return entry, sensor


def draft(sensor):
    """Bestätigter Entwurf vor dem erstmaligen Speichern."""
    return {
        "source_id": "source-a",
        "entity_id": sensor.entity_id,
        "registry_id": sensor.id,
        "kind": "power",
        "scope": "Gesamte Anlage",
        "derived_energy": False,
        "confirmed_pv": True,
        "confirmed_disjoint": True,
        "max_interval_minutes": 5,
        PENDING_HELPER: f"ksem:{sensor.id}",
    }


async def test_detection_and_rename(hass):
    """Nur installierte, aktive AC-Summenquellen unabhängig vom Namen anbieten."""
    entry, sensor = ksem(hass)
    assert len(available_measurement_devices(hass)) == 1
    renamed = er.async_get(hass).async_update_entity(
        sensor.entity_id, new_entity_id="sensor.umbenannt"
    )
    hass.states.async_set(renamed.entity_id, 1000, POWER)
    assert (
        next(iter(available_measurement_devices(hass).values())).entity_id
        == renamed.entity_id
    )
    for register in (512, 516, 40976, 40988, 49254):
        other = er.async_get(hass).async_get_or_create(
            "sensor", "ksem", f"1234_obis_{register}", config_entry=entry
        )
        hass.states.async_set(other.entity_id, 1000, POWER)
    assert len(available_measurement_devices(hass)) == 1
    hass.states.async_set(renamed.entity_id, "unavailable", POWER)
    assert not available_measurement_devices(hass)
    hass.states.async_set(renamed.entity_id, 1000, POWER)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    assert not available_measurement_devices(hass)


async def test_options_easy_path_and_abort(hass):
    """Auswahl und Bestätigung sind Entwürfe; Abbruch erzeugt keinen Helfer."""
    _, sensor = ksem(hass)
    entry = _entry(hass)
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": f"ksem:{sensor.id}"}
    )
    assert result["step_id"] == "measurement_device"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**CONFIRM_DEVICE, "confirmed_no_battery": False}
    )
    assert result["errors"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], CONFIRM_DEVICE
    )
    assert not hass.config_entries.async_entries("integration")
    hass.config_entries.options.async_abort(result["flow_id"])
    assert not hass.config_entries.async_entries("integration")
    assert entry.options["measurement_sources"] == []


@pytest.mark.parametrize("unit", ["W", "kW"])
async def test_native_helper_energy_and_reuse(hass, unit):
    """Der echte HA-Helfer integriert einmalig in kWh und wird wiederverwendet."""
    start = datetime(2026, 9, 9, 10, tzinfo=UTC)
    with freeze_time(start, real_asyncio=True) as clock:
        _, sensor = ksem(hass, unit=unit)
        sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
        source = SourceConfig.from_dict(sources[0])
        assert source.derived_energy and source.kind == "total"
        assert source.upstream_registry_id == sensor.id
        clock.move_to(start + timedelta(minutes=1))
        hass.states.async_set(
            sensor.entity_id,
            1000 if unit == "W" else 1,
            {**POWER, "unit_of_measurement": unit},
        )
        await hass.async_block_till_done()
        state = hass.states.get(source.entity_id)
        assert state.attributes["unit_of_measurement"] == "kWh"
        assert float(state.state) == pytest.approx(1 / 60, abs=0.0001)
        repeated = await async_resolve_measurement_helpers(hass, [draft(sensor)])
        assert repeated[0]["registry_id"] == source.registry_id
        assert len(hass.config_entries.async_entries("integration")) == 1


async def test_save_options_creates_helper(hass):
    """Der gesamte Options Flow speichert den Helfer statt des Leistungsentwurfs."""
    _, sensor = ksem(hass)
    entry = _entry(hass)
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": f"ksem:{sensor.id}"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], CONFIRM_DEVICE
    )
    result = await _choose(hass, result, "measurements_done")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    source = result["data"]["measurement_sources"][0]
    assert source["kind"] == "total" and source["derived_energy"]
    assert PENDING_HELPER not in source
    assert source["upstream_registry_id"] == sensor.id


async def test_partial_failure_rolls_back_only_new_helpers(hass):
    """Ein zweiter fehlerhafter Entwurf lässt keine verwaisten Helfer zurück."""
    _, sensor = ksem(hass)
    with pytest.raises(ValueError):
        await async_resolve_measurement_helpers(
            hass, [draft(sensor), {**draft(sensor), PENDING_HELPER: "ksem:missing"}]
        )
    assert not hass.config_entries.async_entries("integration")


async def test_upstream_permissions_gap_and_changed_helper(hass):
    """Quellenrechte, lange Ausfälle und geänderte Helfer bleiben erkennbar."""
    start = datetime(2026, 9, 9, 10, tzinfo=UTC)
    with freeze_time(start, real_asyncio=True) as clock:
        _, sensor = ksem(hass)
        sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
        entry = _entry(hass, sources)
        manager = MeasurementManager(hass, entry)
        await manager.async_start()
        try:
            assert set(manager.entity_ids) == {
                sources[0]["entity_id"],
                sensor.entity_id,
            }
            for minute in (1, 2, 20, 21):
                clock.move_to(start + timedelta(minutes=minute))
                hass.states.async_set(sensor.entity_id, 1000, POWER)
                await hass.async_block_till_done()
            result = manager.snapshot(
                start, start + timedelta(minutes=21), start + timedelta(minutes=21)
            )
            assert result["total_energy"]["complete"] is False
            helper = hass.config_entries.async_get_entry(sources[0]["helper_entry_id"])
            with patch(
                "homeassistant.config_entries.ConfigEntries.async_reload",
                return_value=True,
            ):
                hass.config_entries.async_update_entry(
                    helper, options={**helper.options, "source": "sensor.other"}
                )
                await hass.async_block_till_done()
            assert manager.identity_unresolved
        finally:
            await manager.async_stop()


async def test_no_installed_device_offers_manual_path(hass):
    """Ohne Geräteintegration bleibt eine verständliche manuelle Auswahl verfügbar."""
    entry = _entry(hass)
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"] == [
        {"value": "manual", "label": "Anderen Sensor selbst auswählen"}
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": "manual"}
    )
    assert result["step_id"] == "measurement_details"


async def test_initial_config_creates_helper_only_at_finish(hass):
    """Auch beim ersten Setup entsteht der Helfer erst beim Gesamtabschluss."""
    from .test_config_flow import _advance_to_summary

    _, sensor = ksem(hass)
    result = await _advance_to_summary(hass)
    for step in ("measurements", "add_measurement"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": f"ksem:{sensor.id}"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CONFIRM_DEVICE
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "measurements_done"}
    )
    assert not hass.config_entries.async_entries("integration")
    with patch("custom_components.pv_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    assert result["options"]["measurement_sources"][0]["kind"] == "total"
    assert len(hass.config_entries.async_entries("integration")) == 1


async def test_duplicate_device_and_existing_helper_are_rejected(hass):
    """Eine schon zugeordnete Helferquelle darf nicht ein zweites Mal zählen."""
    _, sensor = ksem(hass)
    sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
    # Auch ein vorher manuell konfigurierter Helfer ohne Herkunftszusatz zählt.
    manual = {
        key: value
        for key, value in sources[0].items()
        if key not in ("upstream_registry_id", "upstream_entity_id", "helper_entry_id")
    }
    entry = _entry(hass, [manual])
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": f"ksem:{sensor.id}"}
    )
    assert result["errors"] == {"base": "duplicate_measurement_source"}
    with pytest.raises(ValueError, match="duplicate_measurement_source"):
        await async_resolve_measurement_helpers(hass, [manual, draft(sensor)])
    assert len(hass.config_entries.async_entries("integration")) == 1


async def test_concurrent_helper_creation_is_shared(hass):
    """Zwei gleichzeitig abgeschlossene Auswahlen erzeugen nur einen Helfer."""
    import asyncio

    _, sensor = ksem(hass)
    first, second = await asyncio.gather(
        async_resolve_measurement_helpers(hass, [draft(sensor)]),
        async_resolve_measurement_helpers(hass, [draft(sensor)]),
    )
    assert first[0]["registry_id"] == second[0]["registry_id"]
    assert len(hass.config_entries.async_entries("integration")) == 1


async def test_rename_and_unload_keep_source_identity(hass):
    """Native Helfer folgen Registry-Umbenennungen, der PV-Messpfad beendet Listener."""
    _, sensor = ksem(hass)
    sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
    manager = MeasurementManager(hass, _entry(hass, sources))
    await manager.async_start()
    try:
        registry = er.async_get(hass)
        registry.async_update_entity(
            sensor.entity_id, new_entity_id="sensor.neuer_pv_name"
        )
        hass.states.async_remove(sensor.entity_id)
        hass.states.async_set("sensor.neuer_pv_name", 1000, POWER)
        await hass.async_block_till_done()
        assert "sensor.neuer_pv_name" in manager.entity_ids
        assert not manager.identity_unresolved
    finally:
        await manager.async_stop()
    assert not manager._listeners
    assert len(hass.config_entries.async_entries("integration")) == 1


async def test_device_disappears_before_save_is_retryable(hass):
    """Kein Speichern einer verschwundenen Quelle, Entwurf bleibt erhalten."""
    _, sensor = ksem(hass)
    entry = _entry(hass)
    result = await _choose(hass, await _options(hass, entry), "add_measurement")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": f"ksem:{sensor.id}"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], CONFIRM_DEVICE
    )
    hass.states.async_set(sensor.entity_id, "unavailable", POWER)
    result = await _choose(hass, result, "measurements_done")
    assert result["step_id"] == "measurement_save"
    assert entry.options["measurement_sources"] == []
    assert not hass.config_entries.async_entries("integration")
    hass.states.async_set(sensor.entity_id, 1000, POWER)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_archive_keeps_power_provenance_after_roundtrip(hass):
    """Auch gespeicherte Archive verlangen Leserechte auf die Originalleistung."""
    from custom_components.pv_forecast.history import HistoryArchive

    from .test_history import forecast

    _, sensor = ksem(hass)
    source = SourceConfig.from_dict(
        (await async_resolve_measurement_helpers(hass, [draft(sensor)]))[0]
    )
    archive = HistoryArchive("UTC")
    observed = datetime(2026, 9, 9, 6, tzinfo=UTC)
    archive.capture(forecast(), observed, observed, "configuration-a", [source])
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert {item["registry_id"] for item in restored.external_sources()} == {
        source.registry_id,
        source.upstream_registry_id,
    }


async def test_helper_setup_failure_rolls_back(hass):
    """Fehlgeschlagener nativer Setup-Abschluss hinterlässt keine neue Konfiguration."""
    _, sensor = ksem(hass)
    with patch(
        "custom_components.pv_forecast.measurement_helpers._wait_for_helper",
        side_effect=ValueError("measurement_helper_failed"),
    ):
        with pytest.raises(ValueError, match="measurement_helper_failed"):
            await async_resolve_measurement_helpers(hass, [draft(sensor)])
    assert not hass.config_entries.async_entries("integration")


async def test_source_deletion_blocks_measurement_identity(hass):
    """Ein neuer Sensor unter gleichem Namen übernimmt keine alten Messrechte."""
    _, sensor = ksem(hass)
    sources = await async_resolve_measurement_helpers(hass, [draft(sensor)])
    manager = MeasurementManager(hass, _entry(hass, sources))
    await manager.async_start()
    try:
        er.async_get(hass).async_remove(sensor.entity_id)
        await hass.async_block_till_done()
        assert manager.identity_unresolved
        assert sensor.entity_id in manager.entity_ids
    finally:
        await manager.async_stop()
