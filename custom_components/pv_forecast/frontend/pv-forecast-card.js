/* PV Forecast: native, rein lesende Lovelace-Karte ohne Laufzeitabhängigkeiten. */

export const REFRESH_MS = 60_000;
export const ARCHIVE_LABEL = "Jeweils 1 Stunde vorher";
const UPDATE_HINT = "Bitte die PV-Forecast-Integration und die Kartenressource aktualisieren. Die Kartenansicht benötigt Datenvertrag 1.";
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
  if (error instanceof Error && error.message === UPDATE_HINT) return { status: "error", message: UPDATE_HINT };
  const code = `${error?.code ?? ""} ${error?.translation_key ?? ""}`;
  if (/unauthorized|auth_required|permission/i.test(code)) return { status: "error", message: `Keine Leseberechtigung für ${source}.` };
  if (/history_unavailable|measurements_unavailable/.test(code)) return { status: "empty", message: `${source} sind für diese Anlage noch nicht verfügbar.` };
  return { status: "error", message: `${source} sind derzeit nicht erreichbar.` };
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

  request(key, loader) {
    const previous = this.requests.get(key);
    if (previous && this.now() - previous.at < REFRESH_MS) return previous.promise;
    const promise = Promise.resolve().then(loader);
    this.requests.set(key, { at: this.now(), promise });
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

export async function loadView(hass, config, publish, active = () => true, cache = null) {
  const read = (service, data) => cache ? cache.request(JSON.stringify([service, data]), () => readService(hass, service, data)) : readService(hass, service, data);
  let view;
  const state = { loading: true, forecast: { status: "loading" }, measurement: { status: "idle" }, history: { status: "idle" } };
  publish({ ...state });
  try {
    const data = await read("get_forecast", { config_entry_id: config.config_entry_id, include_view: true, day: config.day ?? "today", ...(config.roof_id ? { roof_id: config.roof_id } : {}) });
    if (!active()) return;
    view = validateView(data);
    state.forecast = { status: "ready", data: view, envelope: data };
  } catch (error) {
    const message = /roof_not_found/.test(`${error?.translation_key ?? ""} ${error?.code ?? ""}`) ? "Die ausgewählte Dachfläche ist nicht mehr vorhanden." : error instanceof Error && (error.message === UPDATE_HINT || error.message.startsWith("Die Prognoseansicht")) ? error.message : sourceError(error, "Prognosedaten").message;
    publish({ ...state, loading: false, forecast: { status: "error", message } });
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
        config_entry_id: config.config_entry_id, start: view.today_start, end: view.as_of,
        ...(view.day === "today" && view.intervals.length ? { interval_windows: view.intervals.map(({ start, end }) => ({ start, end })) } : {}),
      });
      state.measurement = { status: "ready", data };
    } catch (error) { state.measurement = sourceError(error, "Messdaten"); }
    if (active()) publish({ ...state });
  })();
  const history = (async () => {
    try {
      const data = await read("get_history", { config_entry_id: config.config_entry_id, days: 7, current_targets: true });
      state.history = data.current_targets?.view_version === 1 && Array.isArray(data.current_targets.intervals) ? { status: "ready", data } : { status: "error", message: UPDATE_HINT };
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
    history: view.roof_id ? [] : (state.history?.data?.current_targets?.intervals ?? []).filter((item) => overlap(item, view)),
    actual: view.roof_id || view.day !== "today" ? [] : (state.measurement?.data?.total_intervals ?? []).filter((item) => overlap(item, view)),
  };
}

export function tableRows(state) {
  const series = selectedSeries(state);
  const rows = new Map();
  for (const [name, items] of Object.entries(series)) for (const item of items) {
    const key = `${millis(item.start)}/${millis(item.end)}`;
    if (!rows.has(key)) rows.set(key, { start: item.start, end: item.end });
    const complete = name === "forecast" ? item.is_complete !== false : name === "actual" ? item.energy_complete === true : true;
    rows.get(key)[name] = complete ? item.energy_kwh : null;
  }
  return [...rows.values()].sort((a, b) => millis(a.start) - millis(b.start) || millis(a.end) - millis(b.end));
}

function renderChart(state, width) {
  const view = state.forecast.data;
  const series = selectedSeries(state);
  const { left, top, bottom, x } = plotGeometry(view, width);
  const values = Object.values(series).flat().filter((item) => item.is_complete !== false && item.energy_complete !== false).map((item) => item.energy_kwh).filter(finite);
  const max = Math.max(1, ...values) * 1.15;
  const y = (value) => bottom - value / max * (bottom - top);
  const paths = (items, className, completeKey) => seriesPaths(items, x, y, completeKey).map((path) => `<path class="${className}" d="${path}"/>`).join("");
  const grid = [0, 0.5, 1].map((part) => `<line class="grid" x1="${left}" x2="${width - 12}" y1="${y(max * part)}" y2="${y(max * part)}"/><text class="axis" x="${left - 7}" y="${y(max * part) + 4}" text-anchor="end">${energyText(max * part)}</text>`).join("");
  const tickCount = width < 440 ? 3 : 5;
  const ticks = Array.from({ length: tickCount }, (_, index) => {
    const instant = new Date(millis(view.start) + (millis(view.end) - millis(view.start)) * index / (tickCount - 1)).toISOString();
    const label = formatPlantTime(instant, view.timezone, false);
    const offset = formatPlantTime(instant, view.timezone).split(" ").at(-1);
    return `<text class="axis" x="${x(instant)}" y="219" text-anchor="${index === 0 ? "start" : index === tickCount - 1 ? "end" : "middle"}">${label}<tspan x="${x(instant)}" dy="14">${offset}</tspan></text>`;
  }).join("");
  const actual = series.actual.filter((item) => item.energy_complete === true && finite(item.energy_kwh)).map((item) => {
    const barWidth = Math.max(1, x(item.end) - x(item.start) - 4);
    const height = Math.max(2, bottom - y(item.energy_kwh));
    return `<rect class="actual-bar" x="${x(item.start) + 2}" y="${bottom - height}" width="${barWidth}" height="${height}"><title>${escapeHtml(formatPlantTime(item.start, view.timezone))}: ${energyText(item.energy_kwh)} kWh gemessen</title></rect>`;
  }).join("");
  const now = millis(view.as_of) >= millis(view.start) && millis(view.as_of) < millis(view.end) ? `<line class="now" x1="${x(view.as_of)}" x2="${x(view.as_of)}" y1="8" y2="${bottom}"/><text class="now-label" x="${Math.min(width - 32, Math.max(left + 15, x(view.as_of)))}" y="10" text-anchor="middle">Jetzt</text>` : "";
  const gaps = series.forecast.filter((item) => item.is_complete === false || !finite(item.energy_kwh)).map((item) => `<rect class="gap" x="${x(item.start)}" y="${top}" width="${x(item.end) - x(item.start)}" height="${bottom - top}"/>`).join("");
  return `<svg class="chart" viewBox="0 0 ${width} 242" role="img" aria-labelledby="chart-title chart-description">
    <title id="chart-title">Energie je Intervall in kWh</title><desc id="chart-description">Aktuelle Prognose durchgezogen, ${ARCHIVE_LABEL} gestrichelt, Messwerte als Rechtecke. Fehlende Werte bleiben leer. Alle Werte stehen auch in der Tabelle.</desc>
    <defs><clipPath id="plot-clip"><rect x="${left}" y="0" width="${width - left - 12}" height="${bottom + 2}"/></clipPath></defs>
    ${grid}<g clip-path="url(#plot-clip)">${gaps}${actual}${paths(series.history, "history-line")}${paths(series.forecast, "forecast-line", "is_complete")}${now}</g>${ticks}
    ${values.length ? "" : `<text class="empty-plot" x="${width / 2}" y="100" text-anchor="middle">Noch keine Intervallwerte</text>`}
  </svg>`;
}

function statusText(section, source) {
  if (section?.status === "loading") return `${source} werden geladen …`;
  if (section?.message) return section.message;
  return "";
}

function renderTable(state) {
  const view = state.forecast.data;
  const rows = tableRows(state);
  return `<details id="values"><summary id="values-toggle">Intervallwerte anzeigen <span>${rows.length} Intervalle</span></summary><p class="hint">kWh je angegebenem Intervall. „—“ bedeutet fehlend; 0 ist ein gültiger Wert. Zeitangaben gelten für ${escapeHtml(view.timezone)}.</p><table><caption class="sr-only">Intervallenergie in kWh</caption><thead><tr><th scope="col">Zeit</th><th scope="col">Prognose</th><th scope="col">1 Stunde<br>vorher</th><th scope="col">Ist</th></tr></thead><tbody>${rows.map((row) => `<tr><th scope="row"><time datetime="${escapeHtml(row.start)}">${escapeHtml(formatPlantTime(row.start, view.timezone))}</time><span class="until">bis ${escapeHtml(formatPlantTime(row.end, view.timezone))}</span></th><td>${energyText(row.forecast)}</td><td>${energyText(row.history)}</td><td>${energyText(row.actual)}</td></tr>`).join("")}</tbody></table></details>`;
}

export function renderReport(report, days) {
  if (!report) return `<p class="hint" role="status">Bericht wird geladen …</p>`;
  if (report.message) return `<p class="hint" role="status">${escapeHtml(report.message)}</p>`;
  const data = report.data;
  const metrics = data?.horizons?.hourly_1h;
  if (!metrics) return `<p class="hint">Noch keine abgeschlossenen Zielintervalle im Archiv.</p>`;
  return `<p class="hint">${days} abgeschlossene lokale Tage · ${ARCHIVE_LABEL}. Nur vollständig belegte, vergleichbare Intervalle gehen in die Fehlermaße ein.</p><dl class="report-metrics"><div><dt>MAE</dt><dd>${energyText(metrics.mae_kwh)} <small>kWh</small></dd></div><div><dt>Bias</dt><dd>${energyText(metrics.bias_kwh)} <small>kWh</small></dd></div><div><dt>Stichprobe</dt><dd>${escapeHtml(metrics.count_valid ?? 0)} <small>Intervalle</small></dd></div><div><dt>Abdeckung</dt><dd>${finite(metrics.coverage) ? energyText(metrics.coverage * 100) : "—"} <small>%</small></dd></div></dl><p class="hint">MAE: mittlerer absoluter Fehler. Bias: Prognose minus Messung; positive Werte bedeuten Überschätzung.${data.retention_truncated ? " Die Aufbewahrungsgrenze hat ältere Daten gekürzt." : ""}${data.enabled === false ? " Die Erfassung ist pausiert." : ""}</p>`;
}

export function renderContent(config, state, width = 600, report = null, reportDays = 7) {
  const forecast = state?.forecast;
  const view = forecast?.data;
  const title = config.title || view?.plant_name || "PV-Prognose";
  if (!view) return `<div class="header"><div><p class="eyebrow">PV FORECAST</p><h2>${escapeHtml(title)}</h2></div></div><p class="placeholder" role="status">${escapeHtml(forecast?.message || "Prognose wird geladen …")}</p>${forecast?.status === "error" && config.roof_id ? '<button class="reset-button" data-reset-roof>Gesamtanlage anzeigen</button>' : ""}`;
  const roof = view.roofs.find((item) => item.id === view.roof_id);
  const scope = roof?.name ?? "Gesamtanlage";
  const fetchedAt = forecast.envelope?.fetched_at;
  const weatherStamp = finite(millis(fetchedAt)) ? `${formatPlantDate(fetchedAt, view.timezone)}, ${formatPlantTime(fetchedAt, view.timezone)}` : "unbekannt";
  const measurement = state.measurement;
  const total = measurement?.data?.total_energy;
  const actualComplete = total?.energy_complete === true;
  const actual = total?.energy_kwh;
  const actualHint = view.roof_id ? "Keine Dachmessung" : measurement?.status === "ready" ? actualComplete ? "Seit Tagesbeginn" : finite(actual) ? "Unvollständig erfasst" : "Noch keine Messwerte" : measurement?.status === "loading" ? "Messdaten laden …" : "Keine Messdaten";
  const kpi = (name, value, detail, className = "") => `<div class="kpi ${className}"><dt>${name}</dt><dd>${energyText(value)} <small>kWh</small></dd><span>${detail}</span></div>`;
  const hasFlags = view.intervals.some((item) => item.quality_flags?.length);
  const notices = [
    state.selectionPending ? "Auswahl wird geladen; bis dahin sind noch die bisherigen Werte sichtbar." : "",
    view.stale ? "Prognosestand veraltet. Der letzte verfügbare Stand bleibt erkennbar." : "",
    !view.complete ? "Prognose unvollständig. Schattierte Lücken werden nicht als null Ertrag dargestellt." : "",
    hasFlags ? "Eingabedaten enthalten Qualitätsmarkierungen. Das ist keine gemessene Prognosegüte." : "",
    statusText(measurement, "Messdaten"), statusText(state.history, "Archivdaten"),
    measurement?.data?.total_energy?.quality_flags?.length ? "Messdaten enthalten Qualitätsmarkierungen; unvollständige Intervalle bleiben frei." : "",
    selectedSeries(state).history.some((item) => item.quality_flags?.length) ? "Die archivierten Prognosestände enthalten Qualitätsmarkierungen ihrer Eingabedaten." : "",
    measurement?.status === "ready" && !actualComplete ? finite(actual) ? "Ist heute ist nur der bisher belegte Teil; die Tageserfassung ist unvollständig." : "Für heute sind noch keine belegten Messwerte verfügbar." : "",
    state.history?.status === "ready" && !selectedSeries(state).history.length ? "Für diesen Tag sind noch keine Stundenstände im Archiv eingefroren." : "",
  ].filter(Boolean);
  return `<div class="header"><div><p class="eyebrow">PV FORECAST</p><h2>${escapeHtml(title)}</h2><p class="subtitle">${escapeHtml(scope)} · ${escapeHtml(formatPlantDate(view.start, view.timezone))}</p></div><span class="badge ${view.stale ? "warning" : ""}">${view.stale ? "Veraltet" : "Prognose"}</span></div>
    <div class="controls"><div class="day-switch" role="group" aria-label="Prognosetag"><button data-day="today" aria-pressed="${config.day === "today"}">Heute</button><button data-day="tomorrow" aria-pressed="${config.day === "tomorrow"}">Morgen</button></div><label class="roof-label"><span>Fläche</span><select id="roof" aria-label="Fläche"><option value="">Gesamtanlage</option>${view.roofs.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === config.roof_id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label></div>
    <dl class="kpis">${kpi("Heute", view.summary.today_kwh, "Tagesprognose")}${kpi("Morgen", view.summary.tomorrow_kwh, "Tagesprognose")}${kpi("Rest heute", view.summary.remaining_today_kwh, "Ab jetzt erwartet")}${kpi("Ist heute", view.roof_id ? null : actual, actualHint, actualComplete ? "measured" : "incomplete")}</dl>
    <section class="chart-section" aria-label="Tagesverlauf"><div class="chart-heading"><h3>Energie im Tagesverlauf</h3><span>kWh / Intervall</span></div><div class="legend"><span><i class="forecast-key"></i>Aktuelle Prognose</span><span><i class="history-key"></i>${ARCHIVE_LABEL}</span><span><i class="actual-key"></i>Ist</span></div>${renderChart(state, width)}<p class="chart-note">${escapeHtml(view.timezone)} · Ansicht ${escapeHtml(formatPlantTime(view.as_of, view.timezone))}<br>Wetterabruf ${escapeHtml(weatherStamp)}</p></section>
    ${notices.length ? `<div class="notices" role="status">${notices.map((text) => `<p>${escapeHtml(text)}</p>`).join("")}</div>` : ""}
    ${renderTable(state)}
    ${view.roof_id ? "" : `<details id="report"><summary id="report-toggle">Prognosegüte im Archiv <span>Gesamtanlage</span></summary><label class="report-label">Zeitraum<select id="report-days"><option value="7" ${reportDays === 7 ? "selected" : ""}>7 Tage</option><option value="30" ${reportDays === 30 ? "selected" : ""}>30 Tage</option></select></label>${renderReport(report ?? (state.history?.status === "ready" && reportDays === 7 ? state.history : null), reportDays)}</details>`}`;
}

const styles = `
  :host{display:block;--pv-line:var(--primary-color,#007c91);--pv-archive:var(--secondary-text-color,#636b73);--pv-actual:var(--accent-color,#e0a53b);color:var(--primary-text-color,#202b32);font-family:var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
  *{box-sizing:border-box}ha-card{display:block;background:var(--ha-card-background,var(--card-background-color,#fff));border-radius:var(--ha-card-border-radius,16px);border:var(--ha-card-border-width,1px) solid var(--ha-card-border-color,var(--divider-color,#e4e8eb));box-shadow:var(--ha-card-box-shadow,none);overflow:hidden} .body{padding:22px 22px 8px;min-width:0}
  .header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.eyebrow{font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--secondary-text-color,#64717a);margin:0 0 6px}h2{font-size:22px;font-weight:600;letter-spacing:-.025em;line-height:1.25;margin:0;overflow-wrap:anywhere}.subtitle{color:var(--secondary-text-color,#64717a);font-size:13px;margin:6px 0 0;overflow-wrap:anywhere}.badge{font-size:11px;border:1px solid var(--divider-color,#e4e8eb);padding:5px 8px;border-radius:20px;white-space:nowrap}.warning{color:var(--warning-color,#956400)}
  .controls{display:flex;align-items:flex-end;gap:16px;justify-content:space-between;margin:24px 0 20px}.day-switch{display:flex;background:var(--secondary-background-color,#f2f5f6);padding:3px;border-radius:9px}.day-switch button{border:0;border-radius:7px;background:transparent;color:var(--secondary-text-color,#64717a);font:inherit;font-size:13px;min-height:40px;padding:0 15px;cursor:pointer}.day-switch button[aria-pressed=true]{background:var(--card-background-color,#fff);box-shadow:0 1px 3px #0002;color:var(--primary-text-color,#202b32);font-weight:600}label{color:var(--secondary-text-color,#64717a);font-size:11px}.roof-label{min-width:0;max-width:50%;display:grid;gap:4px}select{font:inherit;font-size:13px;color:var(--primary-text-color,#202b32);background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#dce3e6);border-radius:7px;min-height:40px;max-width:100%;padding:7px 25px 7px 10px}button:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:3px}
  .kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;margin:0 0 26px;padding:0}.kpi{min-width:0;padding:0 10px 0 14px;border-left:1px solid var(--divider-color,#e4e8eb)}.kpi:first-child{border-left:0;padding-left:0}.kpi dt{font-size:12px;color:var(--secondary-text-color,#64717a);margin-bottom:7px}.kpi dd{margin:0;font-size:25px;font-variant-numeric:tabular-nums;letter-spacing:-.035em;font-weight:600;white-space:nowrap}.kpi small{font-size:11px;font-weight:400;color:var(--secondary-text-color,#64717a);letter-spacing:0}.kpi>span{display:block;color:var(--secondary-text-color,#64717a);font-size:10px;line-height:1.4;margin-top:5px}.measured dd{color:var(--pv-line)}
  .chart-heading{display:flex;justify-content:space-between;align-items:baseline;gap:8px}h3{font-size:13px;font-weight:600;margin:0}.chart-heading>span{font-size:10px;color:var(--secondary-text-color,#64717a)}.legend{display:flex;flex-wrap:wrap;gap:8px 16px;margin:12px 0 8px;color:var(--secondary-text-color,#64717a);font-size:10px;line-height:1.4}.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{display:inline-block;width:18px;flex-shrink:0}.forecast-key{border-top:3px solid var(--pv-line)}.history-key{border-top:2px dashed var(--pv-archive)}.actual-key{height:8px;background:var(--pv-actual);border:1px solid var(--primary-text-color,#202b32)}.chart{display:block;width:100%;height:auto;overflow:visible}.grid{stroke:var(--divider-color,#e4e8eb);stroke-width:1}.axis{fill:var(--secondary-text-color,#64717a);font-size:10px}.axis tspan{font-size:8px}.forecast-line{stroke:var(--pv-line);stroke-width:2.5;fill:none;stroke-linejoin:round}.history-line{stroke:var(--pv-archive);stroke-width:2;stroke-dasharray:5 4;fill:none}.actual-bar{fill:var(--pv-actual);fill-opacity:.25;stroke:var(--pv-actual);stroke-width:1}.now{stroke:var(--secondary-text-color,#64717a);stroke-width:1;stroke-dasharray:2 4}.now-label{font-size:10px;fill:var(--secondary-text-color,#64717a)}.gap{fill:var(--secondary-text-color,#64717a);opacity:.09}.empty-plot{font-size:12px;fill:var(--secondary-text-color,#64717a)}.chart-note{font-size:10px;color:var(--secondary-text-color,#64717a);text-align:right;margin:0 0 17px;overflow-wrap:anywhere}
  .notices{padding:9px 11px;margin:0 0 16px;background:var(--secondary-background-color,#f2f5f6);border-radius:8px}.notices p{font-size:11px;line-height:1.5;color:var(--secondary-text-color,#64717a);margin:3px 0}.placeholder{font-size:14px;line-height:1.6;color:var(--secondary-text-color,#64717a);padding:28px 0;min-height:100px}
  .reset-button{min-height:44px;padding:10px 15px;border-radius:7px;border:1px solid var(--divider-color,#dce3e6);background:var(--card-background-color,#fff);color:var(--primary-color,#007c91);font:inherit;font-size:13px;margin-bottom:16px;cursor:pointer}
  details{border-top:1px solid var(--divider-color,#e4e8eb)}summary{min-height:48px;padding:15px 0;font-size:12px;font-weight:500;cursor:pointer;line-height:1.5}summary span{font-size:10px;color:var(--secondary-text-color,#64717a);float:right;font-weight:400;margin-left:5px}.hint{font-size:11px;line-height:1.6;color:var(--secondary-text-color,#64717a);margin:0 0 14px}.report-label{display:flex;align-items:center;gap:12px;margin:0 0 12px}.report-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:12px 0 16px}.report-metrics dt{font-size:11px;color:var(--secondary-text-color,#64717a)}.report-metrics dd{margin:4px 0 0;font-size:19px}.report-metrics small{font-size:10px;color:var(--secondary-text-color,#64717a)}
  table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:11px;margin-bottom:12px}th,td{padding:9px 3px;border-bottom:1px solid var(--divider-color,#e4e8eb);text-align:right;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}th:first-child{width:40%;text-align:left}thead th{font-size:10px;font-weight:500;color:var(--secondary-text-color,#64717a)}tbody th{font-weight:400;font-size:10px}.until{display:block;color:var(--secondary-text-color,#64717a);font-size:9px;margin-top:3px}.sr-only{position:absolute;clip:rect(0,0,0,0);width:1px;height:1px;overflow:hidden}
  @container (max-width:460px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:19px 0}.kpi:nth-child(3){border-left:0;padding-left:0}.kpi dd{font-size:27px}.body{padding:18px 16px 6px}.controls{gap:10px;margin-top:20px}.day-switch button{padding:0 12px}h2{font-size:21px}.badge{font-size:10px}.legend{column-gap:12px}}
  :host{container-type:inline-size}
`;

const ElementBase = globalThis.HTMLElement ?? class {};
export class PvForecastCard extends ElementBase {
  static getConfigForm() {
    return {
      schema: [
        { name: "config_entry_id", required: true, selector: { config_entry: { integration: "pv_forecast" } } },
        { name: "title", selector: { text: {} } },
        { name: "day", selector: { select: { options: [{ value: "today", label: "Heute" }, { value: "tomorrow", label: "Morgen" }], mode: "dropdown" } } },
      ],
      computeLabel: (schema) => ({ config_entry_id: "PV-Anlage", title: "Titel (optional)", day: "Anfänglich angezeigter Tag" })[schema.name] ?? schema.name,
    };
  }

  static getStubConfig(hass) {
    const entity = Object.values(hass?.entities ?? {}).find((item) => item.platform === "pv_forecast" && item.config_entry_id);
    return { config_entry_id: entity?.config_entry_id ?? "", day: "today" };
  }

  constructor() {
    super();
    this._width = 550;
    this._reportDays = 7;
    this._reportOpen = false;
    this._valuesOpen = false;
    if (!this.attachShadow) return;
    this.attachShadow({ mode: "open" });
    this.shadowRoot.addEventListener("click", (event) => {
      if (event.target.closest?.("[data-reset-roof]")) { this._config = { ...this._config, roof_id: undefined }; this._bind(); return; }
      const day = event.target.closest?.("[data-day]")?.dataset.day;
      if (day && day !== this._config.day) { this._config = { ...this._config, day }; this._bind(); }
    });
    this.shadowRoot.addEventListener("change", (event) => {
      if (event.target.id === "roof") { this._config = { ...this._config, roof_id: event.target.value || undefined }; this._bind(); }
      if (event.target.id === "report-days") { this._reportDays = Number(event.target.value); this._bindReport(); this._render(); }
    });
    this.shadowRoot.addEventListener("toggle", (event) => {
      // Nur das aktuelle Element darf bei einem Neuaufbau seinen Zustand melden.
      if (!event.target.isConnected) return;
      if (event.target.id === "values") this._valuesOpen = event.target.open;
      if (event.target.id === "report" && this._reportOpen !== event.target.open) {
        this._reportOpen = event.target.open;
        this._bindReport();
      }
    }, true);
  }

  setConfig(config) {
    if (!config?.config_entry_id || typeof config.config_entry_id !== "string") throw new Error("Bitte eine PV-Anlage auswählen.");
    if (config.day && !["today", "tomorrow"].includes(config.day)) throw new Error("Der Prognosetag muss Heute oder Morgen sein.");
    if (this._config?.config_entry_id !== config.config_entry_id) this._state = null;
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
    this._connected = false;
    this._unsubscribe?.(); this._unsubscribe = null;
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._observer?.disconnect();
    this._visibilityObserver?.disconnect();
    this._visibilityObserver = null;
  }

  _bind() {
    this._unsubscribe?.(); this._unsubscribe = null;
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._selectionPending = Boolean(this._state?.forecast?.data && (this._state.forecast.data.day !== this._config?.day || (this._state.forecast.data.roof_id ?? null) !== (this._config?.roof_id ?? null)));
    this._report = null;
    if (!this._connected || this._visible === false || !this._hass || !this._config) return;
    const cache = connectionCache(this._hass);
    const config = { ...this._config };
    this._unsubscribe = cache.subscribe(JSON.stringify([config.config_entry_id, config.day, config.roof_id ?? null]), (publish, active) => loadView(this._hass, config, publish, active, cache), (state) => {
      if (!this._connected) return;
      // Bei einer laufenden Auswahl bleiben Bedienelemente und Tastaturfokus bestehen.
      if (state.forecast?.status === "loading" && this._state?.forecast?.data) return;
      this._selectionPending = false;
      this._state = state;
      this._render();
    });
    this._bindReport();
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

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const focused = this.shadowRoot.activeElement;
    const focusId = focused?.id;
    const focusDay = focused?.dataset?.day;
    this.shadowRoot.innerHTML = `<style>${styles}</style><ha-card><div class="body" aria-busy="${Boolean(this._selectionPending)}">${renderContent(this._config, this._state ? { ...this._state, selectionPending: this._selectionPending } : null, this._width, this._report, this._reportDays)}</div></ha-card>`;
    const values = this.shadowRoot.getElementById("values");
    const report = this.shadowRoot.getElementById("report");
    if (values) values.open = this._valuesOpen;
    if (report) report.open = this._reportOpen;
    if (focusId) this.shadowRoot.getElementById(focusId)?.focus({ preventScroll: true });
    else if (focusDay) this.shadowRoot.querySelector(`[data-day="${focusDay}"]`)?.focus({ preventScroll: true });
  }
}

if (globalThis.customElements && !customElements.get("pv-forecast-card")) customElements.define("pv-forecast-card", PvForecastCard);
if (globalThis.window) {
  window.customCards = window.customCards ?? [];
  if (!window.customCards.some((item) => item.type === "pv-forecast-card")) window.customCards.push({ type: "pv-forecast-card", name: "PV Forecast", preview: true, description: "PV-Prognose, lokale Messungen und ehrliches Prognosearchiv.", documentationURL: "https://github.com/dr-dimitri/pv-forecast-ha" });
}
