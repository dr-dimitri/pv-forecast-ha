"""Explizit gestartete Browserprüfung gegen echte HA-Dialoge mit lokalen Fixtures."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from tests.test_measurement_adapters import ksem
from tests.test_measurement_flow import _entry


@pytest.mark.parametrize("has_device", [False, True])
async def test_native_dialogs(
    hass, hass_client, hass_access_token, hass_storage, has_device
):
    """Nur über expliziten Aufruf; benötigt bereits installiertes Playwright."""
    hass_storage["onboarding"] = {
        "version": 4,
        "data": {"done": ["user", "core_config", "integration", "analytics"]},
    }
    hass.config.language = "de"
    entry = _entry(hass)
    if has_device:
        _, sensor = ksem(hass)
        dr.async_get(hass).async_update_device(
            sensor.device_id,
            name_by_user=(
                "KSEM Garage und Werkstatt am Mehrgenerationenhaus mit langem Namen"
            ),
        )
    with patch(
        "custom_components.pv_forecast.async_setup_entry", AsyncMock(return_value=True)
    ):
        assert await async_setup_component(hass, "frontend", {"frontend": {}})
        assert await async_setup_component(hass, "config", {})
        await hass.async_block_till_done()
        client = await hass_client()
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/check_native_flow_browser.cjs",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        output, _ = await process.communicate(
            json.dumps(
                {
                    "origin": str(client.make_url("/")).rstrip("/"),
                    "token": hass_access_token,
                    "entry": entry.entry_id,
                    "hasDevice": has_device,
                }
            ).encode()
        )
        await asyncio.to_thread(
            Path("/tmp/pv115-native-browser.log").write_bytes, output
        )
        assert process.returncode == 0, output.decode()
