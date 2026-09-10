import test from "node:test";
import assert from "node:assert/strict";
import { intervalAtPosition, intervalDetails, intervalKey, loadView, renderContent, renderIntervalDetails, tableRows } from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
import { fixtureHass } from "./fixtures.mjs";

async function load(scenario) {
  let state;
  await loadView(fixtureHass(scenario), { config_entry_id: "demo-plant", day: "today" }, value => { state = value; });
  return state;
}

test("Intervallauswahl verwendet UTC-Grenzen und beide unterschiedlichen Herbststunden", async () => {
  const state = await load("fold"), rows = tableRows(state);
  assert.equal(rows.length, 25);
  const earlier = rows[2], later = rows[3];
  assert.notEqual(intervalKey(earlier), intervalKey(later));
  assert.match(renderIntervalDetails(state, intervalKey(earlier)), /02:00 UTC\+02:00/);
  assert.match(renderIntervalDetails(state, intervalKey(later)), /02:00 UTC\+01:00/);
  assert.equal(intervalKey(intervalAtPosition(state, 3 / 25)), intervalKey(later));
  assert.equal(intervalKey(intervalAtPosition(state, 1)), intervalKey(rows.at(-1)));
  assert.equal(intervalAtPosition(state, -0.1), null);
  assert.equal(intervalAtPosition(state, NaN), null);
});

test("Teilstunden bleiben eigenständige auswählbare Intervalle", async () => {
  const state = await load("kolkata"), rows = tableRows(state);
  assert.equal(Date.parse(rows[0].end) - Date.parse(rows[0].start), 1800000);
  assert.equal(Date.parse(rows.at(-1).end) - Date.parse(rows.at(-1).start), 1800000);
  assert.equal(intervalKey(intervalAtPosition(state, 0)), intervalKey(rows[0]));
  assert.equal(intervalKey(intervalAtPosition(state, 1)), intervalKey(rows.at(-1)));
});

test("Details unterscheiden Nullwerte, unvollständige und fehlende Reihen", async () => {
  const state = await load("gaps"), rows = tableRows(state);
  const zero = intervalDetails(state, intervalKey(rows[0]));
  assert.equal(zero.sources.actual.status, "complete");
  assert.equal(zero.sources.actual.energy_kwh, 0);
  const gap = intervalDetails(state, intervalKey(rows[10]));
  assert.equal(gap.sources.forecast.status, "incomplete");
  assert.equal(gap.sources.actual.status, "incomplete");
  assert.equal(gap.sources.history.status, "missing");
  assert.match(renderIntervalDetails(state, intervalKey(rows[10])), /unvollständig/);
  assert.match(renderIntervalDetails(state, intervalKey(rows[10])), /nicht vorhanden/);
  assert.equal(intervalDetails(state, "entfernt"), null);
});

test("Ausgewähltes Intervall liest neue Werte desselben UTC-Schlüssels ohne eigene Berechnung", async () => {
  const state = await load("sunny"), key = intervalKey(tableRows(state)[8]);
  state.forecast.data.intervals[8].energy_kwh = 1.2345;
  assert.equal(intervalDetails(state, key).sources.forecast.energy_kwh, 1.2345);
  state.forecast.data.intervals[8].is_complete = false;
  assert.equal(intervalDetails(state, key).sources.forecast.energy_kwh, null);
});


test("Vorhandene Ist-Mengen bilden eine gemeinsame Kurve ohne Erfassungshinweise", async () => {
  const state = await load("offset-measurements"), rows = tableRows(state);
  const key = intervalKey(rows[8]);
  const detail = intervalDetails(state, key);
  assert.equal(detail.sources.actual.status, "incomplete");
  assert.equal(detail.sources.actual.energy_kwh, null);
  assert.equal(detail.sources.actual.observed_energy_kwh, 0.81);
  assert.equal(rows[8].actual, null);
  assert.equal(rows[8].actual_observed, 0.81);
  const html = renderContent({ config_entry_id: "demo-plant", day: "today" }, state);
  assert.equal([...html.matchAll(/class="actual-line"/g)].length, 1);
  assert.match(html, /<td>0,81<\/td>/);
  assert.doesNotMatch(html, /unvollständig erfasst|Unvollständig erfasst|Teilweise erfasst|nur belegte Teilmengen|actual-partial|actual-bar/);
  assert.match(html, /Tatsächlich produziert/);
  assert.doesNotMatch(html, /class="history-line"/);
  assert.doesNotMatch(html, /class="history-key"/);
  // Unsichtbare Archivwerte dürfen die Diagrammachse nicht mehr verändern.
  const chart = html.match(/<svg id="interval-chart"[\s\S]*?<\/svg>/)[0];
  for (const item of state.history.data.current_targets.intervals) item.energy_kwh = 10000;
  assert.equal(renderContent({}, state).match(/<svg id="interval-chart"[\s\S]*?<\/svg>/)[0], chart);
  assert.match(renderIntervalDetails(state, key), /<dt>Messung<\/dt><dd>0,81 kWh<\/dd>/);
  // Alte Backendantworten und unvollständige Werte ohne Teilsumme bleiben leer.
  for (const item of state.measurement.data.total_intervals) delete item.observed_energy_kwh;
  assert.match(renderIntervalDetails(state, key), /<dt>Messung<\/dt><dd>—<\/dd>/);
  assert.notEqual(renderContent({}, state).match(/<svg id="interval-chart"[\s\S]*?<\/svg>/)[0], chart);
});
