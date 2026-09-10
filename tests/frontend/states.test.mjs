import test from "node:test";
import assert from "node:assert/strict";
import { dataNotices, loadView, retainReadState, sourceError, renderContent } from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
import { fixtureHass } from "./fixtures.mjs";
const config = { config_entry_id: "demo-plant", day: "today" };
async function load(scenario) { let last; await loadView(fixtureHass(scenario), config, (state) => { last = state; }); return last; }
test("Zustände unterscheiden fehlende Quellen, Messbeginn, Null und Archiv", async () => {
  for (const [scenario, id] of [["no-source", "no-source"], ["no-measurement", "measurement-incomplete"], ["gaps", "measurement-incomplete"], ["archive-off", "archive-off"], ["archive-empty", "archive-empty"], ["stale", "stale"], ["old", "version"]]) {
    assert.ok(dataNotices(await load(scenario)).some((item) => item.id === id), scenario);
  }
  assert.equal(dataNotices(await load("zero")).some((item) => /measurement|no-source/.test(item.id)), false);
  assert.equal(dataNotices(await load("acl")).filter((item) => item.id === "permission").length, 1);
  assert.equal(dataNotices(null)[0].id, "forecast-loading");
});
test("Vorübergehende Ausfälle bewahren Zeitbasis, Rechteentzug und neue Versionen löschen Werte", async () => {
  const previous = await load("sunny");
  const error = sourceError({ code: "home_assistant_error" }, "Prognosedaten");
  const retained = retainReadState(previous, { forecast: error, measurement: { status: "idle" }, history: { status: "idle" } });
  assert.equal(retained.forecast.data, previous.forecast.data);
  assert.equal(retained.forecast.envelope.fetched_at, previous.forecast.envelope.fetched_at);
  assert.match(renderContent(config, retained), /Letzte gelesene Ansicht/);
  for (const reason of ["permission", "version", "roof_removed"]) {
    const result = retainReadState(previous, { forecast: { status: "error", reason } });
    assert.equal(result.forecast.data, undefined);
    assert.equal(result.measurement, undefined);
  }
  const denied = retainReadState(previous, { forecast: previous.forecast, measurement: sourceError({ code: "unauthorized" }, "Messdaten") });
  assert.equal(denied.measurement.data, undefined);
});
