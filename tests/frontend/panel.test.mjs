import test from "node:test";
import assert from "node:assert/strict";
import { PvForecastPanel } from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";

function panelFixture() {
  const configCalls = [];
  const main = { append(card) { card.parentNode = main; } };
  const header = {}, message = {}, menu = { setAttribute(key, value) { this[key] = value; } };
  const card = { setConfig(config) { configCalls.push(config); }, remove() { this.parentNode = null; } };
  const panel = new PvForecastPanel();
  panel.shadowRoot = {
    querySelector(selector) { return selector === "main" ? main : header; },
    getElementById(id) { return id === "message" ? message : menu; },
  };
  return { panel, card, header, message, menu, configCalls };
}

test("Panel reicht genau eine Anlage und den HA-Kontext an dieselbe Karte weiter", () => {
  const f = panelFixture();
  const original = globalThis.document;
  globalThis.document = { createElement(name) { assert.equal(name, "pv-forecast-card"); return f.card; } };
  try {
    f.panel.panel = { title: "PV Zuhause", config: { config_entry_id: "plant-a", menu_label: "Menü öffnen" } };
    const hass = { connection: {} };
    f.panel.hass = hass;
    f.panel.hass = { ...hass, states: {} };
    f.panel.panel = { title: "Neuer Titel", config: { config_entry_id: "plant-a" } };
    assert.deepEqual(f.configCalls, [{ type: "custom:pv-forecast-card", config_entry_id: "plant-a", day: "today" }]);
    assert.equal(f.card.hass.connection, hass.connection);
    assert.equal(f.header.textContent, "Neuer Titel");
    assert.equal(f.message.hidden, true);
    f.panel.panel = { title: "Andere Anlage", config: { config_entry_id: "plant-b" } };
    assert.equal(f.configCalls[1].config_entry_id, "plant-b");
  } finally { globalThis.document = original; }
});

test("Fehlender Anlagenbezug entfernt die alte Karte und zeigt einen klaren Zustand", () => {
  const f = panelFixture();
  f.panel._card = f.card;
  f.panel._entryId = "old";
  f.panel.panel = { title: '<img src=x onerror="alert(1)">', config: {} };
  assert.equal(f.panel._card, null);
  assert.equal(f.card.parentNode, null);
  assert.equal(f.message.hidden, false);
  assert.match(f.message.textContent, /Keine PV-Anlage/);
  assert.equal(f.header.textContent, '<img src=x onerror="alert(1)">');
  assert.equal(f.configCalls.length, 0);
});
