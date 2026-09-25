import test from "node:test";
import assert from "node:assert/strict";
import { loadView, renderContent, renderExplanation, currentExplanation, tableRows, intervalKey, SharedReadCache } from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
import { fixtureHass } from "./fixtures.mjs";
const config = { config_entry_id: "demo-plant", day: "today" };
async function load(options = {}) {
  let state; const calls = [];
  await loadView(fixtureHass("sunny", { calls }), { ...config, ...options }, value => state = value);
  return { state, calls };
}

test("Geschlossene Erklärung fordert keine Zusatzdaten, aktive Ansicht nur vorhandenen Lesezyklus", async () => {
  const closed = await load(), opened = await load({ include_explanation: true });
  assert.equal(closed.calls[0].service_data.include_explanation, undefined);
  assert.equal(opened.calls[0].service_data.include_explanation, true);
  assert.equal(opened.calls.length, closed.calls.length);
  assert.equal(currentExplanation(opened.state).totals.before_clipping_kwh, 30);
  const html = renderContent(config, opened.state);
  assert.match(html, /Prognose erklärt/);
  assert.match(html, /Basis vor AC-Begrenzung/);
  assert.match(html, /Zusätzliche Kürzung durch Anlagenlimit/);
  assert.match(html, /keine gemessenen Geräteverluste/);
  assert.doesNotMatch(html, /Faktor|Selbstkalibrierung|raw-toggle/);
});

test("Erklärung bleibt derselben Tagesgeneration zugeordnet, heutige Messbasis bei Morgen unverändert", async () => {
  const { state, calls } = await load({ day: "tomorrow", include_explanation: true });
  assert.equal(calls[0].service_data.day, "tomorrow");
  assert.equal(currentExplanation(state).start, state.forecast.data.start);
  assert.equal(calls[1].service_data.start, state.forecast.data.today_start);
  assert.equal(calls[1].service_data.interval_windows[0].start, state.forecast.data.today_start);
  state.forecast.envelope.explanation.start = "2020-01-01T00:00:00Z";
  assert.equal(currentExplanation(state), null);
});

test("Mehrere Karten teilen identische Erklärung einschließlich Tag", async () => {
  const cache = new SharedReadCache(), calls = [], hass = fixtureHass("sunny", { calls });
  const active = { ...config, include_explanation: true };
  await Promise.all([loadView(hass, active, () => {}, () => true, cache), loadView(hass, active, () => {}, () => true, cache)]);
  assert.equal(calls.filter(c => c.service === "get_forecast").length, 1);
});

test("Dachansicht hat keine Gesamtbilanz", async () => {
  const { state } = await load({ roof_id: "south", include_explanation: true });
  assert.equal(renderExplanation(state), "");
});

for (const mode of ["available", "roof", "missing", "unavailable", "wrong_day", "wrong_version"]) {
  test(`Intervallauswahl bleibt unabhängig von der Erklärung bedienbar: ${mode}`, async () => {
    const { state } = await load(mode === "roof" ? { roof_id: "south" } : { include_explanation: true });
    if (mode === "missing") delete state.forecast.envelope.explanation;
    if (mode === "unavailable") state.forecast.envelope.explanation.status = "unavailable";
    if (mode === "wrong_day") state.forecast.envelope.explanation.date = "2020-01-01";
    if (mode === "wrong_version") state.forecast.envelope.explanation.schema_version = 1;
    const key = intervalKey(tableRows(state)[0]);
    const html = renderContent(config, state, 360, {}, key);
    const details = html.match(/<section id="interval-detail"[\s\S]*?<\/section>/)[0];
    assert.match(details, /Aktuelle Prognose/);
    assert.match(details, /id="interval-next"/);
    if (mode === "available") assert.match(html, /Ausgewähltes Intervall ·/);
    if (mode === "roof") assert.doesNotMatch(html, /Prognose erklärt/);
  });
}
