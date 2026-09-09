"""Das optionale Kartenmodul ohne Frontendpflicht oder Dashboardänderung ausliefern."""

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.setup import async_setup_component

from custom_components.pv_forecast.frontend import (
    CARD_URL,
    async_setup_frontend,
)


async def test_without_http_keeps_backend_available(hass):
    """Ohne HTTP bleiben Leseaktionen nutzbar; kein Server wird gestartet."""

    assert await async_setup_component(hass, "pv_forecast", {})
    assert hass.http is None
    assert "http" not in hass.config.components
    assert hass.services.has_service("pv_forecast", "get_forecast")
    assert hass.services.has_service("pv_forecast", "get_measurements")


async def test_register_once_when_http_is_already_available(hass):
    """Wiederholtes Setup erzeugt keinen zweiten statischen Pfad."""

    hass.http = Mock(async_register_static_paths=AsyncMock())
    hass.config.components.add("http")
    async_setup_frontend(hass)
    async_setup_frontend(hass)
    await hass.async_block_till_done()
    hass.http.async_register_static_paths.assert_awaited_once()
    [config] = hass.http.async_register_static_paths.call_args.args[0]
    assert config.url_path == CARD_URL
    assert config.path.endswith("/frontend/pv-forecast-card.js")
    assert config.cache_headers is False


async def test_http_started_later_registers_module_once(hass):
    """Die optionale HTTP-Komponente darf nach der Integration eingerichtet werden."""

    async_setup_frontend(hass)
    await hass.async_block_till_done()
    hass.http = Mock(async_register_static_paths=AsyncMock())
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "http"})
    await hass.async_block_till_done()
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "http"})
    await hass.async_block_till_done()
    hass.http.async_register_static_paths.assert_awaited_once()


async def test_module_is_public_but_neighboring_files_are_not(
    hass, hass_client_no_auth, tmp_path
):
    """Nur das statische Modul ist anonym erreichbar; interne Dateien bleiben privat."""

    module = tmp_path / "pv-forecast-card.js"
    module.write_text("export const card = 'pv-forecast-card';", encoding="utf-8")
    (tmp_path / "private.json").write_text('{"secret":true}', encoding="utf-8")
    assert await async_setup_component(hass, "http", {"http": {}})
    with patch("custom_components.pv_forecast.frontend.CARD_FILE", module):
        async_setup_frontend(hass)
        await hass.async_block_till_done()
    client = await hass_client_no_auth()
    response = await client.get(f"{CARD_URL}?v=1")
    assert response.status == 200
    assert await response.text() == module.read_text(encoding="utf-8")
    assert "javascript" in response.headers["Content-Type"]
    assert (await client.get("/pv_forecast/private.json")).status == 404
    assert (await client.get(f"{CARD_URL}/../private.json")).status == 404
