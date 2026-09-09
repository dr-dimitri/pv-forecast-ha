import test from "node:test";
import assert from "node:assert/strict";
import { intervalAtPosition, intervalDetails, intervalKey, loadView, renderIntervalDetails, tableRows } from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
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
