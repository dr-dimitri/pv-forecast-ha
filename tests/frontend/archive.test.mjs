import test from "node:test";
import assert from "node:assert/strict";
import { SharedReadCache, PvForecastCard, readArchiveDay, renderArchiveDay, historicalState, tableRows, shiftArchiveDate } from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
import { archiveFixture, fixtureHass } from "./fixtures.mjs";
const selection = { date: "2026-08-10", horizon: "hourly_1h" };

test("Archivnavigation teilt identische Aufrufe, trennt Auswahl und legt keinen Timer an", async () => {
  let timers = 0;
  const cache = new SharedReadCache({setTimer() { timers++; }}), calls = [], hass = fixtureHass("sunny", {calls, delay: 1});
  await Promise.all([readArchiveDay(hass, selection, "demo-plant", cache), readArchiveDay(hass, selection, "demo-plant", cache)]);
  assert.equal(calls.length, 1);
  await readArchiveDay(hass, selection, "demo-plant", cache);
  assert.equal(calls.length, 1);
  await readArchiveDay(hass, {...selection, horizon: "hourly_3h"}, "demo-plant", cache);
  await readArchiveDay(hass, {...selection, configuration_id: "synthetic-old"}, "demo-plant", cache);
  await readArchiveDay(hass, {...selection, date: "2026-08-09"}, "demo-plant", cache);
  await readArchiveDay(hass, selection, "demo-plant", cache, true);
  assert.equal(calls.length, 5); assert.equal(timers, 0);
});

test("Späte Archivantworten überschreiben weder neue Tage noch versteckte Karten", async () => {
  const pending = [];
  const card = new PvForecastCard();
  Object.assign(card, {_connected: true, _visible: true, _archiveOpen: true, _config: {config_entry_id: "demo-plant"}, _archiveSelection: {...selection}, _render() {}, _hass: {connection: {}, callWS(message) { return new Promise(resolve => pending.push(() => resolve({response: {schema_version: 1, day_view: archiveFixture("sunny", message.service_data.day_view)}}))); }}});
  const first = card._loadArchiveDay();
  card._archiveSelection.date = "2026-08-09";
  const second = card._loadArchiveDay();
  await Promise.resolve();
  pending[1](); await second; pending[0](); await first;
  assert.equal(card._archiveDay.data.date, "2026-08-09");
  card._archiveSelection.date = "2026-08-08";
  const hidden = card._loadArchiveDay(); await Promise.resolve(); card._visible = false;
  pending[2](); await hidden;
  assert.equal(card._archiveDay.status, "loading");
  await card._loadArchiveDay(); assert.equal(pending.length, 3);
});

for (const scenario of ["sunny", "gaps", "spring", "fold", "kolkata"]) test(`${scenario}: Historie verwendet UTC-Grafik und getrennte Tagesmessung`, () => {
  const data = archiveFixture(scenario, selection), rows = tableRows(historicalState(data));
  assert.equal(rows.length, data.intervals.length);
  assert.ok(rows.every(row => row.forecast === undefined));
  const html = renderArchiveDay({selection, result: {status: "ready", data}}, 360);
  assert.match(html, /id="archive-interval-chart"/);
  assert.match(html, /class="history-line"/);
  assert.doesNotMatch(html, /id="chart"/);
  assert.match(html, /18 Uhr am Vortag/); assert.match(html, /Vollständig belegte Tagesmessung/);
  assert.doesNotMatch(html, /Aktuelle Prognose/);
});

test("Datumsnavigation rechnet Kalendertage unabhängig von Sommerzeit", () => {
  assert.equal(shiftArchiveDate("2026-03-29", -1), "2026-03-28");
  assert.equal(shiftArchiveDate("2026-10-25", 1), "2026-10-26");
});
