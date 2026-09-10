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

test("Tageswechsel verwirft abhängige Werte beim Nachladen, Ausfall und wiederholten Ausfall", async () => {
  let state = await load("experience");
  const old = state;
  const base = fixtureHass("experience");
  const nextDayHass = {
    async callWS(message) {
      const result = await base.callWS(message);
      if (message.service === "get_forecast") {
        Object.assign(result.response.view, {
          date: "2026-09-11", start: "2026-09-10T22:00:00Z", end: "2026-09-11T22:00:00Z",
          today_start: "2026-09-10T22:00:00Z", today_end: "2026-09-11T22:00:00Z", as_of: "2026-09-10T22:01:00Z",
        });
      }
      return result;
    },
  };
  const assertNoYesterday = () => {
    assert.equal(state.measurement.data, undefined);
    assert.equal(state.history.data, undefined);
    const html = renderContent(config, state);
    assert.doesNotMatch(html, /Seit Tagesbeginn|17,2–28,4|Heute voraussichtlich insgesamt/);
  };
  for (let attempt = 0; attempt < 2; attempt++) {
    const ready = Promise.withResolvers(), release = Promise.withResolvers();
    let pending = 0;
    const run = loadView({ async callWS(message) {
      if (message.service === "get_forecast") return nextDayHass.callWS(message);
      if (++pending === 2) ready.resolve();
      await release.promise;
      throw { code: "home_assistant_error" };
    } }, config, (incoming) => { state = retainReadState(state, incoming); });
    await ready.promise;
    assertNoYesterday();
    release.resolve();
    await run;
    assertNoYesterday();
  }
  await loadView({ async callWS(message) {
    const result = await nextDayHass.callWS(message);
    if (message.service === "get_measurements") {
      result.response.total_energy.energy_kwh = 0.25;
      Object.assign(result.response.outlook, { as_of: "2026-09-10T22:01:00Z", total_kwh: 19.5 });
    }
    if (message.service === "get_history") {
      Object.assign(result.response.uncertainty.days.today, { target_date: "2026-09-11", lower_kwh: 10 });
    }
    return result;
  } }, config, (incoming) => { state = retainReadState(state, incoming); });
  assert.equal(state.measurement.data.total_energy.energy_kwh, 0.25);
  assert.match(renderContent(config, state), /Seit Tagesbeginn/);
  assert.match(renderContent(config, state), /19,5 kWh/);
  assert.match(renderContent(config, state), /10–28,4/);
  assert.equal(old.measurement.data.total_energy.energy_kwh, 12.4, "Der ursprüngliche Stand wird nicht verändert");
});

test("Tagesbasis nutzt UTC-Grenzen und Anlagenzeitzone, unabhängig von Ansicht und DST", async () => {
  for (const scenario of ["sunny", "spring", "fold", "kolkata"]) {
    const previous = await load(scenario);
    const view = previous.forecast.data;
    for (const change of [
      { today_start: view.today_end, today_end: new Date(Date.parse(view.today_end) + 86400000).toISOString() },
      { timezone: "UTC" },
    ]) {
      const next = { forecast: { status: "ready", data: { ...view, ...change } }, measurement: { status: "loading" }, history: { status: "loading" } };
      const state = retainReadState(previous, next);
      assert.equal(state.measurement.data, undefined, scenario);
      assert.equal(state.history.data, undefined, scenario);
    }
    const sameDay = retainReadState(previous, {
      forecast: { status: "ready", data: { ...view, day: "tomorrow", as_of: new Date(Date.parse(view.as_of) + 60000).toISOString() } },
      measurement: sourceError({}, "Messdaten"), history: sourceError({}, "Archivdaten"),
    });
    assert.equal(sameDay.measurement.data, previous.measurement.data);
    assert.equal(sameDay.history.data, previous.history.data);
    assert.equal(sameDay.measurement.retained, true);
  }
});

test("Späte Tagesantworten werden auch bei erfolgreichem Lesen nicht falsch zugeordnet", async () => {
  const state = await load("experience");
  state.measurement.data.outlook.as_of = state.forecast.data.today_end;
  state.history.data.uncertainty.days.today.target_date = "2026-09-11";
  let html = renderContent(config, state);
  assert.doesNotMatch(html, /Heute voraussichtlich insgesamt|17,2–28,4/);
  state.measurement.data.outlook.as_of = state.forecast.data.as_of;
  state.history.data.uncertainty.days.today.target_date = state.forecast.data.date;
  state.measurement.data.outlook.timezone = "UTC";
  state.history.data.uncertainty.timezone = "UTC";
  html = renderContent(config, state);
  assert.doesNotMatch(html, /Heute voraussichtlich insgesamt|17,2–28,4/);
});
