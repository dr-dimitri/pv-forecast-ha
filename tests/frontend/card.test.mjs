import test from "node:test";
import assert from "node:assert/strict";
import {
  ARCHIVE_LABEL, REFRESH_MS, PvForecastCard, SharedReadCache, connectionCache, energyText,
  formatPlantTime, loadView, planningChoices, plotGeometry, renderContent, renderOutlook, renderPlanning, renderReport, renderUncertainty, renderUnderperformance,
  selectedSeries, seriesPaths, tableRows, validateView,
} from "../../custom_components/pv_forecast/frontend/pv-forecast-card.js";
import { fixture, fixtureHass } from "./fixtures.mjs";

const config = { config_entry_id: "demo-plant", day: "today" };

test("Ist-Kennzahl verwendet nach Standortwechsel nur aktuelle Messanteile", async () => {
  const { state } = await load();
  state.measurement.data.total_energy = { energy_kwh: 99, energy_complete: true };
  state.measurement.data.current_location_total_energy = { energy_kwh: 2, energy_complete: false };
  const html = renderContent(config, state);
  assert.match(html, /Ist heute<\/dt><dd>2 <small>kWh<\/small>/);
  assert.match(html, /Unvollständig erfasst/);
  assert.doesNotMatch(html, /Ist heute<\/dt><dd>99 /);
});
const flush = async () => { for (let index = 0; index < 30; index++) await Promise.resolve(); };
async function load(scenario = "sunny", options = {}) {
  const calls = [], states = [];
  await loadView(fixtureHass(scenario, { calls }), { ...config, ...options }, (state) => states.push(state));
  return { calls, states, state: states.at(-1) };
}
function clockCache() {
  let time = 0, nextId = 1;
  const timers = new Map(), listeners = new Set();
  const document = { hidden: false, addEventListener(name, handler) { assert.equal(name, "visibilitychange"); listeners.add(handler); }, removeEventListener(name, handler) { listeners.delete(handler); } };
  const cache = new SharedReadCache({ document, now: () => time, setTimer: (handler, delay) => { const id = nextId++; timers.set(id, { handler, at: time + delay }); return id; }, clearTimer: (id) => timers.delete(id) });
  return { cache, timers, listeners, document, async advance(milliseconds) { time += milliseconds; for (const [id, timer] of [...timers]) if (timer.at <= time) { timers.delete(id); timer.handler(); } await flush(); }, async visible(visible) { document.hidden = !visible; for (const listener of listeners) listener(); await flush(); } };
}

test("Visueller Editor nutzt HA-Anlagenauswahl; Stub braucht keine Admin-Abfrage", () => {
  const form = PvForecastCard.getConfigForm();
  assert.deepEqual(form.schema[0].selector, { config_entry: { integration: "pv_forecast" } });
  assert.equal(form.computeLabel(form.schema[0]), "PV-Anlage");
  assert.deepEqual(PvForecastCard.getStubConfig(fixtureHass()), config);
  assert.equal(PvForecastCard.getStubConfig({ entities: {} }).config_entry_id, "");
  assert.equal(new PvForecastCard().getGridOptions().columns, 12);
  assert.throws(() => new PvForecastCard().setConfig({}), /PV-Anlage/);
});

test("Ein Lesezyklus verwendet nur lesende HA-Aktionen und unveränderte Backendwerte", async () => {
  const { calls, states, state } = await load();
  assert.deepEqual(calls.map((item) => item.service), ["get_forecast", "get_measurements", "get_history"]);
  for (const call of calls) { assert.equal(call.type, "call_service"); assert.equal(call.return_response, true); assert.equal(call.domain, "pv_forecast"); }
  assert.equal(calls[0].service_data.include_view, true);
  const view = state.forecast.data;
  assert.equal(calls[1].service_data.start, view.today_start);
  assert.equal(calls[1].service_data.end, view.as_of);
  assert.equal(calls[1].service_data.include_outlook, true);
  assert.deepEqual(calls[1].service_data.interval_windows, view.intervals.map(({ start, end }) => ({ start, end })));
  assert.ok(states.some((item) => item.forecast.status === "ready" && item.measurement.status === "loading"));
  assert.equal(state.forecast.data.summary.today_kwh, 23.14);
  assert.equal(state.measurement.data.total_energy.energy_kwh, 12.4);
  assert.equal(state.loading, false);
});

test("Morgen behält den heutigen Ist-KPI, ohne morgige Messfenster anzufordern", async () => {
  const { calls, state } = await load("sunny", { day: "tomorrow" });
  assert.equal(calls[0].service_data.day, "tomorrow");
  assert.equal(calls[1].service_data.interval_windows, undefined);
  assert.equal(selectedSeries(state).actual.length, 0);
  assert.match(renderContent({ ...config, day: "tomorrow" }, state), /12,4/);
});

test("Eine Dachauswahl liest weder Gesamtmessung noch Gesamtarchiv", async () => {
  const { calls, state } = await load("sunny", { roof_id: "south" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].service_data.roof_id, "south");
  assert.equal(state.measurement.status, "roof");
  assert.equal(selectedSeries(state).actual.length, 0);
  assert.equal(selectedSeries(state).history.length, 0);
  const html = renderContent({ ...config, roof_id: "south" }, state);
  assert.match(html, /Keine Dachmessung/);
  assert.match(html, /12,47/);
  assert.doesNotMatch(html, /id="report"/);
  assert.doesNotMatch(html, /id="outlook"|id="uncertainty"|id="planning"/);
});

test("Genau Mitternacht wird kein leeres Messfenster abgefragt", async () => {
  const { calls, state } = await load("midnight");
  assert.deepEqual(calls.map((item) => item.service), ["get_forecast", "get_history"]);
  assert.match(state.measurement.message, /beginnt gerade/);
});

test("Eine inzwischen entfernte gespeicherte Dachauswahl bietet einen Rückweg zur Gesamtanlage", async () => {
  const { calls, state } = await load("deleted-roof", { roof_id: "south" });
  assert.equal(calls.length, 1);
  assert.match(state.forecast.message, /Dachfläche ist nicht mehr vorhanden/);
  assert.match(renderContent({ ...config, roof_id: "south" }, state), /data-reset-roof>Gesamtanlage anzeigen/);
  const recovered = await load("deleted-roof");
  assert.equal(recovered.state.forecast.status, "ready");
});

test("Optionale ACL-Fehler lassen Prognose und Bedienung verfügbar", async () => {
  const { state } = await load("acl");
  assert.equal(state.forecast.status, "ready");
  assert.equal(state.measurement.status, "error");
  assert.equal(state.history.status, "error");
  const html = renderContent(config, state, 328);
  assert.match(html, /Keine Leseberechtigung für Messdaten/);
  assert.match(html, /Keine Leseberechtigung für Archivdaten/);
  assert.match(html, /23,14/);
  assert.match(html, /data-day="tomorrow"/);
});

test("Backendausfall und alter Ansichtsvertrag erzeugen kontrollierte deutsche Zustände", async () => {
  const unavailable = await load("outage");
  assert.equal(unavailable.calls.length, 1);
  assert.equal(unavailable.state.forecast.status, "error");
  assert.match(renderContent(config, unavailable.state), /Prognosedaten sind derzeit nicht erreichbar/);
  const old = await load("old");
  assert.equal(old.calls.length, 1);
  assert.match(renderContent(config, old.state), /Datenvertrag 1/);
  const invalid = fixture().forecast;
  invalid.view.timezone = "Keine/Zeitzone";
  assert.throws(() => validateView(invalid));
  const oldSchema = fixture().forecast;
  oldSchema.schema_version = 2;
  assert.throws(() => validateView(oldSchema), /Datenvertrag 1/);
  delete oldSchema.schema_version;
  assert.throws(() => validateView(oldSchema), /Datenvertrag 1/);
});

for (const service of ["get_measurements", "get_history"]) for (const version of [undefined, 2]) test(`${service}: unbekannte Datenversion ${version} lässt die gültige Prognose verfügbar`, async () => {
  const hass = fixtureHass();
  const read = hass.callWS;
  hass.callWS = async (message) => {
    const result = await read(message);
    if (message.service === service) result.response.schema_version = version;
    return result;
  };
  let state;
  await loadView(hass, config, (value) => { state = value; });
  assert.equal(state.forecast.status, "ready");
  const rejected = service === "get_measurements" ? state.measurement : state.history;
  assert.equal(rejected.status, "error");
  assert.equal(rejected.data, undefined);
  assert.match(rejected.message, /Datenvertrag 1/);
  assert.match(renderContent(config, state), /23,14/);
  assert.match(renderContent(config, state), /data-day="tomorrow"/);
});

for (const version of [undefined, 2]) test(`Separater 30-Tage-Bericht weist unbekannte Datenversion ${version} zurück`, async () => {
  const calls = [], hass = fixtureHass("sunny", { calls });
  const read = hass.callWS;
  hass.callWS = async (message) => {
    const result = await read(message);
    result.response.schema_version = version;
    return result;
  };
  const card = new PvForecastCard();
  card._config = config;
  card._hass = hass;
  card._connected = true;
  card._reportOpen = true;
  card._reportDays = 30;
  try {
    card._bindReport();
    await flush();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].service_data.days, 30);
    assert.equal(card._report.status, "error");
    assert.equal(card._report.data, undefined);
    assert.match(renderReport(card._report, 30), /Datenvertrag 1/);
  } finally { card.disconnectedCallback(); }
});

test("Teilweise erfasste Energie bleibt ausdrücklich unvollständig, mehrere Zähler werden nicht im Browser addiert", async () => {
  const { state } = await load("gaps");
  const html = renderContent(config, state, 328);
  assert.match(html, /5,6/);
  assert.match(html, /Unvollständig erfasst/);
  assert.match(html, /nur der bisher belegte Teil/);
  assert.match(html, /Schattierte Lücken/);
  assert.equal(state.measurement.data.total_energy.source_count, 2);
  assert.equal(tableRows(state)[8].actual, null);
  assert.equal(tableRows(state)[9].forecast, null);
  assert.equal(tableRows(state)[0].actual, 0);
  assert.equal(energyText(0), "0");
  assert.equal(energyText(null), "—");
});

test("Fehlende und unvollständige Intervalle werden nicht verbunden; Null bleibt ein Pfad", () => {
  const items = fixture().forecast.view.intervals.slice(0, 5);
  items[2] = { ...items[2], energy_kwh: null };
  const base = Date.parse(items[0].start);
  const x = (value) => (Date.parse(value) - base) / 3_600_000;
  assert.deepEqual(seriesPaths(items, x, (value) => value, "is_complete"), ["M0,0 L1,0 L1,0 L2,0", "M3,0 L4,0 L4,0 L5,0"]);
  items[2].energy_kwh = 3;
  items[2].is_complete = false;
  assert.equal(seriesPaths(items, x, (value) => value, "is_complete").length, 2);
  assert.equal(seriesPaths([items[0], items[3]], x, (value) => value).length, 2);
});

for (const [scenario, expectedHours] of [["spring", 23], ["fold", 25], ["kolkata", 24]]) test(`${scenario}: absolute Tagesachse mit ${expectedHours} Stunden und unveränderten Intervallen`, async () => {
  const { state } = await load(scenario);
  const view = state.forecast.data, geometry = plotGeometry(view, 328);
  assert.equal((Date.parse(view.end) - Date.parse(view.start)) / 3_600_000, expectedHours);
  assert.equal(geometry.x(view.start), 34);
  assert.equal(geometry.x(view.end), 316);
  assert.ok(Math.abs(geometry.x(view.intervals[0].end) - geometry.x(view.start) - (Date.parse(view.intervals[0].end) - Date.parse(view.start)) / (expectedHours * 3_600_000) * 282) < 1e-10);
  assert.equal(tableRows(state).length, view.intervals.length);
  if (scenario === "kolkata") {
    assert.equal(view.intervals.length, 25);
    assert.equal(Date.parse(view.intervals[0].end) - Date.parse(view.start), 1_800_000);
    assert.match(renderContent(config, state), /UTC\+05:30/);
  }
});

test("Wiederholte Berliner 02-Uhr-Stunden sind durch ihren UTC-Offset eindeutig", async () => {
  const { state } = await load("fold");
  assert.equal(formatPlantTime("2026-10-25T00:00:00Z", "Europe/Berlin"), "02:00 UTC+02:00");
  assert.equal(formatPlantTime("2026-10-25T01:00:00Z", "Europe/Berlin"), "02:00 UTC+01:00");
  const geometry = plotGeometry(state.forecast.data);
  assert.ok(geometry.x("2026-10-25T01:00:00Z") > geometry.x("2026-10-25T00:00:00Z"));
  const html = renderContent(config, state);
  assert.match(html, /02:00 UTC\+02:00/);
  assert.match(html, /02:00 UTC\+01:00/);
});

test("Die Browserzeitzone beeinflusst weder Anlagenzeit noch Achse", () => {
  const previous = process.env.TZ;
  try {
    process.env.TZ = "America/Los_Angeles";
    assert.equal(formatPlantTime("2026-09-10T06:00:00Z", "Asia/Kolkata"), "11:30 UTC+05:30");
    assert.equal(formatPlantTime("2026-09-10T06:00:00Z", "Europe/Berlin"), "08:00 UTC+02:00");
  } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
});

test("Archivintervalle werden nach absoluter Überlappung gewählt und nie browserseitig skaliert", async () => {
  const { state } = await load("kolkata");
  const view = state.forecast.data;
  state.history.data.current_targets.intervals = [{ start: "2026-09-09T18:00:00Z", end: "2026-09-09T19:00:00Z", energy_kwh: 2.7 }];
  const history = selectedSeries(state).history;
  assert.equal(history.length, 1);
  assert.equal(history[0].energy_kwh, 2.7);
  assert.ok(Date.parse(history[0].start) < Date.parse(view.start));
  assert.equal(tableRows(state).find((row) => row.start === history[0].start).history, 2.7);
  assert.match(renderContent(config, state), new RegExp(ARCHIVE_LABEL));
});

test("Dynamische Texte sind HTML-escaped; Semantik und Tabellenfallback sind vorhanden", async () => {
  const { state } = await load();
  state.forecast.data.roofs[0].name = '<script>alert("Dach")</script>';
  state.forecast.data.plant_name = '<img src=x onerror="alert(1)">';
  const html = renderContent(config, state, 328);
  assert.doesNotMatch(html, /<script>|<img src=x/);
  assert.match(html, /&lt;img/);
  assert.match(html, /aria-label="Fläche"/);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /<table>/);
  assert.match(html, /scope="row"/);
  assert.match(html, /role="img"/);
  assert.match(html, /class="history-line"/);
  assert.match(html, /class="actual-bar"/);
});

test("Veralteter Stand und leeres Archiv werden nicht als aktuelle vollständige Nullprognose ausgegeben", async () => {
  const stale = await load("stale"), empty = await load("empty");
  assert.match(renderContent(config, stale.state), /Prognosestand veraltet/);
  assert.match(renderContent(config, stale.state), /Ansicht 13:15 UTC\+02:00/);
  assert.match(renderContent(config, stale.state), /Wetterabruf[^<]+11:15 UTC\+02:00/);
  const html = renderContent(config, empty.state);
  assert.match(html, /Noch keine Intervallwerte/);
  assert.match(html, /noch keine Stundenstände/);
  assert.match(html, /—/);
});

test("Bericht übernimmt MAE/Bias/Stichprobe unverändert und weist Kürzung sowie Pause aus", () => {
  const data = fixture().history;
  data.retention_truncated = true; data.enabled = false;
  const html = renderReport({ data }, 30);
  assert.match(html, /30 abgeschlossene lokale Tage/);
  assert.match(html, /0,18/);
  assert.match(html, /-0,07/);
  assert.match(html, /136/);
  assert.match(html, /ältere Daten gekürzt/);
  assert.match(html, /Erfassung ist pausiert/);
});

test("Mehrere Karten derselben Verbindung teilen einen vollständigen Lesezyklus und einen Timer", async () => {
  const clock = clockCache(), calls = [], hass = fixtureHass("sunny", { calls });
  const first = [], second = [];
  const loader = (publish, active) => loadView(hass, config, publish, active, clock.cache);
  const stopFirst = clock.cache.subscribe("plant/today", loader, (state) => first.push(state));
  const stopSecond = clock.cache.subscribe("plant/today", loader, (state) => second.push(state));
  await flush();
  assert.equal(calls.length, 3);
  assert.equal(first.at(-1).loading, false);
  assert.equal(second.at(-1).loading, false);
  assert.equal(clock.timers.size, 1);
  await clock.advance(REFRESH_MS);
  assert.equal(calls.length, 6);
  stopFirst(); assert.equal(clock.timers.size, 1);
  stopSecond(); assert.equal(clock.timers.size, 0);
  assert.equal(clock.listeners.size, 0);
});

test("Frische identische Einzelaktionen werden auch zwischen Kartenansicht und Bericht geteilt", async () => {
  const clock = clockCache();
  let calls = 0;
  const loader = async () => ({ number: ++calls });
  const [first, second] = await Promise.all([clock.cache.request("history7", loader), clock.cache.request("history7", loader)]);
  assert.equal(calls, 1); assert.equal(first, second);
  await clock.advance(REFRESH_MS);
  assert.equal((await clock.cache.request("history7", loader)).number, 2);
});

test("Standardtimer behalten den Browser als Empfänger beim Planen und Abbrechen", () => {
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  const calls = [];
  globalThis.setTimeout = function (handler, delay) {
    assert.equal(this, globalThis, "setTimeout benötigt den Window-Empfänger");
    assert.equal(typeof handler, "function");
    calls.push(["set", delay]);
    return 42;
  };
  globalThis.clearTimeout = function (timer) {
    assert.equal(this, globalThis, "clearTimeout benötigt den Window-Empfänger");
    calls.push(["clear", timer]);
  };
  try {
    const cache = new SharedReadCache({ now: () => 0 });
    cache.entries.set("plant", { listeners: new Set([() => {}]), running: false, at: 0 });
    cache._schedule();
    assert.equal(cache.timer, 42);
    cache._cancelTimer();
    assert.equal(cache.timer, null);
    assert.deepEqual(calls, [["set", REFRESH_MS], ["clear", 42]]);
  } finally { globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear; }
});

test("Unsichtbares Dokument hält keine Timer oder neuen Lesezyklen; Sichtbarkeit setzt fällige Abfrage fort", async () => {
  const clock = clockCache(); let reads = 0;
  clock.document.hidden = true;
  const stop = clock.cache.subscribe("plant", async (publish) => { reads++; publish({ loading: false }); }, () => {});
  await flush(); assert.equal(reads, 0); assert.equal(clock.timers.size, 0);
  await clock.visible(true); assert.equal(reads, 1); assert.equal(clock.timers.size, 1);
  await clock.visible(false); assert.equal(clock.timers.size, 0);
  await clock.advance(REFRESH_MS * 5); assert.equal(reads, 1);
  await clock.visible(true); assert.equal(reads, 2);
  stop();
});

test("Antwort nach Disconnect wird verworfen und startet keine Folgeaktionen oder Timer", async () => {
  const clock = clockCache(), states = [], calls = [];
  let resolve;
  const hass = { callWS(message) { calls.push(message); return new Promise((done) => { resolve = done; }); } };
  const stop = clock.cache.subscribe("plant", (publish, active) => loadView(hass, config, publish, active, clock.cache), (state) => states.push(state));
  await flush(); assert.equal(calls.length, 1);
  stop();
  resolve({ response: fixture().forecast });
  await flush();
  assert.equal(states.length, 1);
  assert.equal(states[0].forecast.status, "loading");
  assert.equal(calls.length, 1);
  assert.equal(clock.timers.size, 0);
});

test("Erneutes Verbinden nach abgebrochenem Zyklus lädt optionale Bereiche vollständig", async () => {
  const clock = clockCache(), calls = [], hass = fixtureHass("sunny", { calls });
  let release;
  const original = hass.callWS;
  hass.callWS = async (message) => {
    const value = await original(message);
    if (message.service === "get_measurements") await new Promise((resolve) => { release = resolve; });
    return value;
  };
  const loader = (publish, active) => loadView(hass, config, publish, active, clock.cache);
  const stop = clock.cache.subscribe("plant", loader, () => {});
  await flush(); stop(); release(); await flush();
  const states = [], stopAgain = clock.cache.subscribe("plant", loader, (state) => states.push(state));
  await flush();
  assert.equal(states.at(-1).loading, false);
  assert.equal(states.at(-1).measurement.status, "ready");
  assert.equal(clock.timers.size, 1);
  assert.equal(calls.length, 3);
  stopAgain();
});

test("Einzeln unsichtbare Karten beenden Ansicht und Bericht, sichtbare Nachbarn lesen weiter", async () => {
  const previous = globalThis.IntersectionObserver;
  const observers = [];
  globalThis.IntersectionObserver = class {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
    visible(isIntersecting) { this.callback([{ target: this.target, isIntersecting }]); }
  };
  const calls = [], hass = fixtureHass("sunny", { calls });
  const first = new PvForecastCard(), second = new PvForecastCard();
  const cache = connectionCache(hass);
  const listeners = () => [...cache.entries.values()].map((entry) => entry.listeners.size).reduce((a, b) => a + b, 0);
  try {
    for (const card of [first, second]) {
      card.setConfig(config);
      card.hass = hass;
      card._reportOpen = true;
      card._reportDays = 30;
      card.connectedCallback();
    }
    await flush();
    assert.equal(calls.length, 0, "Vor der ersten Sichtbarkeitsmeldung gibt es keinen Lesezyklus");
    assert.equal(listeners(), 0);
    observers[0].visible(true);
    observers[1].visible(true);
    await flush();
    assert.equal(calls.length, 4, "Beide Karten teilen die drei Ansichtsaktionen und den 30-Tage-Bericht");
    assert.equal(listeners(), 4);
    assert.equal(first._report.status, "ready");
    observers[0].visible(false);
    assert.equal(first._unsubscribe, null);
    assert.equal(first._unsubscribeReport, null);
    assert.equal(listeners(), 2);
    assert.notEqual(cache.timer, null, "Der sichtbare Nachbar behält den gemeinsamen Timer");
    observers[1].visible(false);
    assert.equal(listeners(), 0);
    assert.equal(cache.timer, null);
    assert.equal(cache.listening, false);
    first._bindReport();
    assert.equal(first._unsubscribeReport, null, "Ein geöffnetes Berichtselement startet unsichtbar kein Abonnement");
    observers[0].visible(true);
    await flush();
    assert.equal(listeners(), 2);
    assert.equal(first._report.status, "ready");
    assert.equal(calls.length, 4, "Wiederanzeige verwendet frische gemeinsame Daten");
    first.disconnectedCallback();
    assert.equal(observers[0].disconnected, true);
    assert.equal(cache.timer, null);
    observers[0].visible(false);
    observers[0].visible(true);
    await flush();
    assert.equal(listeners(), 0, "Verspätete Observer-Meldungen reaktivieren keine entfernte Karte");
    assert.equal(calls.length, 4);
    first.connectedCallback();
    observers[0].visible(false);
    observers[0].visible(true);
    await flush();
    assert.equal(listeners(), 0, "Nach Wiederverbinden gilt nur die neue Observer-Generation");
    observers[2].visible(true);
    await flush();
    assert.equal(listeners(), 2);
    assert.equal(first._report.status, "ready");
    assert.equal(calls.length, 4);
  } finally {
    first.disconnectedCallback();
    second.disconnectedCallback();
    if (previous === undefined) delete globalThis.IntersectionObserver;
    else globalThis.IntersectionObserver = previous;
  }
});

test("Tagesaussicht übernimmt getrennte Backendwerte ohne eigene Addition", async () => {
  const { state } = await load();
  state.measurement.data.outlook = { ...state.measurement.data.outlook, measured_kwh: 8, bridge_kwh: 0.4, remaining_kwh: 12, total_kwh: 91.23 };
  const html = renderOutlook(state);
  assert.match(html, /91,23 kWh/);
  assert.match(html, /Gesichert gemessen/);
  assert.match(html, /Geschätzt seit letzter Messung/);
  assert.match(html, /Rest ab jetzt/);
  assert.match(html, /0,4/);
  assert.doesNotMatch(html, /20,4/);
  assert.match(html, /Kurzfristige Korrektur ist aus/);
  state.measurement.data.outlook.status = "unavailable";
  assert.doesNotMatch(renderOutlook(state), /91,23/);
  state.measurement.data.outlook.schema_version = 2;
  assert.match(renderOutlook(state), /Noch offen/);
});

test("Erfahrungsband gehört sichtbar zur eigenen eingefrorenen Prognose", async () => {
  const { state } = await load("experience");
  const html = renderUncertainty(state);
  assert.match(html, /17,2–28,4 kWh/);
  assert.match(html, /22,5 kWh/);
  assert.match(html, /06 Uhr am Zieltag/);
  assert.match(html, /unabhängig von der aktuellen Tagesprognose/);
  assert.match(html, /60 Lerntage, 30 Prüftage/);
  assert.match(html, /83,3 %/);
  assert.match(html, /Mittlere Bandbreite in der Prüfung: 11,2 kWh/);
  assert.match(html, /66,4–92,7 %/);
  assert.match(html, /Annahme unabhängiger Tage/);
  assert.doesNotMatch(html, /23,14/);
  state.history.data.uncertainty.days.today.status = "unavailable";
  assert.match(renderUncertainty(state), /Bandbreite noch nicht belastbar/);
  assert.doesNotMatch(renderUncertainty(state), /17,2–28,4/);
});

test("Stundenband zeigt nur die gelieferten festen Grenzen und seinen Vorlauf", async () => {
  const { state } = await load("experience");
  const band = {
    ...state.history.data.uncertainty.days.today,
    rule_version: 2, horizon: "hourly_3h",
    start: "2026-09-10T10:00:00Z", end: "2026-09-10T11:00:00Z",
    lower_kwh: 1.23, central_kwh: 2.34, upper_kwh: 3.45,
  };
  state.history.data.uncertainty.frozen_hours = [band];
  const html = renderUncertainty(state);
  assert.match(html, /Eingefrorene zukünftige Stunden/);
  assert.match(html, /Stand 3 Stunden vorher: 1,23–3,45 kWh/);
  assert.match(html, /damalige Prognose 2,34 kWh/);
  assert.match(html, /Eine Stunde Energie, keine Summe des Vorlaufs/);
  band.rule_version = 99;
  assert.doesNotMatch(renderUncertainty(state), /1,23–3,45/);
  band.rule_version = 2;
  band.status = "unavailable";
  assert.doesNotMatch(renderUncertainty(state), /1,23–3,45/);
  band.target_date = "2026-09-11";
  assert.doesNotMatch(renderUncertainty(state), /Eingefrorene zukünftige Stunden/);
});

for (const scenario of ["fold", "kolkata"]) test(`${scenario}: Planung bietet absolute Grenzen mit Datum und Offset für beide Tage`, async () => {
  const { state } = await load(scenario);
  const choices = planningChoices(state);
  assert.ok(choices.includes(state.forecast.data.as_of));
  assert.ok(Date.parse(choices.at(-1)) > Date.parse(state.forecast.data.end));
  assert.equal(new Set(choices).size, choices.length);
  const html = renderPlanning(state);
  if (scenario === "fold") {
    assert.match(html, /02:00 UTC\+02:00/);
    assert.match(html, /02:00 UTC\+01:00/);
  } else assert.match(html, /UTC\+05:30/);
  assert.match(html, /type="number"[^>]+min="1"[^>]+max="2880"/);
  assert.match(html, /type="submit"/);
});

test("Planung zeigt nur gelieferte Ergebnisse und Fehler, ohne eigene PV-Rechnung", async () => {
  const { state } = await load();
  const data = { ...fixture().forecast.planning, energy_kwh: 63.21, status: "started", hysteresis_applied: true };
  const html = renderPlanning(state, { result: { status: "ready", data } });
  assert.match(html, /63,21/);
  assert.match(html, /läuft bereits und wird nicht automatisch verschoben/);
  assert.match(html, /geringfügig geänderter Prognose/);
  assert.match(html, /Wetterabruf/);
  assert.match(html, /gleichmäßiger mittlerer Leistung/);
  assert.match(html, /Hausverbrauch und Speicher/);
  data.energy_kwh = null;
  assert.match(renderPlanning(state, { result: { data } }), /läuft bereits/);
  assert.doesNotMatch(renderPlanning(state, { result: { data } }), /— kWh/);
  data.status = "unavailable";
  data.reason = "no_energy";
  assert.doesNotMatch(renderPlanning(state, { result: { data } }), /63,21/);
  assert.match(renderPlanning(state, { result: { data } }), /keine nutzbare PV-Energie/);
  assert.match(renderPlanning(state, { result: { message: "<script>" } }), /&lt;script&gt;/);
});

function planningCard(hass) {
  const card = new PvForecastCard();
  card._config = config;
  card._hass = hass;
  card._connected = true;
  card._visible = true;
  card._planningOpen = true;
  card._planningInputs = { duration_minutes: "120", earliest_start: "2026-09-10T11:15:00.000Z", latest_end: "2026-09-11T22:00:00.000Z" };
  return card;
}

async function refreshPlanning(cache) {
  cache._cancelTimer();
  cache.requests.clear();
  for (const entry of cache.entries.values()) entry.at = -Infinity;
  cache._tick();
  await flush();
}

test("Planung liest erst nach bewusster Berechnung und bewahrt previous_start auch über Fehler", async () => {
  const calls = [], hass = fixtureHass("sunny", { calls });
  const card = planningCard(hass), cache = connectionCache(hass);
  const original = hass.callWS;
  let fail = false;
  hass.callWS = async (message) => { const response = await original(message); if (fail) throw { code: "home_assistant_error" }; return response; };
  try {
    card._bindPlanning(); await flush(); assert.equal(calls.length, 0);
    card._calculatePlanning(); await flush();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].service, "get_forecast");
    assert.deepEqual(calls[0].service_data.planning, { duration_minutes: 120, earliest_start: card._planningInputs.earliest_start, latest_end: card._planningInputs.latest_end });
    const previous = card._planning.data.start;
    fail = true;
    await refreshPlanning(cache);
    assert.equal(calls[1].service_data.planning.previous_start, previous);
    assert.equal(card._planning.status, "error");
    fail = false;
    await refreshPlanning(cache);
    assert.equal(calls[2].service_data.planning.previous_start, previous);
    assert.equal(card._planning.status, "ready");
    card._planningOpen = false; card._bindPlanning();
    assert.equal(cache.timer, null);
    card._planningOpen = true; card._bindPlanning(); await flush();
    assert.equal(calls.length, 3);
  } finally { card.disconnectedCallback(); }
});

test("Mehrere Planungen teilen die Leseabfrage; unsichtbare und entfernte Karten halten keinen Timer", async () => {
  const calls = [], hass = fixtureHass("sunny", { calls }), cache = connectionCache(hass);
  const first = planningCard(hass), second = planningCard(hass);
  try {
    first._calculatePlanning(); second._calculatePlanning(); await flush();
    assert.equal(calls.length, 1);
    assert.equal(first._planning.data.start, second._planning.data.start);
    first._visible = false; first._bindPlanning();
    assert.equal(first._unsubscribePlanning, null);
    assert.notEqual(cache.timer, null);
    second.disconnectedCallback();
    assert.equal(cache.timer, null);
    first._visible = true; first._bindPlanning(); await flush();
    assert.equal(calls.length, 1);
  } finally { first.disconnectedCallback(); second.disconnectedCallback(); }
});

test("Ungültige Eingabe, fehlende Rechte und unbekannter Planungsvertrag bleiben kontrolliert", async () => {
  const calls = [], hass = fixtureHass("sunny", { calls }), card = planningCard(hass);
  try {
    card._planningInputs.duration_minutes = "0";
    card._calculatePlanning(); await flush();
    assert.equal(calls.length, 0);
    assert.match(card._planning.message, /1 bis 2880 Minuten/);
    card._planningInputs.duration_minutes = "120";
    const read = hass.callWS;
    hass.callWS = async (message) => { const result = await read(message); result.response.planning.schema_version = 2; return result; };
    card._calculatePlanning(); await flush();
    assert.equal(card._planning.status, "error");
    assert.match(card._planning.message, /Datenvertrag 1/);
    hass.callWS = async () => { throw { code: "unauthorized" }; };
    await refreshPlanning(connectionCache(hass));
    assert.match(card._planning.message, /Keine Leseberechtigung für Planungsdaten/);
  } finally { card.disconnectedCallback(); }
});


test("Kurzfristiger Vergleich bleibt eine Beobachtung mit eigenem Prüffenster", () => {
  const data = { horizons: { hourly_1h: {} }, short_term: { schema_version: 1, rule_version: 1, enabled: true, window_days: 60, minimum_days: 30, horizons: { hourly_1h: { days: 30, count: 90, baseline_mae_kwh: 0.4, candidate_mae_kwh: 0.3, baseline_bias_kwh: 0.2, candidate_bias_kwh: 0.1, criterion_met: true } } } };
  const html = renderReport({data}, 7);
  assert.match(html, /Eigenes Prüffenster: 60 abgeschlossene Tage/);
  assert.match(html, /MAE Basis 0,4 kWh; Kandidat 0,3 kWh/);
  assert.match(html, /weiterhin nur Beobachtung/);
  assert.match(html, /Produktive Prognose unverändert/);
  data.short_term.rule_version = 99;
  assert.match(renderReport({data}, 7), /unbekannte Datenversion/);
  assert.doesNotMatch(renderReport({data}, 7), /Kandidat 0,3/);
});


test("Temperaturvergleich zeigt Rohmodelle derselben Messpaare ohne Modellfreigabe", () => {
  const data = { horizons: { hourly_1h: {} }, temperature_comparison: { schema_version: 1, model: "ross_comparison_v1", enabled: true, parameter_id: "example", window_days: 90, horizons: { hourly_1h: { days: 20, count: 100, raw_mae_kwh: 0.4, alternative_mae_kwh: 0.38, raw_bias_kwh: 0.2, alternative_bias_kwh: 0.1 } } } };
  const html = renderReport({data}, 7);
  assert.match(html, /Rohmodelle ohne übertragene Kalibrierung/);
  assert.match(html, /MAE Rohmodell 0,4 kWh; Ross 0,38 kWh/);
  assert.match(html, /Produktive Prognose unverändert/);
  data.temperature_comparison.model = "unknown";
  assert.match(renderReport({data}, 7), /unbekannte Datenversion/);
});


test("Experimenteller Hinweis trennt Messung, Rohbasis und Lernstopp ohne Dachdiagnose", async () => {
  const report = {schema_version: 1, status: "active", experimental: true, first_day: "2026-09-01", last_day: "2026-09-07", raw_kwh: 140, actual_kwh: 84, difference_kwh: 56, shortfall_fraction: 0.4, coverage_fraction: 1, below_days: 7, accepted_factor: 0.9, learning_paused: true, comparison: {training_count: 60, validation_count: 30, evaluation: {coverage_fraction: 0.8}}};
  const html = renderUnderperformance(report);
  assert.match(html, /Rohprognose 140 kWh · Messung 84 kWh/);
  assert.match(html, /keine Defektdiagnose/);
  assert.match(html, /Faktor 1/);
  assert.match(html, /keine Dachdiagnose/);
  assert.equal(renderUnderperformance({...report, schema_version: 99}), "");
  const changed = renderUnderperformance({...report, status: "reference_changed"});
  assert.doesNotMatch(changed, /Messung 84/);
  assert.match(changed, /Erholung ist damit nicht belegt/);
  const {state} = await load();
  state.history.data.underperformance = report;
  assert.match(renderContent(config, state), /Experimentelle Minderertragsprüfung/);
  const roofState = (await load("sunny", {roof_id: "east"})).state;
  assert.doesNotMatch(renderContent({...config, roof_id: "east"}, roofState), /Experimentelle Minderertragsprüfung/);
});
