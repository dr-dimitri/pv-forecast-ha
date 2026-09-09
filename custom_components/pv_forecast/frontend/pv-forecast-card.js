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

const plantStamp = (value, timezone) => finite(millis(value)) ? `${formatPlantDate(value, timezone)}, ${formatPlantTime(value, timezone)}` : "unbekannt";

export function renderOutlook(state) {
  const timezone = state.forecast.data.timezone;
  const outlook = state.measurement?.data?.outlook;
  const available = outlook?.schema_version === 1 && outlook.status === "available" && finite(outlook.total_kwh);
  const reason = {
    no_energy_sources: "Es sind noch keine bestätigten AC-Energiequellen vorhanden.",
    no_common_measurement_boundary: "Die Messquellen haben noch keinen gemeinsamen gesicherten Zeitpunkt.",
    unresolved_measurement_identity: "Die Zuordnung mindestens einer Messquelle ist nicht mehr bestätigt.",
    input_fallbacks: "Die Wetterdaten enthalten Ersatzwerte; eine aktuelle Tagesaussicht bleibt offen.",
    incomplete_measurement: "Die Messung seit Tagesbeginn ist nicht vollständig belegt.",
    incomplete_measurements: "Die Messung seit Tagesbeginn ist nicht vollständig belegt.",
    stale_measurement: "Der letzte gesicherte Messwert ist zu alt.",
    stale_measurements: "Der letzte gesicherte Messwert ist zu alt.",
    incomplete_forecast: "Die Prognose deckt den restlichen Tag nicht vollständig ab.",
    stale_forecast: "Der Wetterabruf ist für eine aktuelle Tagesaussicht zu alt.",
  }[outlook?.reason] ?? "Für eine Tagesaussicht fehlen ausreichend belegte Mess- oder Prognosedaten.";
  const metric = (label, value) => `<div><dt>${label}</dt><dd>${energyText(value)} <small>kWh</small></dd></div>`;
  return `<details id="outlook"><summary id="outlook-toggle">Aktuelle Tagesaussicht <span>${available ? `${energyText(outlook.total_kwh)} kWh` : "Noch offen"}</span></summary>${available ? `<p class="feature-result">Heute voraussichtlich insgesamt <strong>${energyText(outlook.total_kwh)} kWh</strong></p><dl class="report-metrics">${metric("Gesichert gemessen", outlook.measured_kwh)}${metric("Geschätzt seit letzter Messung", outlook.bridge_kwh)}${metric("Rest ab jetzt", outlook.remaining_kwh)}</dl><p class="hint">Messung bis ${escapeHtml(plantStamp(outlook.measured_until, timezone))}. Die Zeit seit dieser Messung bleibt eine Schätzung. Rest ab jetzt und geschätzte Brücke überschneiden sich nicht. Kurzfristige Korrektur ist aus.</p>${outlook.quality_flags?.length ? '<p class="hint">Die Tagesaussicht enthält Qualitätsmarkierungen; sie ist keine zugesagte Erzeugung.</p>' : ""}` : `<p class="hint">${escapeHtml(reason)}</p>`}</details>`;
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
  const available = uncertainty?.schema_version === 1 && band?.status === "available" && [band.lower_kwh, band.central_kwh, band.upper_kwh].every(finite);
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
  const output = usable ? `<p class="feature-result">${escapeHtml(plantStamp(plan.start, view.timezone))}<br>bis ${escapeHtml(plantStamp(plan.end, view.timezone))}${finite(plan.energy_kwh) ? `<br><strong>${energyText(plan.energy_kwh)} kWh</strong> erwartet` : ""}</p><p class="hint">${plan.status === "started" ? "Dieses empfohlene Fenster läuft bereits und wird nicht automatisch verschoben." : plan.status === "completed" ? "Dieses empfohlene Fenster ist beendet." : "In diesem zusammenhängenden Fenster wird innerhalb deiner Auswahl besonders viel PV-Energie erwartet."}${plan.hysteresis_applied ? " Bei nur geringfügig geänderter Prognose bleibt die bisherige Empfehlung erhalten." : ""} Wetterabruf: ${escapeHtml(plantStamp(plan.fetched_at, view.timezone))}.</p>${plan.quality_flags?.length ? '<p class="hint">Die Prognose enthält Qualitätsmarkierungen. Das Zeitfenster bleibt eine Schätzung.</p>' : ""}` : result?.status === "loading" ? '<p class="hint" role="status">Zeitfenster wird berechnet …</p>' : result ? `<p class="hint" role="status">${escapeHtml(result.message ?? reason)}</p>` : '<p class="hint">Laufdauer und zulässigen Zeitraum wählen, dann bewusst berechnen.</p>';
  return `<details id="planning"><summary id="planning-toggle">Bestes Solarzeitfenster <span>Gesamtanlage</span></summary><form id="planning-form"><label>Laufdauer in Minuten<input id="planning-duration" name="duration_minutes" type="number" inputmode="numeric" min="1" max="2880" step="1" required value="${escapeHtml(inputs.duration_minutes ?? 120)}"></label><label>Frühester Start<select id="planning-earliest" required>${optionList(inputs.earliest_start)}</select></label><label>Spätestes Ende<select id="planning-latest" required>${optionList(inputs.latest_end)}</select></label><button id="planning-calculate" class="reset-button" type="submit" ${choices.length ? "" : "disabled"}>Zeitfenster berechnen</button></form><p id="planning-input-notice" class="hint" role="status">${planningUI.dirty ? PLANNING_CHANGED_HINT : ""}</p>${output}<p class="hint">Basis sind die vorhandenen Prognoseintervalle mit gleichmäßiger mittlerer Leistung innerhalb jedes Intervalls. Für dieses Fenster gibt es noch kein belastbares Erfahrungsband. Verfügbarer Überschuss hängt zusätzlich von Hausverbrauch und Speicher ab. Es werden keine Geräte eingeschaltet.</p></details>`;
}

export function renderContent(config, state, width = 600, report = null, reportDays = 7, planningUI = {}) {
  const forecast = state?.forecast;
  const view = forecast?.data;
  const title = config.title || view?.plant_name || "PV-Prognose";
  if (!view) return `<div class="header"><div><p class="eyebrow">PV FORECAST</p><h2>${escapeHtml(title)}</h2></div></div><p class="placeholder" role="status">${escapeHtml(forecast?.message || "Prognose wird geladen …")}</p>${forecast?.status === "error" && config.roof_id ? '<button class="reset-button" data-reset-roof>Gesamtanlage anzeigen</button>' : ""}`;
  const roof = view.roofs.find((item) => item.id === view.roof_id);
  const scope = roof?.name ?? "Gesamtanlage";
  const fetchedAt = forecast.envelope?.fetched_at;
  const weatherStamp = finite(millis(fetchedAt)) ? `${formatPlantDate(fetchedAt, view.timezone)}, ${formatPlantTime(fetchedAt, view.timezone)}` : "unbekannt";
  const measurement = state.measurement;
  const total = measurement?.data?.current_location_total_energy ?? measurement?.data?.total_energy;
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
    total?.quality_flags?.length ? "Messdaten enthalten Qualitätsmarkierungen; unvollständige Intervalle bleiben frei." : "",
    selectedSeries(state).history.some((item) => item.quality_flags?.length) ? "Die archivierten Prognosestände enthalten Qualitätsmarkierungen ihrer Eingabedaten." : "",
    measurement?.status === "ready" && !actualComplete ? finite(actual) ? "Ist heute ist nur der bisher belegte Teil; die Tageserfassung ist unvollständig." : "Für heute sind noch keine belegten Messwerte verfügbar." : "",
    state.history?.status === "ready" && !selectedSeries(state).history.length ? "Für diesen Tag sind noch keine Stundenstände im Archiv eingefroren." : "",
  ].filter(Boolean);
  return `<div class="header"><div><p class="eyebrow">PV FORECAST</p><h2>${escapeHtml(title)}</h2><p class="subtitle">${escapeHtml(scope)} · ${escapeHtml(formatPlantDate(view.start, view.timezone))}</p></div><span class="badge ${view.stale ? "warning" : ""}">${view.stale ? "Veraltet" : "Prognose"}</span></div>
    <div class="controls"><div class="day-switch" role="group" aria-label="Prognosetag"><button data-day="today" aria-pressed="${config.day === "today"}">Heute</button><button data-day="tomorrow" aria-pressed="${config.day === "tomorrow"}">Morgen</button></div><label class="roof-label"><span>Fläche</span><select id="roof" aria-label="Fläche"><option value="">Gesamtanlage</option>${view.roofs.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === config.roof_id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label></div>
    <dl class="kpis">${kpi("Heute", view.summary.today_kwh, "Tagesprognose")}${kpi("Morgen", view.summary.tomorrow_kwh, "Tagesprognose")}${kpi("Rest heute", view.summary.remaining_today_kwh, "Ab jetzt erwartet")}${kpi("Ist heute", view.roof_id ? null : actual, actualHint, actualComplete ? "measured" : "incomplete")}</dl>
    <section class="chart-section" aria-label="Tagesverlauf"><div class="chart-heading"><h3>Energie im Tagesverlauf</h3><span>kWh / Intervall</span></div><div class="legend"><span><i class="forecast-key"></i>Aktuelle Prognose</span><span><i class="history-key"></i>${ARCHIVE_LABEL}</span><span><i class="actual-key"></i>Ist</span></div>${renderChart(state, width)}<p class="chart-note">${escapeHtml(view.timezone)} · Ansicht ${escapeHtml(formatPlantTime(view.as_of, view.timezone))}<br>Wetterabruf ${escapeHtml(weatherStamp)}</p></section>
    ${notices.length ? `<div class="notices" role="status">${notices.map((text) => `<p>${escapeHtml(text)}</p>`).join("")}</div>` : ""}
    ${view.roof_id ? "" : `${renderOutlook(state)}${renderUncertainty(state)}${renderPlanning(state, planningUI)}`}
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
  #planning-input-notice:empty{display:none}#planning-form{display:grid;gap:12px;margin-bottom:8px}#planning-form label{display:grid;gap:5px;min-width:0}#planning-form select{width:100%}#planning-form input{font:inherit;font-size:14px;min-height:42px;width:100%;padding:8px 10px;border:1px solid var(--divider-color,#dce3e6);border-radius:7px;background:var(--card-background-color,#fff);color:var(--primary-text-color,#202b32)}input:focus-visible{outline:3px solid var(--primary-color,#007c91);outline-offset:3px}#planning-calculate{margin:2px 0 4px}.feature-result{font-size:14px;line-height:1.7;margin:0 0 12px;overflow-wrap:anywhere}.feature-result strong{font-size:18px}.hint strong{color:var(--primary-text-color,#202b32)}
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
    this._outlookOpen = false;
    this._uncertaintyOpen = false;
    this._planningOpen = false;
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
      const field = { "planning-earliest": "earliest_start", "planning-latest": "latest_end" }[event.target.id];
      if (field) this._updatePlanningInput(field, event.target.value);
    });
    this.shadowRoot.addEventListener("input", (event) => {
      if (event.target.id === "planning-duration") this._updatePlanningInput("duration_minutes", event.target.value);
    });
    this.shadowRoot.addEventListener("submit", (event) => {
      if (event.target.id === "planning-form") { event.preventDefault(); this._calculatePlanning(); }
    });
    this.shadowRoot.addEventListener("toggle", (event) => {
      // Nur das aktuelle Element darf bei einem Neuaufbau seinen Zustand melden.
      if (!event.target.isConnected) return;
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

  setConfig(config) {
    if (!config?.config_entry_id || typeof config.config_entry_id !== "string") throw new Error("Bitte eine PV-Anlage auswählen.");
    if (config.day && !["today", "tomorrow"].includes(config.day)) throw new Error("Der Prognosetag muss Heute oder Morgen sein.");
    if (this._config?.config_entry_id !== config.config_entry_id) {
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
    this._connected = false;
    this._unsubscribe?.(); this._unsubscribe = null;
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._unsubscribePlanning?.(); this._unsubscribePlanning = null;
    this._observer?.disconnect();
    this._visibilityObserver?.disconnect();
    this._visibilityObserver = null;
  }

  _bind() {
    this._unsubscribe?.(); this._unsubscribe = null;
    this._unsubscribeReport?.(); this._unsubscribeReport = null;
    this._unsubscribePlanning?.(); this._unsubscribePlanning = null;
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

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const focused = this.shadowRoot.activeElement;
    const focusId = focused?.id;
    const focusDay = focused?.dataset?.day;
    if (!this._planningInputs && this._state?.forecast?.data) {
      const choices = planningChoices(this._state);
      const now = millis(this._state.forecast.data.as_of);
      this._planningInputs = { duration_minutes: "120", earliest_start: choices.find((value) => millis(value) >= now) ?? choices[0], latest_end: choices.at(-1) };
    }
    this.shadowRoot.innerHTML = `<style>${styles}</style><ha-card><div class="body" aria-busy="${Boolean(this._selectionPending)}">${renderContent(this._config, this._state ? { ...this._state, selectionPending: this._selectionPending } : null, this._width, this._report, this._reportDays, { inputs: this._planningInputs, result: this._planning, dirty: this._planningDirty })}</div></ha-card>`;
    const values = this.shadowRoot.getElementById("values");
    const report = this.shadowRoot.getElementById("report");
    if (values) values.open = this._valuesOpen;
    if (report) report.open = this._reportOpen;
    for (const [id, open] of [["outlook", this._outlookOpen], ["uncertainty", this._uncertaintyOpen], ["planning", this._planningOpen]]) {
      const section = this.shadowRoot.getElementById(id);
      if (section) section.open = open;
    }
    if (focusId) this.shadowRoot.getElementById(focusId)?.focus({ preventScroll: true });
    else if (focusDay) this.shadowRoot.querySelector(`[data-day="${focusDay}"]`)?.focus({ preventScroll: true });
  }
}

if (globalThis.customElements && !customElements.get("pv-forecast-card")) customElements.define("pv-forecast-card", PvForecastCard);
if (globalThis.window) {
  window.customCards = window.customCards ?? [];
  if (!window.customCards.some((item) => item.type === "pv-forecast-card")) window.customCards.push({ type: "pv-forecast-card", name: "PV Forecast", preview: true, description: "PV-Prognose, lokale Messungen und ehrliches Prognosearchiv.", documentationURL: "https://github.com/dr-dimitri/pv-forecast-ha" });
}
