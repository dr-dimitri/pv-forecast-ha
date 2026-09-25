"""Alte Archivbestände entfernen, ohne Messdaten oder Prognosecache anzutasten."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import OpenMeteoConnectionError
from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.retirement import (
    _RETIRED_OPTIONS,
    async_remove_retired_stores,
    async_retire_archive,
)

from .helpers import persisted_roof, weather


async def test_retirement_removes_unknown_stores_without_loading_and_keeps_other_data(
    hass, hass_storage
):
    """Alte Versionen brauchen keine Migration; der Anlagenbezug begrenzt Löschungen."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            **dict.fromkeys(_RETIRED_OPTIONS, "beliebige alte Daten"),
            "measurement_sources": [{"source_id": "bestehend"}],
            "forecast_cache_enabled": True,
            "dashboard_enabled": True,
        },
    )
    entry.add_to_hass(hass)
    for name in ("history", "calibration", "morning", "measurements", "forecast_cache"):
        for plant in (entry.entry_id, "andere-anlage"):
            await Store(hass, 99, f"{DOMAIN}.{name}.{plant}").async_save(
                {"behalten": name}
            )
    before = deepcopy(hass_storage)
    with (
        patch.object(Store, "async_load", side_effect=AssertionError("Kein Einlesen")),
        patch(
            "custom_components.pv_forecast.retirement.persistent_notification.async_dismiss"
        ) as dismiss,
    ):
        await async_retire_archive(hass, entry)
        dismiss.assert_called_once_with(hass, f"{DOMAIN}.observation.{entry.entry_id}")
    assert dict(entry.options) == {
        "measurement_sources": [{"source_id": "bestehend"}],
        "forecast_cache_enabled": True,
        "dashboard_enabled": True,
    }
    removed = {
        f"{DOMAIN}.{name}.{entry.entry_id}"
        for name in ("history", "calibration", "morning")
    }
    assert hass_storage == {
        key: value for key, value in before.items() if key not in removed
    }
    await async_retire_archive(hass, entry)
    assert not removed & hass_storage.keys()


async def test_failed_cleanup_keeps_options_and_can_be_retried(hass, hass_storage):
    """Eine fehlgeschlagene Entfernung wird beim nächsten Setup erneut versucht."""
    entry = MockConfigEntry(domain=DOMAIN, options={"history_enabled": True})
    entry.add_to_hass(hass)
    await Store(hass, 8, f"{DOMAIN}.history.{entry.entry_id}").async_save(
        {"archive": {}}
    )
    with patch.object(
        Store, "async_remove", side_effect=PermissionError("nicht möglich")
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_retire_archive(hass, entry)
    assert entry.options == {"history_enabled": True}
    assert f"{DOMAIN}.history.{entry.entry_id}" in hass_storage
    await async_retire_archive(hass, entry)
    assert not entry.options
    assert f"{DOMAIN}.history.{entry.entry_id}" not in hass_storage


async def test_remove_entry_also_cleans_unloaded_retired_stores(hass, hass_storage):
    """Die Entfernung benötigt keinen vorherigen erfolgreichen Start der Anlage."""
    for name in ("history", "calibration", "morning"):
        await Store(hass, 1, f"{DOMAIN}.{name}.entfernen").async_save({"old": True})
    await async_remove_retired_stores(hass, "entfernen")
    assert not any(key.endswith(".entfernen") for key in hass_storage)


async def test_only_exact_corrupt_copies_and_their_repairs_are_removed(hass, tmp_path):
    """HA-Fehlerkopien anderer Stores und beliebige Backups bleiben unverändert."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.pv_forecast.retirement import _remove_corrupt_copies

    target = tmp_path / "pv_forecast.history.anlage"
    removed = target.with_name(target.name + ".corrupt.2026-09-25T00:00:00+00:00")
    retained = [
        target.with_name(target.name + ".backup"),
        tmp_path / "pv_forecast.history.andere.corrupt.2026-09-25",
        tmp_path / "pv_forecast.measurements.anlage.corrupt.2026-09-25",
    ]
    for path in [removed, *retained]:
        path.write_text("nicht einlesen")
    await hass.async_add_executor_job(_remove_corrupt_copies, str(target))
    assert not removed.exists()
    assert all(path.read_text() == "nicht einlesen" for path in retained)
    ours = "storage_corruption_pv_forecast.history.anlage_2026-09-25"
    other = "storage_corruption_pv_forecast.measurements.anlage_2026-09-25"
    for issue_id in (ours, other):
        ir.async_create_issue(
            hass,
            "homeassistant",
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="storage_corruption",
        )
    await async_remove_retired_stores(hass, "anlage")
    assert ("homeassistant", ours) not in ir.async_get(hass).issues
    assert ("homeassistant", other) in ir.async_get(hass).issues


@pytest.mark.parametrize("weather_available", [True, False])
async def test_real_entry_setup_retires_archive_before_forecast_fetch(
    hass, hass_storage, freezer, weather_available
):
    """Das HA-Setup bereinigt Altbestände auch ohne erreichbaren Wetterdienst."""
    freezer.move_to("2026-09-09T12:00:00+00:00")
    retained = {
        "roofs": [persisted_roof("dach")],
        "inverter_max_power_kw": 8,
        "measurement_sources": [],
        "dashboard_enabled": False,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={"latitude": 52.52, "longitude": 13.41, "time_zone": "UTC"},
        options={
            **retained,
            **dict.fromkeys(_RETIRED_OPTIONS, True),
            "calibration_mode": "auto",
            "morning_mode": "auto",
        },
    )
    entry.add_to_hass(hass)
    retired = {
        f"{DOMAIN}.{name}.{entry.entry_id}"
        for name in ("history", "calibration", "morning")
    }
    for key in retired:
        await Store(hass, 99, key).async_save({"unbekannter_vertrag": True})
    start = datetime(2026, 9, 9, tzinfo=UTC)
    intervals = tuple(
        weather(end=start + timedelta(hours=hour)) for hour in range(1, 49)
    )

    async def fetch_roofs(*args, **kwargs):
        assert dict(entry.options) == retained
        assert not retired & hass_storage.keys()
        if not weather_available:
            raise OpenMeteoConnectionError("Wetterdienst nicht erreichbar")
        return {"dach": intervals}

    with patch(
        "custom_components.pv_forecast.api.OpenMeteoClient.async_fetch_roofs",
        side_effect=fetch_roofs,
    ) as fetch:
        assert (
            await hass.config_entries.async_setup(entry.entry_id) is weather_available
        )
        await hass.async_block_till_done()
        fetch.assert_awaited_once()
        assert dict(entry.options) == retained
        assert not retired & hass_storage.keys()
        assert (entry.version, entry.minor_version) == (1, 1)
        assert set(hass.services.async_services()[DOMAIN]) == {
            "get_forecast",
            "get_measurements",
        }
        if weather_available:
            assert entry.state is ConfigEntryState.LOADED
            runtime = entry.runtime_data
            assert runtime.coordinator.data is not None
            assert runtime.measurements is not None
            assert not any(
                hasattr(runtime, name) for name in ("history", "calibration", "morning")
            )
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            fetch.assert_awaited_once()
        else:
            assert entry.state is ConfigEntryState.SETUP_RETRY
            assert getattr(entry, "runtime_data", None) is None
