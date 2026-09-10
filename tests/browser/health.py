"""Explizite native Browserabnahme des rein lokalen Betriebschecks."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component

from custom_components.pv_forecast.api import OpenMeteoRequestState
from tests.test_measurement_flow import _entry


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_native_health(hass, hass_client, hass_access_token, hass_storage, theme):
    hass_storage["onboarding"] = {
        "version": 4,
        "data": {"done": ["user", "core_config", "integration", "analytics"]},
    }
    hass.config.language = "de"
    _entry(hass)
    with patch(
        "custom_components.pv_forecast.async_setup_entry", AsyncMock(return_value=True)
    ):
        assert await async_setup_component(hass, "frontend", {"frontend": {}})
        assert await async_setup_component(hass, "config", {})
        await hass.async_block_till_done()
        request = OpenMeteoRequestState()
        request._record_temporary_failure("5400")
        hass.data["pv_forecast"] = request
        client = await hass_client()
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/check_health_browser.cjs",
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
                    "theme": theme,
                }
            ).encode()
        )
        await asyncio.to_thread(
            Path(f"/tmp/pv131-native-{theme}.log").write_bytes, output
        )
        assert process.returncode == 0, output.decode()
