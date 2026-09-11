/* PV Forecast: native, rein lesende Lovelace-Karte ohne Laufzeitabhängigkeiten. */

export const REFRESH_MS = 60_000;
export const ARCHIVE_LABEL = "Jeweils 1 Stunde vorher";
const UPDATE_HINT = "Bitte die PV-Forecast-Integration und die Kartenressource aktualisieren. Die Kartenansicht benötigt Datenvertrag 1.";
const PLANNING_CHANGED_HINT = "Auswahl geändert. Erneut berechnen, um die Empfehlung anzupassen.";
const numberFormat = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });
const caches = new WeakMap();
const finite = (value) => typeof value === "number" && Number.isFinite(value);
export const energyText = (value) => finite(value) ? numberFormat.format(value) : "—";
export const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const millis = (value) => Date.parse(value);
const validInterval = (item) => Number.isFinite(millis(item?.start)) && millis(item.end) > millis(item.start);
const overlap = (item, view) => validInterval(item) && millis(item.end) > millis(view.start) && millis(item.start) < millis(view.end);

export function formatPlantTime(value, timezone, offset = true) {
  return new Intl.DateTimeFormat("de-DE", {
    timeZone: timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    ...(offset ? { timeZoneName: "longOffset" } : {}),
  }).format(new Date(value)).replace("GMT", "UTC");
}

export function formatPlantDate(value, timezone) {
  return new Intl.DateTimeFormat("de-DE", { timeZone: timezone, weekday: "short", day: "2-digit", month: "short" }).format(new Date(value));
}

export function validateView(response) {
  const view = response?.view;
  if (response?.schema_version !== 1 || view?.view_version !== 1) throw new Error(UPDATE_HINT);
  if (!validInterval(view) || !finite(millis(view.as_of)) || !Array.isArray(view.intervals) || !Array.isArray(view.roofs) || !view.summary) {
    throw new Error("Die Prognoseansicht enthält keine gültigen Zeitfenster.");
  }
  // Die IANA-Zone wird ausdrücklich geprüft; die Browserzone ist kein Ersatz.
  formatPlantTime(view.start, view.timezone);
  return view;
}

export async function readService(hass, service, serviceData) {
  const result = await hass.callWS({ type: "call_service", domain: "pv_forecast", service, service_data: serviceData, return_response: true });
  if (!result?.response || typeof result.response !== "object") throw new Error("Die Aktion hat keine lesbaren Daten geliefert.");
  if (result.response.schema_version !== 1) throw new Error(UPDATE_HINT);
  return result.response;
}

export function sourceError(error, source) {
  if (error instanceof Error && error.message === UPDATE_HINT) return { status: "error", reason: "version", message: UPDATE_HINT };
  const code = `${error?.code ?? ""} ${error?.translation_key ?? ""}`;
  if (/unauthorized|auth_required|permission/i.test(code)) return { status: "error", reason: "permission", message: `Keine Leseberechtigung für ${source}.` };
  if (/history_unavailable|measurements_unavailable/.test(code)) return { status: "empty", message: `${source} sind für diese Anlage noch nicht verfügbar.` };
  return { status: "error", reason: "unavailable", message: `${source} sind derzeit nicht erreichbar.` };
}

/** Ein Timer je Verbindung; Antworten ohne aktive Abnehmer werden verworfen. */
export class SharedReadCache {
  constructor({ document = globalThis.document, now = () => Date.now(), setTimer = (handler, delay) => globalThis.setTimeout(handler, delay), clearTimer = (timer) => globalThis.clearTimeout(timer) } = {}) {
    this.document = document;
    this.now = now;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.entries = new Map();
    this.requests = new Map();
    this.timer = null;
    this.listening = false;
    this.onVisibility = () => { this._cancelTimer(); if (!this.document?.hidden) this._tick(); };
  }

  request(key, loader, force = false) {
    const previous = this.requests.get(key);
    if (previous && (previous.pending || (!force && this.now() - previous.at < REFRESH_MS))) return previous.promise;
    const promise = Promise.resolve().then(loader);
    const request = { at: this.now(), promise, pending: true };
    promise.then(() => { request.pending = false; }, () => { request.pending = false; });
    this.requests.set(key, request);
    if (this.requests.size > 64) this.requests.delete(this.requests.keys().next().value);
    return promise;
  }

  subscribe(key, loader, listener) {
    let entry = this.entries.get(key);
    if (!entry) {
      entry = { listeners: new Set(), loader, state: null, at: -Infinity, running: false, generation: 0 };
      this.entries.set(key, entry);
    }
    entry.listeners.add(listener);
    entry.loader = loader;
    if (!this.listening) {
      this.document?.addEventListener("visibilitychange", this.onVisibility);
      this.listening = true;
    }
    if (entry.state) listener(entry.state);
    if (!this.document?.hidden) this._tick();
    return () => {
      entry.listeners.delete(listener);
      if (!entry.listeners.size) {
        entry.generation += 1;
        entry.running = false;
        // Kein halb fertiger Stand darf nach erneutem Verbinden als frisch gelten.
        if (entry.state?.loading) entry.at = -Infinity;
      }
      if (![...this.entries.values()].some((item) => item.listeners.size)) {
        this._cancelTimer();
        this.document?.removeEventListener("visibilitychange", this.onVisibility);
        this.listening = false;
      } else this._schedule();
    };
  }

  _cancelTimer() { if (this.timer !== null) this.clearTimer(this.timer); this.timer = null; }

  _schedule() {
    this._cancelTimer();
    if (this.document?.hidden) return;
    const due = [...this.entries.values()].filter((item) => item.listeners.size && !item.running).map((item) => item.at + REFRESH_MS);
    if (due.length) this.timer = this.setTimer(() => { this.timer = null; this._tick(); }, Math.max(1, Math.min(...due) - this.now()));
  }

  _tick() {
    if (this.document?.hidden) return;
    for (const entry of this.entries.values()) {
      if (!entry.listeners.size || entry.running || this.now() - entry.at < REFRESH_MS) continue;
      entry.running = true;
      const generation = ++entry.generation;
      const active = () => generation === entry.generation && entry.listeners.size > 0;
      const publish = (state) => {
        if (!active()) return;
        state = retainReadState(entry.state, state);
        entry.state = state;
        for (const listener of entry.listeners) listener(state);
      };
      Promise.resolve().then(() => entry.loader(publish, active)).catch(() => {
        publish({ loading: false, forecast: { status: "error", message: "Die Daten konnten nicht geladen werden." } });
      }).finally(() => {
        if (!active()) return;
        entry.running = false;
        entry.at = this.now();
        this._schedule();
      });
    }
    this._schedule();
  }
}

export function connectionCache(hass) {
  const connection = hass.connection ?? hass;
  if (!caches.has(connection)) caches.set(connection, new SharedReadCache());
  return caches.get(connection);
}

/** Nur fertige Tagesfelder auswählen; Kennzahlen und Messstand bleiben gemeinsam. */
export function selectViewDay(state, day = "today") {
  const view = state?.forecast?.data;
  if (!view || view.day === day) return state;
  const selected = view.day_views?.[day];
  if (!selected || selected.day !== day || !validInterval(selected) || !Array.isArray(selected.intervals)) {
    return { ...state, forecast: { status: "error", reason: "version", message: UPDATE_HINT } };
  }
  return { ...state, forecast: { ...state.forecast, data: { ...view, ...selected } } };
}

export async function loadView(hass, config, publish, active = () => true, cache = null) {
  const emit = publish;
  publish = (state) => emit(selectViewDay(state, config.day));
  const read = (service, data) => cache ? cache.request(JSON.stringify([service, data]), () => readService(hass, service, data)) : readService(hass, service, data);
  let view;
  const state = { loading: true, forecast: { status: "loading" }, measurement: { status: "idle" }, history: { status: "idle" } };
  publish({ ...state });
  try {
    const data = await read("get_forecast", { config_entry_id: config.config_entry_id, include_view: true, day: config.include_explanation ? config.day : "today", ...(config.include_explanation ? { include_explanation: true } : {}), ...(config.roof_id ? { roof_id: config.roof_id } : {}) });
    if (!active()) return;
    view = validateView(data);
    if (view.day !== "today" && view.day_views?.today) view = { ...view, ...view.day_views.today };
    state.forecast = { status: "ready", data: view, envelope: data };
  } catch (error) {
    const message = /roof_not_found/.test(`${error?.translation_key ?? ""} ${error?.code ?? ""}`) ? "Die ausgewählte Dachfläche ist nicht mehr vorhanden." : error instanceof Error && (error.message === UPDATE_HINT || error.message.startsWith("Die Prognoseansicht")) ? error.message : sourceError(error, "Prognosedaten").message;
    publish({ ...state, loading: false, forecast: { ...sourceError(error, "Prognosedaten"), message, ...(/roof_not_found/.test(`${error?.translation_key} ${error?.code}`) ? { reason: "roof_removed" } : {}) } });
    return;
  }
  if (view.roof_id) {
    state.measurement = { status: "roof", message: "Keine Dachmessung zugeordnet." };
    state.history = { status: "roof", message: "Das Archiv bezieht sich auf die Gesamtanlage." };
    publish({ ...state, loading: false });
    return;
  }
  const midnight = millis(view.today_start) === millis(view.as_of);
  state.measurement = midnight ? { status: "empty", message: "Der heutige Messtag beginnt gerade." } : { status: "loading" };
  state.history = { status: "loading" };
  publish({ ...state });
  const measurement = midnight ? Promise.resolve() : (async () => {
    try {
      const data = await read("get_measurements", {
        config_entry_id: config.config_entry_id, start: view.today_start, end: view.as_of, include_outlook: true,
        ...(view.day === "today" && view.intervals.length ? { interval_windows: view.intervals.map(({ start, end }) => ({ start, end })) } : {}),
      });
      state.measurement = { status: "ready", data };
    } catch (error) { state.measurement = sourceError(error, "Messdaten"); }
    if (active()) publish({ ...state });
  })();
  const history = (async () => {
    try {
      const data = await read("get_history", { config_entry_id: config.config_entry_id, days: 7, current_targets: true });
      state.history = data.current_targets?.view_version === 1 && Array.isArray(data.current_targets.intervals) ? { status: "ready", data } : { status: "error", reason: "version", message: UPDATE_HINT };
    } catch (error) { state.history = sourceError(error, "Archivdaten"); }
    if (active()) publish({ ...state });
  })();
  await Promise.all([measurement, history]);
  if (active()) publish({ ...state, loading: false });
}

export function plotGeometry(view, width = 600) {
  const left = 34, right = 12, top = 16, bottom = 198;
  const start = millis(view.start), end = millis(view.end);
  return { width, left, right, top, bottom, x: (time) => left + (millis(time) - start) / (end - start) * (width - left - right) };
}

/** Volle lokale Stunden auf der absoluten Achse, auch bei halbstündiger DST. */
export function hourMarkers(view) {
  const minute = new Intl.DateTimeFormat("en-GB", { timeZone: view.timezone, minute: "2-digit" });
  const markers = [];
  const end = millis(view.end);
  // UTC-Minuten durchlaufen: lokale Stunden weder erfinden noch doppelte
  // Uhrzeiten zusammenlegen. Intervallgrenzen können auf halben Stunden liegen.
  for (let instant = Math.ceil(millis(view.start) / 60_000) * 60_000; instant <= end; instant += 60_000) {
    if (Number(minute.format(instant)) === 0) markers.push(new Date(instant).toISOString());
  }
  return markers;
}

/** Stufen zeigen die gelieferte Intervallenergie. Fehlstellen beginnen neue Pfade. */
export function seriesPaths(intervals, x, y, completeKey = null) {
  const paths = [];
  let path = "", previousEnd = null;
  for (const item of intervals) {
    const valid = validInterval(item) && finite(item.energy_kwh) && item.energy_kwh >= 0 && (!completeKey || item[completeKey] !== false);
    if (!valid) { if (path) paths.push(path); path = ""; previousEnd = null; continue; }
    const start = x(item.start), end = x(item.end), height = y(item.energy_kwh);
    if (path && millis(item.start) === previousEnd) path += ` L${start},${height} L${end},${height}`;
    else { if (path) paths.push(path); path = `M${start},${height} L${end},${height}`; }
    previousEnd = millis(item.end);
  }
  if (path) paths.push(path);
  return paths;
}

export function selectedSeries(state) {
  const view = state.forecast?.data;
  if (!view) return { forecast: [], history: [], actual: [] };
  return {
    forecast: view.intervals.filter((item) => overlap(item, view)),
    ...(state.showRaw && !view.roof_id && currentExplanation(state)?.status === "available" ? { raw: currentExplanation(state).raw_intervals.filter((item) => overlap(item, view)) } : {}),
    history: view.roof_id ? [] : (state.history?.data?.current_targets?.intervals ?? []).filter((item) => overlap(item, view)),
    actual: view.roof_id || (view.day !== "today" && !view.historical) ? [] : (state.measurement?.data?.total_intervals ?? []).filter((item) => overlap(item, view)),
  };
}

export function tableRows(state) {
  const series = selectedSeries(state);
  const rows = new Map();
  for (const [name, items] of Object.entries(series)) for (const item of items) {
    const key = `${millis(item.start)}/${millis(item.end)}`;
    if (!rows.has(key)) rows.set(key, { start: item.start, end: item.end });
    const complete = ["forecast", "raw"].includes(name) ? item.is_complete !== false : name === "actual" ? item.energy_complete === true : true;
    rows.get(key)[name] = complete ? item.energy_kwh : null;
    if (name === "actual" && !complete && finite(item.observed_energy_kwh) && item.observed_energy_kwh >= 0) rows.get(key).actual_observed = item.observed_energy_kwh;
  }
  return [...rows.values()].sort((a, b) => millis(a.start) - millis(b.start) || millis(a.end) - millis(b.end));
}

export const intervalKey = (row) => `${row.start}|${row.end}`;

/** Reine Auswahl vorhandener UTC-Intervalle, keine Interpolation von Energie. */
export function intervalAtPosition(state, fraction) {
  const view = state?.forecast?.data;
  if (!view || !finite(fraction) || fraction < 0 || fraction > 1) return null;
  const rows = tableRows(state);
  const instant = millis(view.start) + fraction * (millis(view.end) - millis(view.start));
  return (fraction === 1 ? rows.at(-1) : rows.find((row) => millis(row.start) <= instant && millis(row.end) > instant)) ?? null;
}

export function intervalDetails(state, key) {
  const row = tableRows(state).find((item) => intervalKey(item) === key);
  if (!row) return null;
  const series = selectedSeries(state);
  return { ...row, sources: Object.fromEntries(Object.entries(series).map(([name, items]) => {
    const item = items.find((value) => millis(value.start) === millis(row.start) && millis(value.end) === millis(row.end));
    const complete = item && (["forecast", "raw"].includes(name) ? item.is_complete !== false : name === "actual" ? item.energy_complete === true : true);
    return [name, { status: !item ? "missing" : complete && finite(item.energy_kwh) ? "complete" : "incomplete", energy_kwh: complete && finite(item.energy_kwh) ? item.energy_kwh : null, observed_energy_kwh: name === "actual" && !complete && finite(item?.observed_energy_kwh) && item.observed_energy_kwh >= 0 ? item.observed_energy_kwh : null, quality_flags: item?.quality_flags ?? [] }];
  })) };
}

export function renderIntervalDetails(state, key) {
  if (!tableRows(state).length) return '<p id="chart-help" class="hint">Noch keine Intervalle zum Erkunden vorhanden.</p>';
  const detail = intervalDetails(state, key);
  if (!detail) return '<button id="chart-explore" class="reset-button" data-interval-action="first">Intervalle erkunden</button><p id="chart-help" class="hint">Tippe auf den Verlauf oder wähle ein Intervall mit den Pfeiltasten. Die Tabelle enthält dieselben Werte.</p>';
  const view = state.forecast.data;
  // Die Detailquellen verwenden bereits dieselbe Zulässigkeit wie die Kurven.
  return `<section id="interval-detail" data-start="${escapeHtml(detail.start)}" data-end="${escapeHtml(detail.end)}" class="interval-detail" aria-label="Ausgewähltes Intervall"><h3>${escapeHtml(plantStamp(detail.start, view.timezone))}<br>bis ${escapeHtml(plantStamp(detail.end, view.timezone))}</h3><p class="hint">${escapeHtml(view.timezone)} · kWh je tatsächlichem Intervall</p><dl class="interval-values">${Object.entries({ ...(view.historical ? {} : { forecast: "Aktuelle Prognose", ...(detail.sources.raw ? { raw: "Grundmodell ohne Selbstkalibrierung" } : {}) }), history: view.historical ? view.label : ARCHIVE_LABEL, actual: "Messung" }).map(([name, label]) => {
    const source = detail.sources[name] ?? { status: "missing", quality_flags: [] };
    const actualValue = source.energy_kwh ?? source.observed_energy_kwh;
    const value = name === "actual" ? `${energyText(actualValue)}${finite(actualValue) ? " kWh" : ""}` : source.status === "complete" ? `${energyText(source.energy_kwh)} kWh · vollständig` : source.status === "incomplete" ? "— · unvollständig" : "— · nicht vorhanden";
    return `<div><dt>${label}</dt><dd>${value}${name !== "actual" && source.quality_flags.some((flag) => flag !== "derived_energy") ? '<span class="hint">Qualitätsmarkierungen vorhanden</span>' : ""}</dd></div>`;
  }).join("")}</dl><div class="interval-controls"><button id="interval-prev" data-interval-action="previous" aria-label="Vorheriges Intervall">Zurück</button><button id="interval-next" data-interval-action="next" aria-label="Nächstes Intervall">Weiter</button><button id="interval-close" data-interval-action="close">Schließen</button></div><p id="chart-help" class="hint">Pfeiltasten: Intervall wechseln. Escape: Detailansicht schließen.</p></section>`;
}

function renderChart(state, width, selectedKey) {
  const view = state.forecast.data;
  const selectedSeriesData = selectedSeries(state);
  // Nur die beiden sichtbaren Reihen bestimmen Kurven und Achsenskalierung.
  const series = {
    forecast: view.historical ? selectedSeriesData.history : selectedSeriesData.forecast,
    actual: selectedSeriesData.actual.map((item) => ({
      ...item, energy_kwh: item.energy_complete === true ? item.energy_kwh : item.observed_energy_kwh,
    })),
  };
  const { left, top, bottom, x } = plotGeometry(view, width);
  const values = Object.values(series).flat().filter((item) => item.is_complete !== false && finite(item.energy_kwh) && item.energy_kwh >= 0).map((item) => item.energy_kwh);
  const max = Math.max(1, ...values) * 1.15;
  const y = (value) => bottom - value / max * (bottom - top);
  const paths = (items, className, completeKey) => seriesPaths(items, x, y, completeKey).map((path) => `<path class="${className}" d="${path}"/>`).join("");
  const grid = [0, 0.5, 1].map((part) => `<line class="grid" x1="${left}" x2="${width - 12}" y1="${y(max * part)}" y2="${y(max * part)}"/><text class="axis" x="${left - 7}" y="${y(max * part) + 4}" text-anchor="end">${energyText(max * part)}</text>`).join("");
  const tickCount = width < 440 ? 3 : 5;
  const hourTicks = hourMarkers(view).map((instant) => `<line class="hour-tick" x1="${x(instant)}" x2="${x(instant)}" y1="${bottom}" y2="${bottom + 5}"/>`).join("");
  const ticks = Array.from({ length: tickCount }, (_, index) => {
    const instant = new Date(millis(view.start) + (millis(view.end) - millis(view.start)) * index / (tickCount - 1)).toISOString();
    const label = formatPlantTime(instant, view.timezone, false);
    const offset = formatPlantTime(instant, view.timezone).split(" ").at(-1);
    return `<text class="axis" x="${x(instant)}" y="219" text-anchor="${index === 0 ? "start" : index === tickCount - 1 ? "end" : "middle"}">${label}<tspan x="${x(instant)}" dy="14">${offset}</tspan></text>`;
  }).join("");
  const now = millis(view.as_of) >= millis(view.start) && millis(view.as_of) < millis(view.end) ? `<line class="now" x1="${x(view.as_of)}" x2="${x(view.as_of)}" y1="8" y2="${bottom}"/><text class="now-label" x="${Math.min(width - 32, Math.max(left + 15, x(view.as_of)))}" y="10" text-anchor="middle">Jetzt</text>` : "";
  const gaps = series.forecast.filter((item) => item.is_complete === false || !finite(item.energy_kwh)).map((item) => `<rect class="gap" x="${x(item.start)}" y="${top}" width="${x(item.end) - x(item.start)}" height="${bottom - top}"/>`).join("");
  const selected = tableRows(state).find((row) => intervalKey(row) === selectedKey);
  const highlight = selected ? `<rect class="selected-interval" x="${x(selected.start)}" y="${top}" width="${x(selected.end) - x(selected.start)}" height="${bottom - top}"/>` : "";
  return `<svg id="interval-chart" class="chart" viewBox="0 0 ${width} 242" tabindex="0" role="group" aria-roledescription="Interaktives Diagramm" aria-labelledby="chart-title" aria-describedby="chart-description chart-help">
    <title id="chart-title">Energie je Intervall in kWh</title><desc id="chart-description">${view.historical ? "Archivierte Prognose: " + escapeHtml(view.label) : "Aktuelle Prognose"} durchgezogen, tatsächliche Produktion gestrichelt. Fehlende Werte bleiben leer. Alle Werte stehen auch in der Tabelle.</desc>
    <defs><clipPath id="plot-clip"><rect x="${left}" y="0" width="${width - left - 12}" height="${bottom + 2}"/></clipPath></defs>
    ${grid}<g clip-path="url(#plot-clip)">${gaps}${highlight}${paths(series.forecast, "forecast-line", "is_complete")}${paths(series.actual, "actual-line")}${now}</g><g aria-hidden="true" class="hour-ticks">${hourTicks}</g>${ticks}
    ${values.length ? "" : `<text class="empty-plot" x="${width / 2}" y="100" text-anchor="middle">Noch keine Intervallwerte</text>`}
  </svg>`;
}

/** Nur vorübergehende Ausfälle bewahren denselben gelesenen Stand samt Zeitbasis. */
export function retainReadState(previous, incoming) {
  const state = { ...incoming };
  for (const key of ["forecast", "measurement", "history"]) {
    const next = state[key], old = previous?.[key];
    if (!old?.data) continue;
    if (key !== "forecast") {
      const before = previous?.forecast?.data, after = state.forecast?.data;
      // Relative Tageswerte gehören zur damaligen lokalen Tagesbasis, auch
      // während nur die neue Prognose und noch keine optionalen Antworten da sind.
      if (!before || !after || before.timezone !== after.timezone
        || millis(before.today_start) !== millis(after.today_start)
        || millis(before.today_end) !== millis(after.today_end)) continue;
    }
    if (["loading", "idle"].includes(next?.status)) state[key] = old;
    else if (next?.reason === "unavailable") state[key] = { ...old, ...next, retained: true };
  }
  // Ohne lesbare Prognose dürfen abhängige alte Quellen nicht stehen bleiben.
  if (state.forecast && !state.forecast.data) {
    state.measurement = incoming.measurement;
    state.history = incoming.history;
  }
  return state;
}

export function dataNotices(state) {
  const result = [];
  const add = (id, level, title, text, help = "") => result.push({ id, level, title, text, help });
  const view = state?.forecast?.data;
  for (const [key, label] of [["forecast", "Prognose"], ["measurement", "Messdaten"], ["history", "Archiv"]]) {
    const section = state?.[key];
    if (!section || section.status === "idle") continue;
    if (section.status === "loading") add(`${key}-loading`, "Information", `${label} wird geladen`, "Die vorhandenen lokalen Daten werden gelesen. Bitte kurz warten.");
    else if (section.reason === "permission") add("permission", "Fehler", "Leseberechtigung fehlt", "Die benötigten Quellen dürfen nicht gelesen werden.", "Eine Person mit Administrationsrechten kann die Leserechte der zugeordneten Sensoren prüfen.");
    else if (section.reason === "version") add("version", "Fehler", "Datenversion nicht kompatibel", UPDATE_HINT);
    else if (section.status === "error") add(`${key}-error`, "Fehler", `${label} derzeit nicht erreichbar`, section.message, section.retained ? "Der letzte lesbare Stand bleibt sichtbar. Die nächste reguläre Aktualisierung versucht es erneut." : "HA-Verbindung und Integrationsstatus prüfen. Die nächste reguläre Aktualisierung versucht es erneut.");
    else if (section.status === "empty") add(`${key}-empty`, "Information", `${label} noch nicht verfügbar`, section.message, "Einrichtung und Integrationsstatus in Home Assistant prüfen.");
  }
  if (!state) add("forecast-loading", "Information", "Prognose wird geladen", "Die vorhandenen lokalen Daten werden gelesen. Bitte kurz warten.");
  if (view) {
    if (state.selectionPending) add("selection", "Information", "Auswahl wird geladen", "Bis dahin sind noch die bisherigen Werte sichtbar.");
    if (view.stale) add("stale", "Einschränkung", "Prognosestand veraltet", "Der letzte verfügbare Stand bleibt sichtbar; seine Zeitangabe steht am Tagesverlauf.", "Integrationsstatus prüfen und den regulären Wetterabruf abwarten.");
    if (!view.complete) add("incomplete-forecast", "Einschränkung", "Prognose unvollständig", "Schattierte Lücken sind fehlende Daten und kein null Ertrag.");
    if (view.roof_id) add("roof", "Information", "Nur Dachprognose", "Keine Dachmessung zugeordnet. Das Archiv bezieht sich auf die Gesamtanlage.", "Für Messung und Archiv die Gesamtanlage auswählen.");
    else if (state.measurement?.status === "ready") {
      const total = state.measurement.data.current_location_total_energy ?? state.measurement.data.total_energy;
      if (total?.source_count === 0) add("no-source", "Information", "Keine Messquelle zugeordnet", "Die Prognose funktioniert auch ohne Messung.", "Optional in den Integrationsoptionen eine bestätigte AC-PV-Messquelle zuordnen; bei fehlenden Rechten die Administration darum bitten.");
      else if (!finite(total?.energy_kwh)) add("measurement-empty", "Information", "Noch keine Messung", "Für heute sind noch keine Messwerte verfügbar.", "Messquelle prüfen und weitere Zählerstände abwarten.");
    }
    if (state.history?.status === "ready") {
      if (!state.history.data.enabled) add("archive-off", "Information", "Archiv ausgeschaltet", "Es werden keine Prognosestände archiviert.", "Optional das Prognosearchiv in den Integrationsoptionen aktivieren lassen.");
      else if (!selectedSeries(state).history.length) add("archive-empty", "Information", "Archiv noch leer", "Für diesen Tag sind noch keine Stundenstände eingefroren.", "Das Archiv sammelt rechtzeitig beobachtete Stände ab seiner Aktivierung. Frühere Prognosen werden nicht ergänzt.");
    }
    const flags = view.intervals.some((item) => item.quality_flags?.length) || selectedSeries(state).history.some((item) => item.quality_flags?.length);
    if (flags) add("quality", "Einschränkung", "Qualitätsmarkierungen vorhanden", "Betroffene Intervalle sind in der Detailansicht gekennzeichnet. Eingabemängel sind keine gemessene Prognosegüte.");
    if (view.horizon_shading?.active) add("experimental", "Information", "Experimentelles Horizontprofil aktiv", "Direktlicht wird geometrisch abgeschattet, ein diffuser Rest bleibt erhalten. Eine bessere Prognosegüte ist noch nicht belegt.");
  }
  // Berechtigungs- und Versionsfehler mehrerer Quellen erhalten einen gemeinsamen Hinweis.
  return result.filter((item, index) => result.findIndex((other) => other.id === item.id) === index);
}

function renderNotices(state) {
  return dataNotices(state).map((item) => `<section class="notices data-notice" data-notice="${item.id}"><h3>${item.level} · ${escapeHtml(item.title)}</h3><p>${escapeHtml(item.text)}</p>${item.help ? `<p>${escapeHtml(item.help)}</p>` : ""}</section>`).join("");
}

function renderTable(state) {
  const view = state.forecast.data;
  const rows = tableRows(state);
  return `<details id="values"><summary id="values-toggle">Intervallwerte anzeigen <span>${rows.length} Intervalle</span></summary><p class="hint">kWh je angegebenem Intervall. „—“ bedeutet fehlend; 0 ist ein gültiger Wert. Zeitangaben gelten für ${escapeHtml(view.timezone)}.</p><div class="table-scroll" tabindex="0" role="region" aria-label="Intervallwerte, horizontal scrollbar"><table><caption class="sr-only">Intervallenergie in kWh</caption><thead><tr><th scope="col">Zeit</th><th scope="col">Prognose</th><th scope="col">1 Stunde<br>vorher</th><th scope="col">Ist</th>${state.showRaw && !view.roof_id ? '<th scope="col">Grundmodell ohne Selbstkalibrierung</th>' : ""}</tr></thead><tbody>${rows.map((row) => `<tr><th scope="row"><time datetime="${escapeHtml(row.start)}">${escapeHtml(formatPlantTime(row.start, view.timezone))}</time><span class="until">bis ${escapeHtml(formatPlantTime(row.end, view.timezone))}</span></th><td>${energyText(row.forecast)}</td><td>${energyText(row.history)}</td><td>${energyText(row.actual ?? row.actual_observed)}</td>${state.showRaw && !view.roof_id ? `<td>${energyText(row.raw)}</td>` : ""}</tr>`).join("")}</tbody></table></div></details>`;
}

function renderShortTerm(report) {
  if (!report) return "";
  if (report.schema_version !== 1 || report.rule_version !== 1) return '<p class="hint">Kurzfristiger Vergleich: unbekannte Datenversion.</p>';
  const labels = { hourly_1h: "Eine Stunde Vorlauf", hourly_3h: "Drei Stunden Vorlauf", daily_remaining_12: "Resttag ab 12 Uhr" };
  return `<h3>Kurzfristiger Vergleich</h3><p class="hint">${report.enabled ? "Beobachtung aktiviert" : "Beobachtung ausgeschaltet"}. Produktive Prognose unverändert. Eigenes Prüffenster: ${escapeHtml(report.window_days)} abgeschlossene Tage, mindestens ${escapeHtml(report.minimum_days)} belegte Tage je Horizont.</p>${Object.entries(labels).map(([key, label]) => {
    const value = report.horizons?.[key];
    if (!value) return "";
    return `<p class="hint"><strong>${label}</strong>: ${escapeHtml(value.days)} Tage, ${escapeHtml(value.count)} Messpaare.<br>MAE Basis ${energyText(value.baseline_mae_kwh)} kWh; Kandidat ${energyText(value.candidate_mae_kwh)} kWh. Bias Basis ${energyText(value.baseline_bias_kwh)} kWh; Kandidat ${energyText(value.candidate_bias_kwh)} kWh.<br>${value.criterion_met ? "Vorab festgelegtes Prüfziel erreicht; weiterhin nur Beobachtung." : "Noch kein ausreichender Nutzennachweis."}</p>`;
  }).join("")}`;
}

function renderTemperatureComparison(report) {
  if (!report) return "";
  if (report.schema_version !== 1 || report.model !== "ross_comparison_v1") return '<p class="hint">Temperaturvergleich: unbekannte Datenversion.</p>';
  const labels = { daily_previous_18: "Tagesstand vom Vortag, 18 Uhr", daily_same_06: "Tagesstand von 06 Uhr", hourly_1h: "Eine Stunde Vorlauf", hourly_3h: "Drei Stunden Vorlauf" };
  return `<h3>Temperaturvergleich</h3><p class="hint">${report.enabled ? "Beobachtung aktiviert" : "Beobachtung ausgeschaltet"}. Ross-Näherung mit gewählten Literaturannahmen; keine gemessene Zelltemperatur. Rohmodelle ohne übertragene Kalibrierung, gleiche Messpaare aus ${escapeHtml(report.window_days)} abgeschlossenen Tagen. Produktive Prognose unverändert.</p>${report.parameter_id ? Object.entries(labels).map(([key, label]) => {
    const value = report.horizons?.[key];
    return value ? `<p class="hint"><strong>${label}</strong>: ${escapeHtml(value.days)} Tage, ${escapeHtml(value.count)} Messpaare.<br>MAE Rohmodell ${energyText(value.raw_mae_kwh)} kWh; Ross ${energyText(value.alternative_mae_kwh)} kWh. Bias Rohmodell ${energyText(value.raw_bias_kwh)} kWh; Ross ${energyText(value.alternative_bias_kwh)} kWh.</p>` : "";
  }).join("") : '<p class="hint">Zuerst eine Vergleichsannahme für jede Dachfläche wählen.</p>'}`;
}

export function renderUnderperformance(report) {
  if (!report || report.schema_version !== 1 || (report.status === "off" && !report.learning_paused)) return "";
  const active = report.status === "active";
  const changed = report.status === "reference_changed";
  const text = report.status === "off" ? "Beobachtung ausgeschaltet. Ein vorhandener Hinweis hält das Lernen bis zum bewussten Löschen weiter an." : active ? "Wiederkehrende Abweichung zur geprüften Rohmodellbasis. Das ist keine Defektdiagnose." : changed ? "Die ursprüngliche Vergleichsgrundlage hat sich geändert oder fehlt. Eine Erholung ist damit nicht belegt." : report.status === "clear" ? "Die sieben zuletzt vollständig belegten Tage erfüllen die Hinweisregel nicht." : "Noch keine belastbare Vergleichsfolge: benötigt sieben vollständige Tage und eine zuvor bestandene Prüfung mit 60 Trainings- und 30 Prüftagen.";
  return `<section class="notices" aria-label="Experimentelle Minderertragsprüfung"><h3>Experimentelle Minderertragsprüfung</h3><p>${text}</p>${active || changed ? `<p>${escapeHtml(report.first_day)} bis ${escapeHtml(report.last_day)} · Gesamtanlage</p>` : ""}${active ? `<p>Rohprognose ${energyText(report.raw_kwh)} kWh · Messung ${energyText(report.actual_kwh)} kWh. Abweichung ${energyText(report.difference_kwh)} kWh (${energyText(report.shortfall_fraction * 100)} %). Vollständige Abdeckung: ${energyText(report.coverage_fraction * 100)} %. ${escapeHtml(report.below_days)} von 7 Tagen deutlich unter der Untergrenze.</p><p>Feste Vergleichsbasis: ${escapeHtml(report.comparison?.training_count)} Trainings- und ${escapeHtml(report.comparison?.validation_count)} spätere Prüftage; beobachtete Bandabdeckung ${energyText(report.comparison?.evaluation?.coverage_fraction * 100)} %. Sie ist keine Sicherheitsgarantie. Zuvor verwendeter Lernfaktor: ${energyText(report.accepted_factor)}.</p>` : ""}${report.learning_paused ? `<p>Lernen und Kandidatenprüfung pausieren; die Kalibrierung verwendet Faktor 1. ${report.acknowledged ? "Hinweis quittiert." : "Quittieren und bewusstes Löschen: Integrationsoptionen → Prognosearchiv → Prüfhinweis."}</p>` : ""}${active || changed ? "<p>Messquelle und Wechselrichterstatus prüfen; bekannte Abregelung oder Anlagenänderung berücksichtigen. Wetterabweichungen, Schnee und Verschattung sind mögliche Erklärungen. Aus dem Gesamtzähler folgt keine Dachdiagnose.</p>" : ""}<p>Reale Trefferquote und Fehlalarmrate sind noch nicht belegt.</p></section>`;
}

export function renderReport(report, days) {
  if (!report) return `<p class="hint">Bericht wird geladen …</p>`;
  if (report.message) return `<p class="hint">${escapeHtml(report.message)}</p>`;
  const data = report.data;
  const metrics = data?.horizons?.hourly_1h;
  if (!metrics) return `<p class="hint">Noch keine abgeschlossenen Zielintervalle im Archiv.</p>`;
  return `<p class="hint">${days} abgeschlossene lokale Tage · ${ARCHIVE_LABEL}. Nur vollständig belegte, vergleichbare Intervalle gehen in die Fehlermaße ein.</p><dl class="report-metrics"><div><dt>MAE</dt><dd>${energyText(metrics.mae_kwh)} <small>kWh</small></dd></div><div><dt>Bias</dt><dd>${energyText(metrics.bias_kwh)} <small>kWh</small></dd></div><div><dt>Stichprobe</dt><dd>${escapeHtml(metrics.count_valid ?? 0)} <small>Intervalle</small></dd></div><div><dt>Abdeckung</dt><dd>${finite(metrics.coverage) ? energyText(metrics.coverage * 100) : "—"} <small>%</small></dd></div></dl><p class="hint">MAE: mittlerer absoluter Fehler. Bias: Prognose minus Messung; positive Werte bedeuten Überschätzung.${data.retention_truncated ? " Die Aufbewahrungsgrenze hat ältere Daten gekürzt." : ""}${data.enabled === false ? " Die Erfassung ist pausiert." : ""}</p>${renderShortTerm(data.short_term)}${renderTemperatureComparison(data.temperature_comparison)}`;
}

const plantStamp = (value, timezone) => finite(millis(value)) ? `${formatPlantDate(value, timezone)}, ${formatPlantTime(value, timezone)}` : "unbekannt";

export function renderOutlook(state) {
  const view = state.forecast.data;
  const timezone = view.timezone;
  const outlook = state.measurement?.data?.outlook;
  const currentDay = outlook?.timezone === timezone && millis(outlook.as_of) >= millis(state.forecast.data.today_start) && millis(outlook.as_of) < millis(state.forecast.data.today_end);
  const supported = currentDay && outlook?.schema_version === 1;
  const available = supported && outlook.status === "available" && finite(outlook.total_kwh);
  const supportedEstimate = supported && outlook.estimate?.schema_version === 1 ? outlook.estimate : null;
  const estimate = supportedEstimate?.status === "available" && finite(supportedEstimate.total_kwh) ? supportedEstimate : null;
  const measurementAge = supported && finite(outlook.measurement_age_minutes) && outlook.measurement_age_minutes >= 0 ? `<p class="hint">Alter des gemeinsamen Messzeitpunkts: ${energyText(outlook.measurement_age_minutes)} Minuten.</p>` : "";
  const staleMeasurement = supported && outlook.measurement_stale === true ? '<p class="hint"><strong>Der letzte gesicherte Messwert ist zu alt.</strong> Mindestens eine Messquelle hat ihre bestätigte Meldefrist überschritten. Die Zeit seit dem gemeinsamen Messzeitpunkt bleibt geschätzt.</p>' : "";
  const metric = (label, value) => `<div><dt>${label}</dt><dd>${energyText(value)} <small>kWh</small></dd></div>`;
  if (estimate || !available) {
    const total = estimate?.total_kwh ?? view.summary.today_kwh;
    const hasTotal = finite(total);
    const measured = estimate?.basis === "measurements_and_forecast";
    const fallbackReason = supportedEstimate?.measurement_fallback_reason;
    const identityUnresolved = supported && (outlook.reason === "unresolved_measurement_identity" || fallbackReason === "unresolved_measurement_identity");
    const identityHint = identityUnresolved ? '<p class="hint">Die Zuordnung mindestens einer Messquelle ist derzeit nicht sicher bestätigt. Bitte in den Integrationsoptionen unter „PV-Erzeugung“ prüfen und bei geänderter Quelle erneut bestätigen.</p>' : "";
    const fallbackExplanations = {
      no_energy_sources: "Es ist keine Energiequelle für die Tagesaussicht zugeordnet.",
      no_usable_measurements: "Für mindestens eine Messquelle liegen noch keine verwendbaren Messabschnitte vor.",
      no_common_measurement_boundary: "Die Messquellen liefern Werte, aber keine gemeinsam belegten Zeitabschnitte, etwa wegen versetzter Meldezeiten oder Messlücken. Die gemessene Energie steht weiterhin unter „Ist heute“.",
    };
    const fallbackExplanation = Object.hasOwn(fallbackExplanations, fallbackReason) ? fallbackExplanations[fallbackReason] : null;
    const explanation = measured
      ? "Vorhandene Messwerte sind berücksichtigt. Für die übrigen Zeiten wird die Prognose verwendet."
      : `Die Tagesaussicht basiert auf der Wetterprognose.${identityUnresolved ? "" : ` ${fallbackExplanation ?? "Verwertbare Messwerte werden automatisch berücksichtigt, sobald sie vorliegen."}`}`;
    const stale = estimate?.forecast_stale || view.stale || state.forecast.retained || (supported && outlook.reason === "stale_forecast");
    const todayIntervals = view.day_views?.today?.intervals ?? (view.day === "today" ? view.intervals : []);
    const quality = estimate ? estimate.forecast_quality_flags?.length : todayIntervals?.some((interval) => interval.quality_flags?.length);
    return `<details id="outlook"><summary id="outlook-toggle">Tagesaussicht für heute <span>${hasTotal ? `${energyText(total)} kWh` : "Keine Prognosedaten"}</span></summary>${hasTotal ? `<p class="feature-result">Heute voraussichtlich insgesamt <strong>${energyText(total)} kWh</strong></p><p class="hint">${explanation}</p>${estimate ? `<dl class="report-metrics">${measured ? metric("Berücksichtigte Messung", estimate.measured_kwh) : ""}${metric("Bisheriger Tag geschätzt", estimate.estimated_past_kwh)}${metric("Rest ab jetzt", estimate.remaining_kwh)}</dl>` : ""}${stale ? '<p class="hint">Letzter verfügbarer Prognosestand: Der Wetterabruf ist nicht aktuell. Die Tagesaussicht wird beim nächsten erfolgreichen Abruf aktualisiert.</p>' : ""}${quality ? '<p class="hint">Die Wetterprognose verwendet teilweise Ersatzwerte.</p>' : ""}` : '<p class="hint">Für heute fehlen auch Prognosewerte für benötigte Zeitabschnitte. Sobald diese vorliegen, erscheint die Tagesaussicht automatisch.</p>'}${identityHint}${measurementAge}${staleMeasurement}</details>`;
  }
  return `<details id="outlook"><summary id="outlook-toggle">Tagesaussicht für heute <span>${energyText(outlook.total_kwh)} kWh</span></summary><p class="feature-result">Heute voraussichtlich insgesamt <strong>${energyText(outlook.total_kwh)} kWh</strong></p><dl class="report-metrics">${metric("Gesichert gemessen", outlook.measured_kwh)}${metric("Geschätzt seit letzter Messung", outlook.bridge_kwh)}${metric("Rest ab jetzt", outlook.remaining_kwh)}</dl><p class="hint">Messung bis ${escapeHtml(plantStamp(outlook.measured_until, timezone))}. Die Zeit seit dieser Messung bleibt eine Schätzung. Rest ab jetzt und geschätzte Brücke überschneiden sich nicht. Kurzfristige Korrektur ist aus.</p>${outlook.quality_flags?.length ? '<p class="hint">Die Tagesaussicht enthält Qualitätsmarkierungen; sie ist keine zugesagte Erzeugung.</p>' : ""}${measurementAge}${staleMeasurement}</details>`;
}

function renderHourlyBands(uncertainty, view) {
  if (uncertainty?.schema_version !== 1 || !Array.isArray(uncertainty.frozen_hours)) return "";
  const hours = uncertainty.frozen_hours.filter((band) => band.target_date === view.date);
  if (!hours.length) return "";
  return `<h3>Eingefrorene zukünftige Stunden</h3>${hours.map((band) => {
    const available = band.rule_version === 2 && band.status === "available" && [band.lower_kwh, band.central_kwh, band.upper_kwh].every(finite);
    const lead = band.horizon === "hourly_1h" ? "1 Stunde" : band.horizon === "hourly_3h" ? "3 Stunden" : null;
    if (!lead) return "";
    return `<p class="hint"><strong>${escapeHtml(plantStamp(band.start, view.timezone))} bis ${escapeHtml(plantStamp(band.end, view.timezone))}</strong><br>Stand ${lead} vorher: ${available ? `${energyText(band.lower_kwh)}–${energyText(band.upper_kwh)} kWh; damalige Prognose ${energyText(band.central_kwh)} kWh.` : "Bandbreite noch nicht belastbar."}<br>${escapeHtml(band.training_count ?? 0)} Lerntage, ${escapeHtml(band.validation_count ?? 0)} spätere Prüftage. ${available && finite(band.evaluation?.coverage_fraction) ? `Zielabdeckung ${energyText(band.target_coverage * 100)} %, Prüfdeckung ${energyText(band.evaluation.coverage_fraction * 100)} %, mittlere Breite ${energyText(band.evaluation.mean_width_kwh)} kWh. Winkler-Score ${energyText(band.evaluation.winkler_score_kwh)} kWh (Referenz ${energyText(band.evaluation.reference_winkler_score_kwh)} kWh).` : ""}${available && finite(band.evaluation?.coverage_wilson95?.lower) && finite(band.evaluation?.coverage_wilson95?.upper) ? `<br>95-%-Wilson-Intervall der Prüfdeckung: ${energyText(band.evaluation.coverage_wilson95.lower * 100)}–${energyText(band.evaluation.coverage_wilson95.upper * 100)} %. Nur indikativ unter der Annahme unabhängiger Tage.` : ""}<br>Stichtag ${escapeHtml(plantStamp(band.cutoff, view.timezone))}. Eine Stunde Energie, keine Summe des Vorlaufs. Keine Garantie.</p>`;
  }).join("")}`;
}

export function renderUncertainty(state) {
  const view = state.forecast.data;
  const uncertainty = state.history?.data?.uncertainty;
  const band = uncertainty?.days?.[view.day];
  const available = uncertainty?.schema_version === 1 && uncertainty.timezone === view.timezone && band?.target_date === view.date && band.status === "available" && [band.lower_kwh, band.central_kwh, band.upper_kwh].every(finite);
  const checkpoint = band?.horizon === "daily_same_06" ? "06 Uhr am Zieltag" : "18 Uhr am Vortag";
  const evaluation = band?.evaluation;
  const wilson = evaluation?.coverage_wilson95;
  const evaluationText = [
    finite(evaluation?.mean_width_kwh) ? `Mittlere Bandbreite in der Prüfung: ${energyText(evaluation.mean_width_kwh)} kWh.` : "",
    finite(wilson?.lower) && finite(wilson?.upper) ? `95-%-Wilson-Intervall der Prüfdeckung: ${energyText(wilson.lower * 100)}–${energyText(wilson.upper * 100)} %. Nur ein Anhaltspunkt unter der Annahme unabhängiger Tage; aufeinanderfolgendes Wetter kann diese Annahme verletzen.` : "",
  ].filter(Boolean).join(" ");
  return `<details id="uncertainty"><summary id="uncertainty-toggle">Erfahrungsband <span>${available ? "Eingefrorener Stand" : "Noch nicht belastbar"}</span></summary>${available ? `<p class="feature-result">${energyText(band.lower_kwh)}–${energyText(band.upper_kwh)} kWh</p><p class="hint">Zum eingefrorenen Tageswert von <strong>${energyText(band.central_kwh)} kWh</strong> (${checkpoint}). Diese Basis ist unabhängig von der aktuellen Tagesprognose oben.</p><p class="hint">Stichtag ${escapeHtml(plantStamp(band.cutoff, view.timezone))}; Prognose beobachtet ${escapeHtml(plantStamp(band.forecast_observed_at, view.timezone))}. ${escapeHtml(band.training_count ?? 0)} Lerntage, ${escapeHtml(band.validation_count ?? 0)} Prüftage.${finite(band.target_coverage) ? ` Zielabdeckung: ${energyText(band.target_coverage * 100)} %.` : ""}${finite(band.evaluation?.coverage_fraction) ? ` Erreichte Prüfdeckung: ${energyText(band.evaluation.coverage_fraction * 100)} %.` : ""} Keine Garantie für den einzelnen Tag.</p>` : '<p class="hint">Bandbreite noch nicht belastbar. Es fehlen passende Daten, genügend spätere Prüftage oder eine bestandene Prüfung.</p>'}${available && evaluationText ? `<p class="hint">${evaluationText}</p>` : ""}${uncertainty?.retention_truncated ? '<p class="hint">Die Aufbewahrungsgrenze hat ältere Vergleichsdaten gekürzt.</p>' : ""}${renderHourlyBands(uncertainty, view)}<p class="hint">Für den gleitenden Resttag und die nächsten gleitenden 60 Minuten gibt es noch kein belastbares Erfahrungsband.</p></details>`;
}

export function planningChoices(state) {
  const view = state?.forecast?.data;
  if (!view) return [];
  const intervals = state.forecast.envelope?.intervals ?? view.intervals;
  const boundaries = intervals.filter(validInterval).flatMap(({ start, end }) => [millis(start), millis(end)]);
  if (intervals.some((item) => validInterval(item) && millis(item.start) <= millis(view.as_of) && millis(view.as_of) < millis(item.end))) boundaries.push(millis(view.as_of));
  return [...new Set(boundaries)].sort((left, right) => left - right).map((value) => new Date(value).toISOString());
}

export function renderPlanning(state, planningUI = {}) {
  const view = state.forecast.data;
  const choices = planningChoices(state);
  const inputs = planningUI.inputs ?? {};
  const optionList = (selected) => [ ...(finite(millis(selected)) && !choices.some((value) => millis(value) === millis(selected)) ? [selected] : []), ...choices ].map((value) => `<option value="${escapeHtml(value)}" ${millis(value) === millis(selected) ? "selected" : ""}>${escapeHtml(plantStamp(value, view.timezone))}</option>`).join("");
  const result = planningUI.result;
  const plan = result?.data;
  const usable = plan?.schema_version === 1 && ["available", "started", "completed"].includes(plan.status) && validInterval(plan) && (plan.status !== "available" || finite(plan.energy_kwh));
  const reason = {
    outside_forecast: "Die Auswahl liegt außerhalb der beiden aktuellen Prognosetage.",
    infeasible_window: "Die Laufdauer passt nicht mehr in das gewählte Zeitfenster.",
    incomplete_forecast: "Die Prognose deckt das gewählte Zeitfenster nicht vollständig ab.",
    input_fallbacks: "Die Wetterdaten enthalten Ersatzwerte; daraus entsteht keine neue Empfehlung.",
    no_solar_energy: "In diesem Zeitraum wird keine nutzbare PV-Energie erwartet.",
    no_energy: "In diesem Zeitraum wird keine nutzbare PV-Energie erwartet.",
    no_remaining_energy: "In diesem Zeitraum wird keine nutzbare PV-Energie erwartet.",
    no_feasible_window: "Die Laufdauer passt nicht in das gewählte Zeitfenster.",
    window_too_short: "Die Laufdauer passt nicht in das gewählte Zeitfenster.",
    incomplete_coverage: "Die Prognose deckt das gewählte Zeitfenster nicht vollständig ab.",
    stale_forecast: "Der Prognosestand ist für ein neues Zeitfenster zu alt.",
    forecast_unavailable: "Es liegt keine verwendbare Prognose für das Zeitfenster vor.",
  }[plan?.reason] ?? "Für diese Auswahl kann noch kein belastbares Solarzeitfenster angegeben werden.";
  const output = usable ? `<p class="feature-result">${escapeHtml(plantStamp(plan.start, view.timezone))}<br>bis ${escapeHtml(plantStamp(plan.end, view.timezone))}${finite(plan.energy_kwh) ? `<br><strong>${energyText(plan.energy_kwh)} kWh</strong> erwartet` : ""}</p><p class="hint">${plan.status === "started" ? "Dieses empfohlene Fenster läuft bereits und wird nicht automatisch verschoben." : plan.status === "completed" ? "Dieses empfohlene Fenster ist beendet." : "In diesem zusammenhängenden Fenster wird innerhalb deiner Auswahl besonders viel PV-Energie erwartet."}${plan.hysteresis_applied ? " Bei nur geringfügig geänderter Prognose bleibt die bisherige Empfehlung erhalten." : ""} Wetterabruf: ${escapeHtml(plantStamp(plan.fetched_at, view.timezone))}.</p>${plan.quality_flags?.length ? '<p class="hint">Die Prognose enthält Qualitätsmarkierungen. Das Zeitfenster bleibt eine Schätzung.</p>' : ""}` : result?.status === "loading" ? '<p class="hint">Zeitfenster wird berechnet …</p>' : result ? `<p class="hint">${escapeHtml(result.message ?? reason)}</p>` : '<p class="hint">Laufdauer und zulässigen Zeitraum wählen, dann bewusst berechnen.</p>';
  return `<details id="planning"><summary id="planning-toggle">Bestes Solarzeitfenster <span>Gesamtanlage</span></summary><form id="planning-form"><label>Laufdauer in Minuten<input id="planning-duration" name="duration_minutes" type="number" inputmode="numeric" min="1" max="2880" step="1" required value="${escapeHtml(inputs.duration_minutes ?? 120)}"></label><label>Frühester Start<select id="planning-earliest" required>${optionList(inputs.earliest_start)}</select><span class="selected-time" aria-hidden="true">${finite(millis(inputs.earliest_start)) ? escapeHtml(plantStamp(inputs.earliest_start, view.timezone)) : "Noch kein Start gewählt"}</span></label><label>Spätestes Ende<select id="planning-latest" required>${optionList(inputs.latest_end)}</select><span class="selected-time" aria-hidden="true">${finite(millis(inputs.latest_end)) ? escapeHtml(plantStamp(inputs.latest_end, view.timezone)) : "Noch kein Ende gewählt"}</span></label><button id="planning-calculate" class="reset-button" type="submit" ${choices.length ? "" : "disabled"}>Zeitfenster berechnen</button></form><p id="planning-input-notice" class="hint">${planningUI.dirty ? PLANNING_CHANGED_HINT : ""}</p>${output}<p class="hint">Basis sind die vorhandenen Prognoseintervalle mit gleichmäßiger mittlerer Leistung innerhalb jedes Intervalls. Für dieses Fenster gibt es noch kein belastbares Erfahrungsband. Verfügbarer Überschuss hängt zusätzlich von Hausverbrauch und Speicher ab. Es werden keine Geräte eingeschaltet.</p></details>`;
}

export function renderDailyTendencies(view) {
  if (!Array.isArray(view.daily_forecasts) || view.daily_forecasts.length <= 2) return "";
  return `<section aria-label="Mehrtagesaussicht"><h3>Mehrtagesaussicht</h3><dl class="daily-tendencies">${view.daily_forecasts.map((day) => `<div><dt>${escapeHtml(day.date)}${day.tendency ? " · Tendenz" : ""}</dt><dd>${energyText(day.energy_kwh)} kWh${day.quality_flags?.length ? " · Eingabewerte eingeschränkt" : ""}</dd></div>`).join("")}</dl><p class="hint">Datierte Werte des angezeigten Prognosestands. Die Prognosegüte späterer Tage ist noch nicht gemessen; ein belastbares Erfahrungsband fehlt. Zeitfenster können über den gesamten geladenen Zeitraum geplant werden.</p></section>`;
}


export function currentExplanation(state) {
  const view = state?.forecast?.data, data = state?.forecast?.envelope?.explanation;
  return data?.schema_version === 1 && data.scope === "total" && data.date === view?.date && data.timezone === view?.timezone && millis(data.start) === millis(view?.start) && millis(data.end) === millis(view?.end) ? data : null;
}

const explanationLabels = {
  before_calibration_kwh: "Basis vor Selbstkalibrierung",
  calibration_delta_kwh: "+ Beitrag des angewendeten Faktors",
  group_clipping_kwh: "− Kürzung durch AC-Gruppen",
  total_clipping_kwh: "− Zusätzliche Kürzung durch Anlagenlimit",
  effective_kwh: "= Wirksame AC-Prognose",
};
function renderBalance(values) {
  return `<dl class="interval-values">${Object.entries(explanationLabels).map(([key,label]) => `<div><dt>${label}</dt><dd>${energyText(values[key])} kWh</dd></div>`).join("")}</dl>`;
}

export function renderExplanation(state, selectedKey = null) {
  if (state?.forecast?.data?.roof_id) return "";
  const data = currentExplanation(state), view = state?.forecast?.data;
  const available = data?.status === "available";
  const selected = available ? data.intervals?.find(item => intervalKey(item) === selectedKey) : null;
  const content = available ? `<p class="hint">${escapeHtml(data.date)} · Gesamtanlage · ${escapeHtml(data.timezone)}<br>Wetterabruf ${escapeHtml(plantStamp(data.fetched_at, data.timezone))} · ${data.origin === "restored" ? "Gespeicherter Stand" : "Live-Abruf"}${data.last_update_success ? data.stale ? " · veralteter Stand" : "" : " · letzte Aktualisierung fehlgeschlagen"}</p><p class="hint">Angewendeter Anlagenfaktor: ${energyText(data.factor)}</p>${renderBalance(data.totals)}<dl class="interval-values"><div><dt>Grundmodell nach AC-Grenzen</dt><dd>${energyText(data.totals.raw_model_kwh)} kWh</dd></div><div><dt>Wirksam minus Grundmodell nach AC-Grenzen</dt><dd>${energyText(data.totals.effective_minus_raw_kwh)} kWh · ${finite(data.totals.effective_minus_raw_percent) ? energyText(data.totals.effective_minus_raw_percent) + " %" : "Prozent nicht anwendbar"}</dd></div></dl>${selected ? `<h3>Ausgewähltes Intervall · ${escapeHtml(formatPlantTime(selected.start, data.timezone))}</h3>${renderBalance(selected)}` : ""}${data.quality_flags?.length || data.complete === false ? '<p class="hint">Die Wetterbasis enthält Qualitätsmarkierungen. Die Bilanz bestätigt keine Prognosegüte.</p>' : ""}` : `<p class="hint">${data ? "Für diesen Stand fehlt eine kompatible oder vollständig belegte Rohbasis. Es werden keine Einflüsse rückwärts aus der Endkurve geschätzt." : "Die Erklärung wird beim Öffnen aus derselben Prognosegeneration gelesen."}</p>`;
  return `<details id="explanation"><summary id="explanation-toggle">Prognose erklärt <span>${view?.day === "tomorrow" ? "Morgen" : "Heute"} · Gesamtanlage</span></summary><label class="raw-toggle"><input id="raw-toggle" type="checkbox" ${state.showRaw ? "checked" : ""}>Grundmodellwerte in Details und Tabelle anzeigen</label>${content}<p class="hint">Das Grundmodell enthält die Temperaturannahme, den eingestellten Systemwirkungsgrad, ${data?.assumptions?.includes("horizon_profile") ? "das aktive Horizontprofil und " : "gegebenenfalls ein Horizontprofil sowie "}die realen AC-Grenzen. Es verwendet Faktor 1 und ist keine verlustlose Modulproduktion.</p><p class="hint">Modellierte Einflüsse, keine gemessenen Geräteverluste oder nachgewiesene Verbesserung. Für Temperatur, Wirkungsgrad und Horizont wird keine separate kWh-Wirkung behauptet. Die Auswahl zusätzlicher Grundmodellwerte lässt sich auch im Karteneditor speichern.</p></details>`;
}

export function shiftArchiveDate(value, days) {
  return new Date(Date.parse(`${value}T12:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
}

function plantDateISO(value, timezone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value)).map((item) => [item.type, item.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function historicalState(day) {
  return {
    forecast: { data: { ...day, historical: true, day: "history", roof_id: null, intervals: [] } },
    history: { data: { current_targets: { intervals: day.intervals } } },
    measurement: { data: { total_intervals: day.intervals.map((item) => ({ start: item.start, end: item.end, energy_kwh: item.measurement?.energy_kwh, energy_complete: item.measurement?.complete === true })) } },
  };
}

function archiveIds(html) {
  return html.replace(/id="([^"]+)"/g, (_, id) => `id="archive-${id}"`)
    .replace(/aria-(labelledby|describedby)="([^"]+)"/g, (_, kind, ids) => `aria-${kind}="${ids.split(" ").map((id) => `archive-${id}`).join(" ")}"`)
    .replaceAll("url(#", "url(#archive-").replaceAll("data-interval-action", "data-archive-interval-action");
}

export async function readArchiveDay(hass, selection, entry, cache, force = false) {
  const data = { config_entry_id: entry, day_view: { ...selection } };
  const response = await cache.request(JSON.stringify(["get_history", data]), () => readService(hass, "get_history", data), force);
  const day = response.day_view;
  if (day?.schema_version !== 1 || day.scope !== "total" || day.date !== selection.date || day.horizon !== selection.horizon || (selection.configuration_id && day.configuration_id !== selection.configuration_id) || !validInterval(day) || !Array.isArray(day.intervals) || day.intervals.length > 50) throw new Error(UPDATE_HINT);
  formatPlantTime(day.start, day.timezone);
  return day;
}

export function renderArchiveDay(ui, width = 600) {
  const selection = ui.selection ?? {};
  const day = ui.result?.data;
  const contexts = ui.contexts ?? day?.contexts ?? [];
  const context = contexts.find((item) => item.configuration_id === selection.configuration_id) ?? contexts.find((item) => item.active);
  const bounds = context ?? {};
  const status = day?.status === "unavailable" ? "Archivansicht nicht verfügbar. Speicherstatus, Konfiguration und Quellen prüfen." : day?.status === "empty" ? "Für diesen Tag fehlen archivierte Stände." : day?.status === "partial" ? "Der Tag ist nur teilweise archiviert. Fehlende Intervalle bleiben leer." : "";
  let chart = "";
  if (day && day.status !== "unavailable") {
    const state = historicalState(day);
    const selected = day.intervals.find((item) => intervalKey(item) === ui.selected);
    const provenance = selected ? `<p class="hint">Stichtag ${escapeHtml(plantStamp(selected.cutoff, day.timezone))}<br>Wetterabruf ${escapeHtml(plantStamp(selected.fetched_at, day.timezone))}<br>Beobachtet ${escapeHtml(plantStamp(selected.observed_at, day.timezone))}<br>Bewertet ${escapeHtml(plantStamp(selected.measurement?.assessed_at, day.timezone))} · ${selected.measurement?.previous_revision_count ?? 0} frühere Bewertungen${selected.measurement?.revised ? " · Messung revidiert" : ""}<br>Ursprüngliches Messfenster ${escapeHtml(plantStamp(selected.source_start, day.timezone))} bis ${escapeHtml(plantStamp(selected.source_end, day.timezone))}: ${energyText(selected.measurement?.whole_interval_energy_kwh)} kWh${selected.measurement?.reasons?.includes("measurement_boundary") ? " · positive Randmessung nicht anteilig geteilt" : ""}</p>` : "";
    chart = `<p class="hint">Historischer Tag ${escapeHtml(day.date)} · ${escapeHtml(day.timezone)}<br>Kontext ${escapeHtml(day.configuration_id)}<br>${day.running ? "Archiv erfasst aktuell" : "Archivierung pausiert"}${day.retention_truncated ? " · Aufbewahrung wurde gekürzt" : ""}</p><div class="legend"><span><i class="forecast-key"></i>${escapeHtml(day.label)}</span><span><i class="actual-key"></i>Archivierte Messung</span></div>${archiveIds(renderChart(state, width, ui.selected))}${archiveIds(renderIntervalDetails(state, ui.selected))}${provenance}
      <dl class="interval-values">${Object.entries({ daily_previous_18: "Tagesprognose · 18 Uhr am Vortag", daily_same_06: "Tagesprognose · 06 Uhr am Zieltag" }).map(([key, title]) => `<div><dt>${title}</dt><dd>${energyText(day.daily_forecasts?.[key]?.energy_kwh)} kWh</dd></div>`).join("")}<div><dt>${day.daily_measurement?.manual_correction ? "Bestätigter Tagesertrag · korrigiert" : "Vollständig belegte Tagesmessung"}</dt><dd>${energyText(day.daily_measurement?.energy_kwh)} kWh</dd></div>${day.daily_measurement?.manual_correction ? `<div><dt>Automatisch erfasster Tageswert</dt><dd>${energyText(day.daily_measurement.measured_energy_kwh)} kWh</dd></div>` : ""}</dl>${day.daily_measurement?.manual_correction ? `<p class="hint">Tagesertrag manuell bestätigt am ${escapeHtml(plantStamp(day.daily_measurement.assessed_at, day.timezone))}. Die Stundenmessungen bleiben unverändert.</p>` : ""}<p class="hint">Stundenstände haben jeweils einen eigenen Stichtag. Ihre Summe ist keine ursprünglich ausgegebene Tageskurve. Aktuelle Lernfaktoren und Erfahrungsbänder werden nicht rückwirkend angewendet.</p>
      <details id="archive-values"><summary>Historische Intervallwerte</summary><div class="table-scroll" tabindex="0" role="region" aria-label="Historische Intervallwerte"><table><thead><tr><th>Beginn</th><th>Ende</th><th>Prognose kWh</th><th>Messung kWh</th><th>Stichtag</th><th>Bewertung</th></tr></thead><tbody>${day.intervals.map((item) => `<tr><td>${escapeHtml(plantStamp(item.start, day.timezone))}</td><td>${escapeHtml(plantStamp(item.end, day.timezone))}</td><td>${energyText(item.energy_kwh)}</td><td>${energyText(item.measurement?.energy_kwh)}</td><td>${escapeHtml(plantStamp(item.cutoff, day.timezone))}</td><td>${item.measurement?.revised ? "Revidiert · " : ""}${escapeHtml(plantStamp(item.measurement?.assessed_at, day.timezone))}</td></tr>`).join("")}</tbody></table></div></details>`;
  }
  return `<details id="archive-day"><summary id="archive-day-toggle">Archivtag erkunden <span>Historische Gesamtanlage</span></summary><div class="archive-controls"><label>Abgeschlossener Tag<input id="archive-date" type="date" value="${escapeHtml(selection.date ?? "")}" min="${escapeHtml(bounds.min_date ?? "")}" max="${escapeHtml(bounds.max_date ?? "")}"></label><label>Vergleichsgrundlage<select id="archive-context"><option value="">Aktuelle Anlage</option>${contexts.filter((item) => !item.active).map((item) => `<option value="${escapeHtml(item.configuration_id)}" ${selection.configuration_id === item.configuration_id ? "selected" : ""}>${escapeHtml(item.timezone)} · ${escapeHtml(item.configuration_id.slice(0, 12))}</option>`).join("")}</select></label><label>Vorlauf<select id="archive-horizon"><option value="hourly_1h" ${selection.horizon !== "hourly_3h" ? "selected" : ""}>Jeweils 1 Stunde vorher</option><option value="hourly_3h" ${selection.horizon === "hourly_3h" ? "selected" : ""}>Jeweils 3 Stunden vorher</option></select></label></div><div class="interval-controls"><button id="archive-prev" data-archive-nav="-1" ${bounds.min_date && selection.date <= bounds.min_date ? "disabled" : ""}>Vorheriger Tag</button><button id="archive-next" data-archive-nav="1" ${bounds.max_date && selection.date >= bounds.max_date ? "disabled" : ""}>Nächster Tag</button><button id="archive-refresh" data-archive-refresh>Aktualisieren</button></div><p class="hint" role="status">${escapeHtml(ui.result?.message ?? (ui.result?.status === "loading" ? "Archivtag wird gelesen …" : status))}</p>${chart}</details>`;
}

export function renderContent(config, state, width = 600, report = null, reportDays = 7, planningUI = {}, selectedKey = null, archiveUI = {}) {
  state = state ? { ...state, showRaw: config.show_raw_forecast === true } : state;
  const forecast = state?.forecast;
  const view = forecast?.data;
  const title = config.title || view?.plant_name || "PV-Prognose";
  if (!view) return `<div class="header"><div><p class="eyebrow">PV FORECAST</p><h2 tabindex="-1">${escapeHtml(title)}</h2></div></div><div class="placeholder">${renderNotices(state)}</div>${forecast?.status === "error" && config.roof_id ? '<button class="reset-button" id="reset-roof" data-reset-roof>Gesamtanlage anzeigen</button>' : ""}`;
  const roof = view.roofs.find((item) => item.id === view.roof_id);
  const scope = roof?.name ?? "Gesamtanlage";
  const fetchedAt = forecast.envelope?.fetched_at;
  const weatherStamp = finite(millis(fetchedAt)) ? `${formatPlantDate(fetchedAt, view.timezone)}, ${formatPlantTime(fetchedAt, view.timezone)}` : "unbekannt";
  const measurement = state.measurement;
  const total = measurement?.data?.current_location_total_energy ?? measurement?.data?.total_energy;
  const actual = total?.energy_kwh;
  const actualHint = measurement?.retained || forecast?.retained ? "Letzter Messstand · Aktualisierung fehlgeschlagen" : view.roof_id ? "Keine Dachmessung" : measurement?.status === "ready" ? finite(actual) ? "Aktueller Stand" : "Noch keine Messwerte" : measurement?.status === "loading" ? "Messdaten laden …" : "Keine Messdaten";
  const kpi = (name, value, detail, className = "") => `<div class="kpi ${className}${energyText(value).length > 6 ? " kpi-wide" : ""}"><dt>${name}</dt><dd>${energyText(value)} <small>kWh</small></dd><span>${detail}</span></div>`;
  return `<div class="header"><div><p class="eyebrow">PV FORECAST</p><h2 tabindex="-1">${escapeHtml(title)}</h2><p class="subtitle">${escapeHtml(scope)} · ${escapeHtml(formatPlantDate(view.start, view.timezone))}</p></div><span class="badge ${view.stale ? "warning" : ""}">${forecast.envelope?.origin === "restored" ? "Gespeicherte Prognose" : forecast.retained ? "Letzter Stand" : view.stale ? "Veraltet" : "Prognose"}</span></div>
    <div class="controls"><div class="day-switch" role="group" aria-label="Prognosetag"><button data-day="today" aria-pressed="${config.day === "today"}">Heute</button><button data-day="tomorrow" aria-pressed="${config.day === "tomorrow"}">Morgen</button></div><label class="roof-label"><span>Fläche</span><select id="roof" aria-label="Fläche"><option value="">Gesamtanlage</option>${view.roofs.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === config.roof_id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label></div>
    <nav class="section-nav" aria-label="Bereiche der PV-Karte"><button id="nav-overview" data-section="overview-heading">Übersicht</button>${view.roof_id ? "" : '<button id="nav-planning" data-section="planning-heading">Planen</button>'}<button id="nav-comparison" data-section="comparison-heading">Vergleichen</button></nav>
    <section aria-labelledby="overview-heading"><h3 class="section-heading" id="overview-heading" tabindex="-1">Tagesübersicht</h3>
    <div class="overview-metrics"><section aria-label="Tagesprognosen"><h3 class="metric-heading">Tagesprognosen</h3><dl class="kpis">${kpi("Heute", view.summary.today_kwh, view.stale || forecast.retained ? "Letzter Prognosestand" : "Tagesprognose")}${kpi("Morgen", view.summary.tomorrow_kwh, view.stale || forecast.retained ? "Letzter Prognosestand" : "Tagesprognose")}</dl></section><section aria-label="Heutiger Stand"><h3 class="metric-heading">Heutiger Stand · ${escapeHtml(formatPlantDate(view.today_start, view.timezone))}</h3><dl class="kpis">${kpi("Rest heute", view.summary.remaining_today_kwh, forecast.retained ? "Rest zum letzten Stand" : "Ab jetzt erwartet")}${kpi("Ist heute", view.roof_id ? null : actual, actualHint, "measured")}</dl></section></div>
    <section class="chart-section" aria-label="Tagesverlauf"><div class="chart-heading"><h3>Energie im Tagesverlauf · ${view.day === "tomorrow" ? "Morgen" : "Heute"}</h3><span>kWh / Intervall</span></div><div class="legend"><span><i class="forecast-key"></i>Aktuelle Prognose</span><span><i class="actual-key"></i>Tatsächlich produziert</span></div>${renderChart(state, width, selectedKey)}${renderIntervalDetails(state, selectedKey)}<p class="chart-note">${escapeHtml(view.timezone)} · Ansicht ${escapeHtml(formatPlantTime(view.as_of, view.timezone))}<br>Wetterabruf ${escapeHtml(weatherStamp)}${forecast.envelope?.origin === "restored" ? "<br>Gespeicherter Stand · Aktualisierung fehlgeschlagen" : ""}${forecast.retained || measurement?.retained ? `<br>Letzte gelesene Ansicht: ${escapeHtml(plantStamp(view.as_of, view.timezone))}. Messfenster bis ${escapeHtml(plantStamp(measurement?.data?.end ?? measurement?.data?.outlook?.as_of ?? view.as_of, view.timezone))}.` : ""}</p></section>
    ${view.roof_id ? "" : renderExplanation(state, selectedKey)}
    ${renderNotices(state)}
    ${renderDailyTendencies(view)}
    ${view.roof_id ? "" : renderUnderperformance(state.history?.data?.underperformance)}</section>
    ${view.roof_id ? "" : `<section class="task-section" aria-labelledby="planning-heading"><h3 class="section-heading" id="planning-heading" tabindex="-1">Planen</h3><p class="hint">Heutige Tagesaussicht und ein passendes Solarzeitfenster finden.</p>${renderOutlook(state)}${renderPlanning(state, planningUI)}</section>`}
    <section class="task-section" aria-labelledby="comparison-heading"><h3 class="section-heading" id="comparison-heading" tabindex="-1">Vergleichen</h3><p class="hint">${view.roof_id ? "Prognoseintervalle dieser Dachfläche nachlesen." : "Prognose, belegte Messung und die bisherige Prognosegüte einordnen."}</p>
    ${view.roof_id ? "" : renderUncertainty(state)}${renderTable(state)}
    ${view.roof_id ? "" : `<details id="report"><summary id="report-toggle">Prognosegüte im Archiv <span>Gesamtanlage</span></summary><label class="report-label">Zeitraum<select id="report-days"><option value="7" ${reportDays === 7 ? "selected" : ""}>7 Tage</option><option value="30" ${reportDays === 30 ? "selected" : ""}>30 Tage</option></select></label>${renderReport(report ?? (state.history?.status === "ready" && reportDays === 7 ? state.history : null), reportDays)}</details>${renderArchiveDay(archiveUI, width)}`}</section>`;
}

const styles = `
  .raw-toggle{display:flex;align-items:center;gap:10px;min-height:44px;margin:12px 0}.raw-toggle input{font:inherit;width:20px;height:20px;flex-shrink:0}.raw-toggle input:focus-visible{outline:3px solid var(--primary-color);outline-offset:3px}

  #archive-day .hint{overflow-wrap:anywhere}.archive-controls{display:grid;gap:12px;margin:16px 0}.archive-controls label{display:grid;gap:6px;min-width:0}.archive-controls input{box-sizing:border-box;max-width:100%;min-width:0;min-height:44px;font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--pv-muted);border-radius:8px;padding:8px}.archive-controls input:focus-visible{outline:3px solid var(--primary-color);outline-offset:3px}
  :host{display:block;--pv-space:8px;--pv-muted:color-mix(in srgb,var(--secondary-text-color,#64717a) 85%,var(--primary-text-color,#202b32));--pv-radius:12px;--pv-text:.875rem;--pv-surface:var(--secondary-background-color,#f2f5f6);--pv-border:var(--divider-color,#e4e8eb);--pv-line:var(--primary-color,#007c91);--pv-archive:var(--secondary-text-color,#636b73);--pv-actual:color-mix(in srgb,var(--accent-color,#b88424) 55%,var(--primary-text-color,#202b32));color:var(--primary-text-color,#202b32);font-family:var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
  *{box-sizing:border-box}ha-card{display:block;background:var(--ha-card-background,var(--card-background-color,#fff));border-radius:var(--ha-card-border-radius,16px);border:var(--ha-card-border-width,1px) solid var(--ha-card-border-color,var(--divider-color,#e4e8eb));box-shadow:var(--ha-card-box-shadow,none);overflow:hidden} .body{padding:22px 22px 8px;min-width:0}
  .header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.eyebrow{font-size:var(--pv-text);font-weight:700;letter-spacing:.14em;color:var(--pv-muted);margin:0 0 6px}h2{font-size:1.625rem;font-weight:600;letter-spacing:-.025em;line-height:1.25;margin:0;overflow-wrap:anywhere}.subtitle{color:var(--pv-muted);font-size:var(--pv-text);margin:6px 0 0;overflow-wrap:anywhere}.badge{font-size:var(--pv-text);border:1px solid var(--divider-color,#e4e8eb);padding:5px 8px;border-radius:20px;white-space:nowrap}.warning{color:var(--warning-color,#956400)}
  .controls{display:flex;flex-wrap:wrap;align-items:flex-end;gap:16px;justify-content:space-between;margin:24px 0 20px}.day-switch{display:flex;background:var(--pv-surface);padding:3px;border-radius:var(--pv-radius)}.day-switch button{border:0;border-radius:var(--pv-radius);background:transparent;color:var(--pv-muted);font:inherit;font-size:var(--pv-text);min-height:44px;padding:0 15px;cursor:pointer}.day-switch button[aria-pressed=true]{background:var(--card-background-color,#fff);box-shadow:0 1px 3px #0002;color:var(--primary-text-color,#202b32);font-weight:600}label{color:var(--pv-muted);font-size:var(--pv-text)}.roof-label{grid-template-columns:minmax(0,1fr);min-width:0;max-width:100%;flex:1;display:grid;gap:4px}select{width:100%;min-width:0;font:inherit;font-size:var(--pv-text);color:var(--primary-text-color,#202b32);background:var(--card-background-color,#fff);border:1px solid var(--pv-muted);border-radius:var(--pv-radius);min-height:44px;max-width:100%;padding:7px 25px 7px 10px}button:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:3px}
  .kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--pv-space);margin:0 0 26px;padding:0}.kpi{min-width:0;padding:16px;background:var(--pv-surface);border-radius:var(--pv-radius)}.kpi dt{font-size:var(--pv-text);color:var(--pv-muted);margin-bottom:7px}.kpi dd{margin:0;font-size:1.875rem;font-variant-numeric:tabular-nums;letter-spacing:-.035em;font-weight:600;white-space:normal;overflow-wrap:normal}.kpi small{font-size:var(--pv-text);font-weight:400;color:var(--pv-muted);letter-spacing:0}.kpi>span{display:block;color:var(--pv-muted);font-size:var(--pv-text);line-height:1.4;margin-top:5px}.measured dd{color:var(--pv-line)}
  .chart-heading{display:flex;justify-content:space-between;align-items:baseline;gap:8px}h3{font-size:var(--pv-text);font-weight:600;margin:0}.chart-heading>span{font-size:var(--pv-text);color:var(--pv-muted)}.legend{display:flex;flex-wrap:wrap;gap:8px 16px;margin:12px 0 8px;color:var(--pv-muted);font-size:var(--pv-text);line-height:1.4}.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{display:inline-block;width:18px;flex-shrink:0}.forecast-key{border-top:3px solid var(--pv-line)}.actual-key{border-top:3px dashed var(--pv-actual)}.chart{display:block;width:100%;height:auto;overflow:visible}.grid{stroke:var(--divider-color,#e4e8eb);stroke-width:1}.hour-tick{stroke:var(--pv-muted);stroke-width:1;vector-effect:non-scaling-stroke;pointer-events:none}.axis{fill:var(--pv-muted);font-size:11px}.axis tspan{font-size:10px}.forecast-line{stroke:var(--pv-line);stroke-width:2.5;fill:none;stroke-linejoin:round}.actual-line{stroke:var(--pv-actual);stroke-width:3;stroke-dasharray:5 4;fill:none;stroke-linejoin:round}.now{stroke:var(--pv-muted);stroke-width:1;stroke-dasharray:2 4}.now-label{font-size:11px;fill:var(--pv-muted)}.gap{fill:var(--pv-muted);opacity:.09}.empty-plot{font-size:var(--pv-text);fill:var(--pv-muted)}.chart-note{font-size:var(--pv-text);color:var(--pv-muted);text-align:right;margin:0 0 17px;overflow-wrap:anywhere}
  .notices{padding:9px 11px;margin:0 0 16px;background:var(--pv-surface);border-radius:var(--pv-radius)}.notices p{font-size:var(--pv-text);line-height:1.5;color:var(--pv-muted);margin:3px 0}.placeholder{min-height:540px;font-size:0.875rem;line-height:1.6;color:var(--pv-muted);padding:28px 0}
  .reset-button{min-height:44px;padding:10px 15px;border-radius:var(--pv-radius);border:1px solid var(--divider-color,#dce3e6);background:var(--card-background-color,#fff);color:var(--primary-color,#007c91);font:inherit;font-size:var(--pv-text);margin-bottom:16px;cursor:pointer}
  details{border-top:1px solid var(--divider-color,#e4e8eb)}summary{min-height:48px;padding:15px 0;font-size:var(--pv-text);font-weight:500;cursor:pointer;line-height:1.5}summary span{font-size:var(--pv-text);color:var(--pv-muted);display:block;font-weight:400;margin-top:4px}.hint{font-size:var(--pv-text);line-height:1.6;color:var(--pv-muted);margin:0 0 14px}.report-label{display:flex;align-items:center;gap:12px;margin:0 0 12px}.report-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:12px 0 16px}.report-metrics dt{font-size:var(--pv-text);color:var(--pv-muted)}.report-metrics dd{margin:4px 0 0;font-size:1.1875rem}.report-metrics small{font-size:var(--pv-text);color:var(--pv-muted)}
  table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:var(--pv-text);margin-bottom:12px}th,td{padding:9px 3px;border-bottom:1px solid var(--divider-color,#e4e8eb);text-align:right;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}th:first-child{width:34%;text-align:left}thead th{font-size:var(--pv-text);font-weight:500;color:var(--pv-muted)}tbody th{font-weight:400;font-size:var(--pv-text)}.until{display:block;color:var(--pv-muted);font-size:var(--pv-text);margin-top:3px}.sr-only{position:absolute;clip:rect(0,0,0,0);width:1px;height:1px;overflow:hidden}
  @container (max-width:460px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--pv-space)}.kpi{padding:12px}.kpi dd{font-size:1.875rem}.body{padding:18px 16px 6px}.controls{gap:10px;margin-top:20px}.day-switch button{padding:0 12px}h2{font-size:1.5rem}.badge{font-size:var(--pv-text)}.legend{column-gap:12px}}
  .daily-tendencies{display:grid;gap:8px;font-size:0.875rem}.daily-tendencies>div{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.daily-tendencies dd{margin:0}
  #planning-input-notice:empty{display:none}#planning-form{display:grid;gap:12px;margin-bottom:8px}#planning-form label{display:grid;gap:5px;min-width:0}#planning-form select{width:100%}#planning-form input{font:inherit;font-size:0.875rem;min-height:44px;width:100%;padding:8px 10px;border:1px solid var(--pv-muted);border-radius:var(--pv-radius);background:var(--card-background-color,#fff);color:var(--primary-text-color,#202b32)}input:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:3px}#planning-calculate{margin:2px 0 4px}.feature-result{font-size:0.875rem;line-height:1.7;margin:0 0 12px;overflow-wrap:anywhere}.feature-result strong{font-size:1.125rem}.hint strong{color:var(--primary-text-color,#202b32)}
  .section-nav{display:flex;flex-wrap:wrap;gap:var(--pv-space);margin:0 0 24px}.section-nav button{font:inherit;font-size:var(--pv-text);min-height:44px;padding:8px 12px;border:1px solid var(--pv-muted);border-radius:var(--pv-radius);background:transparent;color:var(--primary-text-color,#202b32);cursor:pointer}.section-nav button:hover{background:var(--pv-surface)}
  .section-heading{font-size:1.125rem;margin:0 0 16px;scroll-margin-top:76px}.section-heading:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:4px}.metric-heading{font-weight:500;color:var(--pv-muted);margin:0 0 8px;line-height:1.5}.overview-metrics{display:grid;gap:var(--pv-space)}.overview-metrics>section{min-width:0}.overview-metrics .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.task-section{border-top:1px solid var(--pv-border);padding-top:24px;margin-top:24px}.task-section>details:last-child{margin-bottom:8px}
  @container (min-width:720px){.overview-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .selected-interval{fill:var(--primary-color,#007c91);fill-opacity:.1;stroke:var(--primary-text-color,#202b32);stroke-width:1;stroke-dasharray:3 3}.chart:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:4px}.interval-detail{padding:16px;margin:12px 0;background:var(--pv-surface);border-radius:var(--pv-radius)}.interval-detail h3{line-height:1.6}.interval-values{display:grid;gap:12px;margin:12px 0}.interval-values dt{font-size:var(--pv-text);color:var(--pv-muted)}.interval-values dd{margin:4px 0 0;font-size:var(--pv-text);font-variant-numeric:tabular-nums}.interval-values dd span{display:block}.interval-controls{display:flex;flex-wrap:wrap;gap:8px}.interval-controls button{min-width:44px;min-height:44px;padding:8px 12px;border:1px solid var(--pv-muted);border-radius:var(--pv-radius);font:inherit;font-size:var(--pv-text);color:var(--primary-text-color,#202b32);background:var(--card-background-color,#fff);cursor:pointer}
  .table-scroll{max-width:100%;overflow:auto;margin-bottom:12px}.table-scroll table{min-width:30rem}.table-scroll:focus-visible,h2:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:3px}
  button,select,input{min-width:44px;min-height:44px}button,summary,label,.hint,.notices,.interval-detail{overflow-wrap:anywhere}
  @container (max-width:22rem){.overview-metrics .kpis{grid-template-columns:minmax(0,1fr)}.overview-metrics .kpi-wide{grid-column:auto}.report-metrics{grid-template-columns:minmax(0,1fr)}.day-switch{flex-shrink:1;max-width:100%;flex-wrap:wrap}.kpi dd{overflow-wrap:anywhere}}
  @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
  :host{container-type:inline-size}
  .header>div,.chart-section{min-width:0}.header{flex-wrap:wrap}.chart-heading{flex-wrap:wrap}.badge{color:var(--pv-muted)}.badge.warning{color:var(--primary-text-color,#202b32);border-color:currentColor}
  .kpi-wide{grid-column:span 2}.kpi dd small{display:inline-block;white-space:nowrap}.report-metrics dd{overflow-wrap:anywhere}.legend{row-gap:10px}.chart-note{line-height:1.6;text-align:left}.notices{padding:12px 16px}.hint{margin-top:8px}.day-switch{flex-shrink:0}
  @container (min-width:720px){.kpis{grid-template-columns:repeat(4,minmax(0,1fr))}.report-metrics{grid-template-columns:repeat(4,minmax(0,1fr))}}

`;

/** Gleiche Bedienelemente bleiben verbunden: Fokus, Cursor und native Auswahl
 * gehören dem Browser. Nur tatsächlich geänderte Knoten/Attribute aktualisieren. */
function updateChildren(parent, desired, focused) {
  const key = (node) => node.nodeType === 1 ? node.id || node.getAttribute("data-day") || node.getAttribute("data-notice") || node.getAttribute("aria-labelledby") || node.getAttribute("aria-label") || "" : "";
  const compatible = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b);
  let cursor = parent.firstChild;
  for (const next of [...desired.childNodes]) {
    let current = cursor;
    while (current && !compatible(current, next)) current = current.nextSibling;
    if (!current) {
      current = next.cloneNode(true);
      parent.insertBefore(current, cursor);
    } else {
      if (current !== cursor) parent.insertBefore(current, cursor);
      if (current.nodeType === 3) {
        if (current.data !== next.data) current.data = next.data;
      } else if (current.nodeType === 1) {
        // Die Optionen einer geöffneten nativen Auswahl erst nach Verlassen ändern.
        // Entfernte Ansichten selbst werden weiterhin sofort entfernt.
        if (current === focused && current.localName === "select") {
          cursor = current.nextSibling;
          continue;
        }
        for (const attr of [...current.attributes]) {
          if (current.localName === "details" && attr.name === "open") continue;
          if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
        }
        for (const attr of [...next.attributes]) {
          if (current === focused && attr.name === "value") continue;
          if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
        }
        updateChildren(current, next, focused);
        if (current !== focused && ["input", "select"].includes(current.localName) && current.value !== next.value) current.value = next.value;
      }
    }
    cursor = current.nextSibling;
  }
  while (cursor) { const next = cursor.nextSibling; cursor.remove(); cursor = next; }
}

const ElementBase = globalThis.HTMLElement ?? class {};
export class PvForecastCard extends ElementBase {
  static getConfigForm() {
    return {
      schema: [
        { name: "config_entry_id", required: true, selector: { config_entry: { integration: "pv_forecast" } } },
        { name: "show_raw_forecast", selector: { boolean: {} } },
        { name: "title", selector: { text: {} } },
        { name: "day", selector: { select: { options: [{ value: "today", label: "Heute" }, { value: "tomorrow", label: "Morgen" }], mode: "dropdown" } } },
      ],
      computeLabel: (schema) => ({ config_entry_id: "PV-Anlage", show_raw_forecast: "Grundmodellwerte in Details und Tabelle anzeigen", title: "Titel (optional)", day: "Anfänglich angezeigter Tag" })[schema.name] ?? schema.name,
    };
  }

  static getStubConfig(hass) {
    const entity = Object.values(hass?.entities ?? {}).find((item) => item.platform === "pv_forecast" && item.config_entry_id);
    return { config_entry_id: entity?.config_entry_id ?? "", day: "today" };
  }

  constructor() {
    super();
    this._width = 550;
    this._selectedInterval = null;
    this._reportDays = 7;
    this._reportOpen = false;
    this._archiveOpen = false;
    this._explanationOpen = false;
    this._archiveGeneration = 0;
    this._valuesOpen = false;
    this._outlookOpen = false;
    this._uncertaintyOpen = false;
    this._planningOpen = false;
    if (!this.attachShadow) return;
    this.attachShadow({ mode: "open" });
    this.shadowRoot.addEventListener("click", (event) => {
      const archiveAction = event.target.closest?.("[data-archive-interval-action]")?.dataset.archiveIntervalAction;
      if (archiveAction) { this._chooseArchiveInterval(archiveAction); return; }
      const archiveChart = event.target.closest?.("#archive-interval-chart");
      if (archiveChart && this._archiveDay?.data) {
        const state = historicalState(this._archiveDay.data), rect = archiveChart.getBoundingClientRect();
        const { left } = plotGeometry(state.forecast.data, this._width);
        const row = intervalAtPosition(state, ((event.clientX - rect.left) * this._width / rect.width - left) / (this._width - left - 12));
        if (row) { this._archiveSelected = intervalKey(row); this._render(); }
        return;
      }
      const nav = event.target.closest?.("[data-archive-nav]")?.dataset.archiveNav;
      if (nav && this._archiveSelection?.date) { this._archiveSelection.date = shiftArchiveDate(this._archiveSelection.date, Number(nav)); this._loadArchiveDay(); return; }
      if (event.target.closest?.("[data-archive-refresh]")) { this._loadArchiveDay(true); return; }
      const action = event.target.closest?.("[data-interval-action]")?.dataset.intervalAction;
      if (action) { this._chooseInterval(action); return; }
      const chart = event.target.closest?.("#interval-chart");
      if (chart && this._state?.forecast?.data) {
        const rect = chart.getBoundingClientRect();
        const { left } = plotGeometry(this._state.forecast.data, this._width);
        const fraction = ((event.clientX - rect.left) * this._width / rect.width - left) / (this._width - left - 12);
        const row = intervalAtPosition(this._state, fraction);
        if (row) { this._selectedInterval = intervalKey(row); this._render(); this.shadowRoot.getElementById("interval-chart")?.focus({ preventScroll: true }); }
        return;
      }
      const section = event.target.closest?.("[data-section]")?.dataset.section;
      if (section) { const heading = this.shadowRoot.getElementById(section); heading?.focus({ preventScroll: true }); heading?.scrollIntoView({ block: "start" }); return; }
      if (event.target.closest?.("[data-reset-roof]")) { this._config = { ...this._config, roof_id: undefined }; this._bind(); return; }
      const day = event.target.closest?.("[data-day]")?.dataset.day;
      if (day && day !== this._config.day) { this._config = { ...this._config, day }; this._bind(); }
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.target.id === "archive-interval-chart" || event.target.closest?.("#archive-interval-detail")) {
        const action = ({ ArrowLeft: "previous", ArrowRight: "next", Home: "first", End: "last", Enter: "first", " ": "first", Escape: "close" })[event.key];
        if (action) { event.preventDefault(); this._chooseArchiveInterval(action); }
        return;
      }
      const inChart = event.target.id === "interval-chart";
      const inDetails = event.target.closest?.("#interval-detail");
      const action = event.key === "Escape" ? "close" : inChart ? ({ ArrowLeft: "previous", ArrowRight: "next", Home: "first", End: "last", Enter: "first", " ": "first" })[event.key] : null;
      if (action && (inChart || inDetails)) { event.preventDefault(); this._chooseInterval(action); }
    });
    this.shadowRoot.addEventListener("change", (event) => {
      if (event.target.id === "raw-toggle") { this._config = { ...this._config, show_raw_forecast: event.target.checked }; this._bind(); return; }
      const archiveField = { "archive-date": "date", "archive-horizon": "horizon", "archive-context": "configuration_id" }[event.target.id];
      if (archiveField) {
        this._archiveSelection = { ...this._archiveSelection, [archiveField]: event.target.value || undefined };
        if (archiveField === "configuration_id") {
          const context = this._archiveContexts?.find((item) => item.configuration_id === event.target.value) ?? this._archiveContexts?.find((item) => item.active);
          if (context && (this._archiveSelection.date < context.min_date || this._archiveSelection.date > context.max_date)) this._archiveSelection.date = context.max_date;
        }
        this._loadArchiveDay(); return;
      }
      if (event.target.id === "roof") { this._config = { ...this._config, roof_id: event.target.value || undefined }; this._bind(); }
      if (event.target.id === "report-days") { this._reportDays = Number(event.target.value); this._bindReport(); this._render(); }
      const field = { "planning-earliest": "earliest_start", "planning-latest": "latest_end" }[event.target.id];
      if (field) this._updatePlanningInput(field, event.target.value);
    });
    this.shadowRoot.addEventListener("input", (event) => {
      if (event.target.id === "planning-duration") this._updatePlanningInput("duration_minutes", event.target.value);
    });
    this.shadowRoot.addEventListener("focusout", (event) => {
      if (event.target.localName === "select") queueMicrotask(() => { if (this._connected) this._render(); });
    });
    this.shadowRoot.addEventListener("submit", (event) => {
      if (event.target.id === "planning-form") { event.preventDefault(); this._calculatePlanning(); }
    });
    this.shadowRoot.addEventListener("toggle", (event) => {
      // Nur das aktuelle Element darf bei einem Neuaufbau seinen Zustand melden.
      if (!event.target.isConnected) return;
      if (event.target.id === "explanation") {
        const open = event.target.open;
        if (open !== this._explanationOpen) { this._explanationOpen = open; this._bind(); }
      }
      if (event.target.id === "archive-day") {
        this._archiveOpen = event.target.open;
        if (this._archiveOpen && !this._archiveDay) this._loadArchiveDay();
        else if (!this._archiveOpen) { this._archiveGeneration++; if (this._archiveDay?.status === "loading") this._archiveDay = null; }
      }
      if (event.target.id === "values") this._valuesOpen = event.target.open;
      if (event.target.id === "outlook") this._outlookOpen = event.target.open;
      if (event.target.id === "uncertainty") this._uncertaintyOpen = event.target.open;
      if (event.target.id === "planning" && this._planningOpen !== event.target.open) { this._planningOpen = event.target.open; this._bindPlanning(); }
      if (event.target.id === "report" && this._reportOpen !== event.target.open) {
        this._reportOpen = event.target.open;
        this._bindReport();
      }
    }, true);
  }

  async _loadArchiveDay(force = false) {
    const generation = ++this._archiveGeneration;
    if (!this._connected || this._visible === false || globalThis.document?.hidden || !this._archiveOpen || this._config?.roof_id || !this._archiveSelection?.date) return;
    const selection = { ...this._archiveSelection }, entry = this._config.config_entry_id;
    this._archiveDay = { status: "loading" }; this._archiveSelected = null; this._render();
    try {
      const data = await readArchiveDay(this._hass, selection, entry, connectionCache(this._hass), force);
      if (generation === this._archiveGeneration && globalThis.document?.hidden) { this._archiveDay = null; return; }
      if (generation !== this._archiveGeneration || !this._connected || !this._archiveOpen || this._visible === false) return;
      this._archiveDay = { status: "ready", data }; this._archiveContexts = data.contexts;
    } catch (error) {
      if (generation === this._archiveGeneration && globalThis.document?.hidden) { this._archiveDay = null; return; }
      if (generation !== this._archiveGeneration || !this._connected || !this._archiveOpen || this._visible === false) return;
      this._archiveDay = sourceError(error, "Archivtag");
      if (this._archiveDay.reason === "permission") this._archiveContexts = [];
    }
    this._render();
  }

  _chooseArchiveInterval(action) {
    if (!this._archiveDay?.data) return;
    const rows = tableRows(historicalState(this._archiveDay.data));
    if (!rows.length) return;
    const current = rows.findIndex((row) => intervalKey(row) === this._archiveSelected);
    const index = action === "last" ? rows.length - 1 : action === "first" || current < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, current + (action === "previous" ? -1 : 1)));
    this._archiveSelected = action === "close" ? null : intervalKey(rows[index]); this._render();
    if (action === "first" || action === "close") this.shadowRoot.getElementById("archive-interval-chart")?.focus({ preventScroll: true });
  }

  _chooseInterval(action) {
    if (!this._state?.forecast?.data) return;
    const rows = tableRows(this._state);
    if (!rows.length) return;
    const current = rows.findIndex((row) => intervalKey(row) === this._selectedInterval);
    const index = action === "last" ? rows.length - 1 : action === "first" ? 0 : current < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, current + (action === "previous" ? -1 : 1)));
    this._selectedInterval = action === "close" ? null : intervalKey(rows[index]);
    this._render();
    if (action === "close" || action === "first") this.shadowRoot.getElementById("interval-chart")?.focus({ preventScroll: true });
  }

  setConfig(config) {
    if (!config?.config_entry_id || typeof config.config_entry_id !== "string") throw new Error("Bitte eine PV-Anlage auswählen.");
    if (config.day && !["today", "tomorrow"].includes(config.day)) throw new Error("Der Prognosetag muss Heute oder Morgen sein.");
    if (this._config?.config_entry_id !== config.config_entry_id) {
      this._archiveGeneration++; this._archiveDay = null; this._archiveSelection = null; this._archiveContexts = [];
      this._state = null;
      this._planning = null;
      this._planningInputs = null;
      this._planningRequest = null;
      this._planningPreviousStart = null;
      this._planningDirty = false;
    }
    this._config = { ...config, day: config.day ?? "today" };
    this._bind();
    this._render();
  }

  set hass(hass) {
    const changed = this._hass?.connection !== hass.connection || !this._hass;
    this._hass = hass;
    if (changed || !this._unsubscribe) this._bind();
  }

  getCardSize() { return 7; }
  getGridOptions() { return { columns: 12, rows: 8, min_columns: 6, min_rows: 7 }; }

  connectedCallback() {
    this._connected = true;
    this._visible = !globalThis.IntersectionObserver;
    if (globalThis.IntersectionObserver) {
      const observer = new IntersectionObserver(([entry]) => {
        if (!this._connected || this._visibilityObserver !== observer || this._visible === entry.isIntersecting) return;
        this._visible = entry.isIntersecting;
        this._bind();
      });
      this._visibilityObserver = observer;
      observer.observe(this);
    }
    if (globalThis.ResizeObserver) {
      this._observer = new ResizeObserver(([entry]) => {
        const width = Math.max(250, Math.round(entry.contentRect.width) - (entry.contentRect.width <= 460 ? 32 : 44));
        if (width !== this._width) { this._width = width; this._render(); }
      });
      this._observer.observe(this);
    }
    this._bind();
  }

  disconnectedCallback() {
    this._archiveGeneration++;
    this._connected = false;
    this._unsubscribe?.(); this._unsubscribe = null;
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._unsubscribePlanning?.(); this._unsubscribePlanning = null;
    this._observer?.disconnect();
    this._visibilityObserver?.disconnect();
    this._visibilityObserver = null;
  }

  _bind() {
    this._archiveGeneration++;
    if (this._archiveDay?.status === "loading") this._archiveDay = null;
    this._unsubscribe?.(); this._unsubscribe = null;
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._unsubscribePlanning?.(); this._unsubscribePlanning = null;
    this._selectedInterval = null;
    this._selectionPending = Boolean(this._state?.forecast?.data && (this._state.forecast.data.day !== this._config?.day || (this._state.forecast.data.roof_id ?? null) !== (this._config?.roof_id ?? null)));
    this._report = null;
    if (!this._connected || this._visible === false || !this._hass || !this._config) return;
    const cache = connectionCache(this._hass);
    const explain = !this._config.roof_id && (this._explanationOpen || this._config.show_raw_forecast === true);
    const config = { ...this._config, day: explain ? this._config.day : "today", include_explanation: explain };
    this._unsubscribe = cache.subscribe(JSON.stringify(["view", config.config_entry_id, config.roof_id ?? null, ...(explain ? ["explanation", config.day] : [])]), (publish, active) => loadView(this._hass, config, publish, active, cache), (state) => {
      if (!this._connected) return;
      // Bei einer laufenden Auswahl bleiben Bedienelemente und Tastaturfokus bestehen.
      if (state.forecast?.status === "loading" && this._state?.forecast?.data) return;
      this._selectionPending = false;
      this._state = selectViewDay(state, this._config.day);
      this._render();
      if (this._archiveOpen && !this._archiveDay) this._loadArchiveDay();
    });
    this._bindReport();
    this._bindPlanning();
    this._render();
  }

  _bindReport() {
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._report = null;
    if (!this._connected || this._visible === false || !this._hass || !this._config || !this._reportOpen || this._config.roof_id) return;
    const cache = connectionCache(this._hass);
    const days = this._reportDays, entry = this._config.config_entry_id;
    this._unsubscribeReport = cache.subscribe(JSON.stringify(["report", entry, days]), async (publish, active) => {
      try {
        const data = await cache.request(JSON.stringify(["get_history", { config_entry_id: entry, days, current_targets: true }]), () => readService(this._hass, "get_history", { config_entry_id: entry, days, current_targets: true }));
        if (active()) publish({ status: "ready", data });
      } catch (error) { if (active()) publish(sourceError(error, "Archivdaten")); }
    }, (report) => { if (this._connected) { this._report = report; this._render(); } });
  }

  _updatePlanningInput(field, value) {
    this._planningInputs = { ...this._planningInputs, [field]: value };
    this._planningDirty = true;
    const notice = this.shadowRoot?.getElementById("planning-input-notice");
    if (notice) notice.textContent = PLANNING_CHANGED_HINT;
    this._announceInteraction();
  }

  _calculatePlanning() {
    const inputs = this._planningInputs ?? {};
    const duration = Number(inputs.duration_minutes);
    if (!Number.isInteger(duration) || duration < 1 || duration > 2880 || !finite(millis(inputs.earliest_start)) || !finite(millis(inputs.latest_end)) || millis(inputs.latest_end) <= millis(inputs.earliest_start)) {
      this._unsubscribePlanning?.(); this._unsubscribePlanning = null;
      this._planningRequest = null;
      this._planning = { status: "error", message: "Bitte 1 bis 2880 Minuten und ein Ende nach dem frühesten Start wählen." };
      this._render();
      return;
    }
    this._planningRequest = { duration_minutes: duration, earliest_start: inputs.earliest_start, latest_end: inputs.latest_end };
    this._planningPreviousStart = null;
    this._planning = { status: "loading" };
    this._planningDirty = false;
    this._bindPlanning();
    this._render();
  }

  _bindPlanning() {
    this._unsubscribePlanning?.(); this._unsubscribePlanning = null;
    if (!this._connected || this._visible === false || !this._hass || !this._config || !this._planningOpen || !this._planningRequest || this._config.roof_id) return;
    const cache = connectionCache(this._hass);
    const entry = this._config.config_entry_id;
    const request = { ...this._planningRequest };
    this._unsubscribePlanning = cache.subscribe(JSON.stringify(["planning", entry, request]), async (publish, active) => {
      const previous = this._planningPreviousStart;
      const serviceData = { config_entry_id: entry, planning: { ...request, ...(finite(millis(previous)) ? { previous_start: previous } : {}) } };
      try {
        const response = await cache.request(JSON.stringify(["get_forecast", serviceData]), () => readService(this._hass, "get_forecast", serviceData));
        if (response.planning?.schema_version !== 1) throw new Error(UPDATE_HINT);
        if (active()) publish({ status: "ready", data: response.planning });
      } catch (error) { if (active()) publish(sourceError(error, "Planungsdaten")); }
    }, (planning) => {
      if (!this._connected) return;
      if (finite(millis(planning.data?.start))) this._planningPreviousStart = planning.data.start;
      this._planning = planning;
      this._render();
    });
  }

  _announceInteraction() {
    let live = this.shadowRoot.getElementById("interaction-status");
    if (!live) {
      live = document.createElement("div"); live.id = "interaction-status"; live.className = "sr-only";
      live.setAttribute("role", "status"); live.setAttribute("aria-atomic", "true"); this.shadowRoot.append(live);
    }
    if (this._announcedInterval !== this._selectedInterval) {
      const hadInterval = Boolean(this._announcedInterval);
      this._announcedInterval = this._selectedInterval;
      const detail = this.shadowRoot.getElementById("interval-detail");
      if (detail) {
        const values = [...detail.querySelectorAll("dl>div")].map((row) => `${row.querySelector("dt").textContent}: ${row.querySelector("dd").textContent}`).join(". ");
        live.textContent = `${detail.querySelector("h3").innerText.replaceAll("\n", " ")}. ${values}`;
      } else if (hadInterval) live.textContent = "Intervallauswahl geschlossen.";
    }
    const planning = JSON.stringify([this._planningDirty, this._planning?.status, this._planning?.message, this._planning?.data?.status, this._planning?.data?.start, this._planning?.data?.end]);
    if (planning !== this._announcedPlanning) {
      this._announcedPlanning = planning;
      if (this._planningDirty) live.textContent = PLANNING_CHANGED_HINT;
      else if (this._planning) live.textContent = this._planning.status === "loading" ? "Zeitfenster wird berechnet." : this._planning.message || (this._planning.data?.status === "available" ? "Solarzeitfenster verfügbar. Die Empfehlung steht unter der Auswahl." : "Planung aktualisiert. Das Ergebnis steht unter der Auswahl.");
    }
    const report = JSON.stringify([this._reportDays, this._report?.status, this._report?.message]);
    if (report !== this._announcedReport) {
      this._announcedReport = report;
      if (this._reportOpen && this._report) live.textContent = this._report.message || `Archivbericht für ${this._reportDays} Tage verfügbar.`;
    }
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    if (this._selectedInterval && !tableRows(this._state).some((row) => intervalKey(row) === this._selectedInterval)) this._selectedInterval = null;
    // Dokument und eingebettetes Panel können jeweils den Scrollbereich besitzen.
    const scrollPositions = [];
    for (let node = this; node; node = node.parentNode || node.host) {
      if (typeof node.scrollTop === "number") scrollPositions.push([node, node.scrollTop, node.scrollLeft]);
    }
    for (const node of this.shadowRoot.querySelectorAll(".table-scroll")) scrollPositions.push([node, node.scrollTop, node.scrollLeft]);
    const existingDetails = new Set(this.shadowRoot.querySelectorAll("details"));
    const focused = this.shadowRoot.activeElement;
    const focusId = focused?.id;
    const focusDay = focused?.dataset?.day;
    if (!this._planningInputs && this._state?.forecast?.data) {
      const choices = planningChoices(this._state);
      const now = millis(this._state.forecast.data.as_of);
      this._planningInputs = { duration_minutes: "120", earliest_start: choices.find((value) => millis(value) >= now) ?? choices[0], latest_end: choices.at(-1) };
    }
    if (!this._archiveSelection && this._state?.forecast?.data) this._archiveSelection = { date: shiftArchiveDate(plantDateISO(this._state.forecast.data.today_start, this._state.forecast.data.timezone), -1), horizon: "hourly_1h" };
    let live = this.shadowRoot.getElementById("data-status");
    if (!live) {
      live = document.createElement("div"); live.id = "data-status"; live.className = "sr-only";
      live.setAttribute("role", "status"); live.setAttribute("aria-atomic", "true");
    }
    let content = this.shadowRoot.getElementById("card-content");
    if (!content) { content = document.createElement("div"); content.id = "card-content"; this.shadowRoot.append(content, live); }
    const template = document.createElement("template");
    template.innerHTML = `<style>${styles}</style><ha-card><div class="body" aria-busy="${Boolean(this._selectionPending)}">${renderContent(this._config, this._state ? { ...this._state, selectionPending: this._selectionPending } : null, this._width, this._report, this._reportDays, { inputs: this._planningInputs, result: this._planning, dirty: this._planningDirty }, this._selectedInterval, { selection: this._archiveSelection, result: this._archiveDay, contexts: this._archiveContexts, selected: this._archiveSelected })}</div></ha-card>`;
    updateChildren(content, template.content, focused);
    const announcement = dataNotices(this._state).map((item) => `${item.level}: ${item.title}`).join(". ");
    if (announcement !== this._dataAnnouncement) {
      this._dataAnnouncement = announcement;
      live.textContent = announcement || "Daten verfügbar.";
    }
    const values = this.shadowRoot.getElementById("values");
    const report = this.shadowRoot.getElementById("report");
    if (values && !existingDetails.has(values)) values.open = this._valuesOpen;
    if (report && !existingDetails.has(report)) report.open = this._reportOpen;
    for (const [id, open] of [["explanation", this._explanationOpen], ["archive-day", this._archiveOpen], ["outlook", this._outlookOpen], ["uncertainty", this._uncertaintyOpen], ["planning", this._planningOpen]]) {
      const section = this.shadowRoot.getElementById(id);
      if (section && !existingDetails.has(section)) section.open = open;
    }
    // Auch das Verschieben eines verbundenen Vorfahren kann den Browserfokus
    // lösen. Nur bei tatsächlichem Verlust fokussieren, damit native Auswahlen
    // und unveränderte Eingaben weiterhin unberührt bleiben.
    if (focused && this.shadowRoot.activeElement !== focused) {
      const target = (focused.isConnected ? focused : null)
        || this.shadowRoot.getElementById(focusId)
        || (focusDay && this.shadowRoot.querySelector(`[data-day="${focusDay}"]`))
        || this.shadowRoot.getElementById("reset-roof")
        || this.shadowRoot.getElementById("roof")
        || this.shadowRoot.querySelector("h2");
      target?.focus({ preventScroll: true });
    }
    this._announceInteraction();
    for (const [node, top, left] of scrollPositions) { node.scrollTop = top; node.scrollLeft = left; }
  }
}

if (globalThis.customElements && !customElements.get("pv-forecast-card")) customElements.define("pv-forecast-card", PvForecastCard);
if (globalThis.window) {
  window.customCards = window.customCards ?? [];
  if (!window.customCards.some((item) => item.type === "pv-forecast-card")) window.customCards.push({ type: "pv-forecast-card", name: "PV Forecast", preview: true, description: "PV-Prognose, lokale Messungen und ehrliches Prognosearchiv.", documentationURL: "https://github.com/dr-dimitri/pv-forecast-ha" });
}

// Die verwaltete Dashboard-Seite bettet dieselbe Karte ein. Sie besitzt keinen
// eigenen Datenabruf, keine Berechnung und keine globale Ressourcenregistrierung.
export class PvForecastPanel extends ElementBase {
  constructor() {
    super();
    if (!this.attachShadow) return;
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;background:var(--primary-background-color,#f4f6f8);color:var(--primary-text-color,#20313c)}
      *{box-sizing:border-box}header{display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:2;padding:8px 16px;background:var(--app-header-background-color,var(--card-background-color,#fff));color:var(--app-header-text-color,var(--primary-text-color,#20313c));border-bottom:1px solid var(--divider-color,#dce3e8)}
      h1{font:500 20px/1.4 var(--paper-font-body1_-_font-family,Roboto,sans-serif);margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      button{display:grid;place-items:center;flex:0 0 44px;width:44px;height:44px;border:0;border-radius:50%;background:transparent;color:inherit;cursor:pointer}button:focus-visible{outline:3px solid var(--primary-color,#008999);outline-offset:2px}button:hover{background:var(--secondary-background-color,#eef3f5)}svg{width:24px;height:24px;fill:currentColor}
      main{max-width:1000px;margin:0 auto;padding:24px}pv-forecast-card{display:block}#message{font:14px/1.5 sans-serif}
      @media(max-width:460px){header{padding:6px 8px}main{padding:12px 8px}}
    </style><header><button id="menu" type="button" aria-label="Menü öffnen"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg></button><h1>PV Forecast</h1></header><main><p id="message" role="status">Dashboard wird geladen …</p></main>`;
    this.shadowRoot.getElementById("menu").addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })));
  }

  set panel(value) { this._panel = value; this._updateCard(); }
  set hass(value) { this._hass = value; this._updateCard(); }
  connectedCallback() { this._updateCard(); }

  _updateCard() {
    if (!this.shadowRoot) return;
    this.shadowRoot.querySelector("h1").textContent = this._panel?.title || "PV Forecast";
    this.shadowRoot.getElementById("menu").setAttribute("aria-label", this._panel?.config?.menu_label || "Menü öffnen");
    const entryId = this._panel?.config?.config_entry_id;
    if (typeof entryId !== "string" || !entryId) {
      this._card?.remove();
      this._card = null;
      this._entryId = null;
      this.shadowRoot.getElementById("message").hidden = false;
      this.shadowRoot.getElementById("message").textContent = "Keine PV-Anlage für dieses Dashboard ausgewählt.";
      return;
    }
    if (!this._card) this._card = document.createElement("pv-forecast-card");
    if (entryId !== this._entryId) {
      this._card.setConfig({ type: "custom:pv-forecast-card", config_entry_id: entryId, day: "today" });
      this._entryId = entryId;
    }
    if (this._hass) this._card.hass = this._hass;
    if (this._card.parentNode !== this.shadowRoot.querySelector("main")) this.shadowRoot.querySelector("main").append(this._card);
    this.shadowRoot.getElementById("message").hidden = true;
  }
}

if (globalThis.customElements && !customElements.get("pv-forecast-panel")) customElements.define("pv-forecast-panel", PvForecastPanel);
