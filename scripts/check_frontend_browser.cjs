#!/usr/bin/env node
/**
 * Reproduzierbare Browserprüfung der synthetischen Offline-Demo, kein nativer HA-Test.
 * Aufruf: node scripts/check_frontend_browser.cjs --playwright /pfad/zu/playwright
 *         --chromium /pfad/zu/chromium [--prefix ui-111] [--output /tmp/pv-ui-browser]
 * Alternativ: PV_PLAYWRIGHT_MODULE und PV_CHROMIUM_EXECUTABLE. Ohne diese Angaben
 * werden ein bereits installiertes Playwright und dessen Chromium verwendet.
 * Es werden keine Pakete installiert und keine externen Daten angefordert.
 */
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const assert = require("node:assert/strict");

const args = process.argv.slice(2);
function option(name, fallback) {
  const index = args.indexOf(`--${name}`);
  return index < 0 ? fallback : args[index + 1];
}
const root = path.resolve(__dirname, "..");
const output = path.resolve(option("output", "/tmp/pv-ui-browser"));
const prefix = option("prefix", "ui-111");
assert.match(prefix, /^[a-z0-9-]+$/, "Bildpräfix muss ein einfacher Dateiname sein");
const { chromium } = require(option("playwright", process.env.PV_PLAYWRIGHT_MODULE || "playwright"));
const executablePath = option("chromium", process.env.PV_CHROMIUM_EXECUTABLE);
const customTheme = {
  "--primary-background-color": "#e8e4f2", "--primary-color": "#60439a",
  "--accent-color": "#ab3e59", "--primary-text-color": "#251b39",
  "--secondary-text-color": "#665c73", "--card-background-color": "#fffaf6",
  "--secondary-background-color": "#ede6ef", "--divider-color": "#d4cadc",
  "--ha-card-border-radius": "4px",
};

function serverForRepository() {
  return http.createServer((request, response) => {
    const file = path.resolve(root, `.${decodeURIComponent(new URL(request.url, "http://localhost").pathname)}`);
    if (!file.startsWith(`${root}${path.sep}`)) { response.writeHead(403); response.end(); return; }
    try {
      const content = fs.readFileSync(file);
      response.setHeader("Content-Type", file.endsWith(".html") ? "text/html; charset=utf-8" : file.endsWith(".json") ? "application/json" : "text/javascript; charset=utf-8");
      response.end(content);
    } catch { response.writeHead(404); response.end(); }
  });
}

// Dieser Teil läuft im Browser. Farben werden mit dem Browser selbst aufgelöst,
// damit auch color-mix() und durchsichtige Hintergründe korrekt geprüft werden.
function inspectCard(card) {
  const shadow = card.shadowRoot;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  const color = (value) => {
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data].map((part, index) => index === 3 ? part / 255 : part);
  };
  const blend = (front, back) => front.slice(0, 3).map((part, index) => part * front[3] + back[index] * (1 - front[3]));
  const parent = (element) => element.parentElement || element.getRootNode().host;
  const background = (element) => {
    const ancestors = [];
    for (let item = element; item; item = parent(item)) ancestors.push(item);
    return ancestors.reverse().reduce((back, item) => blend(color(getComputedStyle(item).backgroundColor), back), [255, 255, 255]);
  };
  const luminance = (rgb) => rgb.reduce((sum, channel, index) => {
    const value = channel / 255;
    return sum + (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4) * [0.2126, 0.7152, 0.0722][index];
  }, 0);
  const ratio = (first, second) => {
    const a = luminance(first), b = luminance(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const label = (element) => `${element.localName}${element.id ? `#${element.id}` : ""}${typeof element.className === "string" && element.className ? `.${element.className.trim().replaceAll(" ", ".")}` : ""}`;
  const visible = (element) => element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && !element.closest(".sr-only, [aria-hidden=true]") && !["style", "script", "option", "title", "desc"].includes(element.localName);
  const findings = [];
  let minimumText = Infinity, minimumChartText = Infinity, minimumContrast = Infinity;
  let minimumControlContrast = Infinity, minimumLineContrast = Infinity, textCount = 0;
  const cardBounds = card.getBoundingClientRect();
  for (const element of shadow.querySelectorAll("*")) {
    if (!visible(element)) continue;
    const style = getComputedStyle(element);
    const isSvg = element instanceof SVGElement;
    const bounds = element.getBoundingClientRect();
    if (!element.parentElement?.closest(".table-scroll") && (!isSvg || element.matches("text, tspan")) && bounds.width > 0 && (bounds.left < cardBounds.left - 1 || bounds.right > cardBounds.right + 1)) findings.push({ kind: "overflow", element: label(element), left: bounds.left, right: bounds.right, cardRight: cardBounds.right });
    const directText = [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    if (element.matches(".kpi dd") && element.scrollWidth > element.clientWidth + 1) findings.push({ kind: "kpi-overflow", element: label(element), width: element.clientWidth, textWidth: element.scrollWidth });
    if (!directText && !element.matches("input, select")) continue;
    if (!bounds.width || !bounds.height) continue;
    const fontSize = parseFloat(style.fontSize) * (isSvg ? Math.abs(element.getScreenCTM()?.a || 1) : 1);
    if (isSvg) minimumChartText = Math.min(minimumChartText, fontSize);
    else {
      minimumText = Math.min(minimumText, fontSize);
      if (fontSize < 13.9) findings.push({ kind: "font", element: label(element), size: fontSize });
    }
    const back = background(element);
    const contrast = ratio(blend(color(isSvg ? style.fill : style.color), back), back);
    const threshold = fontSize >= 24 || (fontSize >= 18.66 && parseInt(style.fontWeight, 10) >= 700) ? 3 : 4.5;
    minimumContrast = Math.min(minimumContrast, contrast);
    textCount++;
    if (contrast < threshold - 0.01) findings.push({ kind: "text-contrast", element: label(element), contrast, threshold });
    if (element.matches("input, select")) {
      const contour = ratio(blend(color(style.borderTopColor), back), back);
      minimumControlContrast = Math.min(minimumControlContrast, contour);
      if (contour < 2.99) findings.push({ kind: "control-contrast", element: label(element), contrast: contour });
    }
  }
  for (const element of shadow.querySelectorAll(".forecast-line, .history-line, .actual-bar, .hour-tick")) {
    if (!element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;
    const back = background(element);
    const contrast = ratio(blend(color(getComputedStyle(element).stroke), back), back);
    minimumLineContrast = Math.min(minimumLineContrast, contrast);
    if (contrast < 2.99) findings.push({ kind: "chart-contrast", element: label(element), contrast });
  }
  if (document.documentElement.scrollWidth > innerWidth + 1) findings.push({ kind: "page-overflow", width: document.documentElement.scrollWidth, viewport: innerWidth });
  return { findings, textCount, minimumText, minimumChartText, minimumContrast, minimumControlContrast, minimumLineContrast, cardWidth: cardBounds.width };
}

async function runCase(browser, origin, test) {
  const page = await browser.newPage({ viewport: { width: test.viewport, height: 1000 }, hasTouch: Boolean(test.touch), isMobile: Boolean(test.touch) });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // Nur synthetische Fixture-Werte werden für den Belastungsfall verändert.
  if (test.stress) await page.route("**/fixtures.mjs", (route) => {
    const source = fs.readFileSync(path.join(root, "tests/frontend/fixtures.mjs"), "utf8")
      .replaceAll('"Sonnenhaus"', '"Photovoltaikanlage am Mehrgenerationenhaus mit Werkstatt und außergewöhnlich langem Anlagennamen"')
      .replaceAll('"Süddach"', '"Südostdachfläche des Mehrgenerationenhauses mit Werkstattanbau"')
      .replaceAll("23.14", String(test.largeToday ?? 12345.67)).replaceAll("26.9", String(test.largeTomorrow ?? 23456.78)).replaceAll("10.76", "9876.54")
      .replace(/const HOURS = (\[[^\n]+\]);/, "const HOURS = $1.map((value) => value * 1000);");
    return route.fulfill({ contentType: "text/javascript", body: source });
  });
  try {
    const params = new URLSearchParams({ theme: test.theme === "dark" ? "dark" : "light", width: String(test.cardWidth), scenario: test.scenario || "sunny", day: test.day || "today" });
    if (test.panel) params.set("panel", "1");
    await page.goto(`${origin}/tests/frontend/demo.html?${params}`, { waitUntil: "networkidle" });
    if (test.theme === "custom") await page.evaluate((theme) => {
      for (const [name, value] of Object.entries(theme)) document.documentElement.style.setProperty(name, value);
    }, customTheme);
    const card = page.locator("pv-forecast-card").first();
    await card.locator(".kpis").first().waitFor();
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const closed = await card.evaluate(inspectCard);
    const screenshot = `${prefix}-${test.name}.png`;
    await page.screenshot({ path: path.join(output, screenshot), fullPage: true });
    if (test.representative && Number(prefix.split("-").at(-1)) < 113) fs.copyFileSync(path.join(output, screenshot), path.join(root, "docs/images", screenshot));
    await card.locator("details").evaluateAll((items) => { for (const item of items) item.open = true; });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const expanded = await card.evaluate(inspectCard);
    const navigation = Number(prefix.split("-").at(-1)) >= 112 ? await checkNavigation(page, card, test) : null;
    const chart = Number(prefix.split("-").at(-1)) >= 113 ? await checkChart(page, card, test) : null;
    return { chart, navigation, name: test.name, fixtureOnly: true, errors, closed, expanded, screenshot: path.join(output, screenshot) };
  } finally { await page.close(); }
}

// Tageswahl, Abschnittsnavigation und lokale Aktualisierung bleiben unabhängig
// von den nur simulierten HA-Antworten prüfbar.
async function checkNavigation(page, card, test) {
  const expectedDay = test.day || "today";
  const structure = await card.evaluate((element) => {
    const shadow = element.shadowRoot;
    return {
      headings: [...shadow.querySelectorAll(".section-heading")].map((item) => item.textContent),
      chart: shadow.querySelector(".chart-heading h3").textContent,
      today: shadow.querySelector('[aria-label="Heutiger Stand"] h3').textContent,
      todayLabels: [...shadow.querySelectorAll('[aria-label="Heutiger Stand"] dt')].map((item) => item.textContent),
      day: shadow.querySelector('[data-day][aria-pressed="true"]').dataset.day,
      roof: shadow.getElementById("roof").value,
    };
  });
  assert.equal(structure.day, expectedDay);
  assert.ok(structure.chart.endsWith(expectedDay === "tomorrow" ? "Morgen" : "Heute"));
  assert.deepEqual(structure.todayLabels, ["Rest heute", "Ist heute"]);
  assert.match(structure.today, test.scenario === "fold" ? /Heutiger Stand.*25\. Okt/ : /Heutiger Stand.*10\. Sept/);
  assert.deepEqual(structure.headings, test.scenario === "roof" ? ["Tagesübersicht", "Vergleichen"] : ["Tagesübersicht", "Planen", "Vergleichen"]);
  if (test.scenario === "roof") assert.equal(structure.roof, "south");
  for (const button of await card.locator("[data-section]").all()) {
    const target = await button.getAttribute("data-section");
    await button.click();
    assert.equal(await card.evaluate((element) => element.shadowRoot.activeElement?.id), target, "Sprungziel übernimmt den Tastaturfokus");
  }
  if (test.scenario !== "roof") {
    await card.locator("#report-days").selectOption("30");
    await card.locator("#planning-duration").fill("90");
  }
  await card.locator(test.scenario === "roof" ? "#values-toggle" : "#planning-duration").focus();
  const before = await card.evaluate((element) => {
    const panel = element.getRootNode().host;
    const scroll = panel?.localName === "pv-forecast-panel" ? panel : document.scrollingElement;
    scroll.scrollTop = 450;
    const snapshot = () => ({
      scrollTop: scroll.scrollTop,
      focus: element.shadowRoot.activeElement?.id,
      open: [...element.shadowRoot.querySelectorAll("details[open]")].map((item) => item.id).sort(),
      day: element.shadowRoot.querySelector('[data-day][aria-pressed="true"]').dataset.day,
      roof: element.shadowRoot.getElementById("roof").value,
      report: element.shadowRoot.getElementById("report-days")?.value || null,
      duration: element.shadowRoot.getElementById("planning-duration")?.value || null,
    });
    const previous = snapshot();
    element._render();
    return { previous, current: snapshot(), scrollOwner: scroll.localName };
  });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const after = await card.evaluate((element) => {
    const panel = element.getRootNode().host;
    const scroll = panel?.localName === "pv-forecast-panel" ? panel : document.scrollingElement;
    return { scrollTop: scroll.scrollTop, focus: element.shadowRoot.activeElement?.id,
      open: [...element.shadowRoot.querySelectorAll("details[open]")].map((item) => item.id).sort(),
      day: element.shadowRoot.querySelector('[data-day][aria-pressed="true"]').dataset.day,
      roof: element.shadowRoot.getElementById("roof").value,
      report: element.shadowRoot.getElementById("report-days")?.value || null,
      duration: element.shadowRoot.getElementById("planning-duration")?.value || null };
  });
  assert.ok(before.previous.scrollTop > 0, "Scrollprüfung benötigt tatsächlich verschobenen Inhalt");
  assert.deepEqual(before.current, before.previous, "Neuaufbau bewahrt Auswahl, Fokus, offene Abschnitte und Scrollposition");
  assert.deepEqual(after, before.previous, "Zustand bleibt auch nach Browser-Layout erhalten");
  return { structure, preserved: after, scrollOwner: before.scrollOwner };
}

// Echte Browserereignisse prüfen die Auswahl unabhängig von Backendberechnungen.
async function checkChart(page, card, test) {
  const chart = card.locator("#interval-chart");
  const markers = await chart.locator(".hour-tick").evaluateAll((items) => items.map((item) => ({
    x: item.x1.baseVal.value, endX: item.x2.baseVal.value,
    height: item.y2.baseVal.value - item.y1.baseVal.value,
  })));
  assert.equal(markers.length, test.scenario === "fold" ? 26 : 25);
  assert.ok(markers.every((item, index) => item.x === item.endX && item.height === 5 && (!index || item.x > markers[index - 1].x)));
  if (prefix === "ui-127" && ["360-light", "360-dark"].includes(test.name)) {
    const screenshot = `${prefix}-${test.name}-stunden.png`;
    await chart.screenshot({ path: path.join(output, screenshot) });
    fs.copyFileSync(path.join(output, screenshot), path.join(root, "docs/images", screenshot));
  }
  const detail = card.locator("#interval-detail");
  const readDetail = () => detail.evaluate((element) => ({
    text: element.textContent.replace(/\s+/g, " ").trim(),
    stamps: element.dataset.start ? [element.dataset.start, element.dataset.end] : [...element.querySelectorAll("time[datetime]")].map((item) => item.getAttribute("datetime")),
  }));
  const calls = await page.evaluate(() => window.demo.calls.length);
  await card.locator("#chart-explore").click();
  assert.equal(await card.evaluate((element) => element.shadowRoot.activeElement?.id), "interval-chart");
  await chart.press("Home");
  await chart.press("Enter");
  const first = await readDetail();
  assert.match(first.text, /0\s*kWh/, "Eine belegte Null bleibt als null kWh erkennbar");
  await card.locator("#interval-next").click();
  assert.notDeepEqual(await readDetail(), first);
  await card.locator("#interval-prev").click();
  assert.deepEqual(await readDetail(), first);
  await chart.press("End");
  const last = await readDetail();
  assert.notDeepEqual(last, first);
  await chart.press("ArrowRight");
  assert.deepEqual(await readDetail(), last, "Der letzte Randpunkt bleibt beim letzten Intervall");
  await chart.press("ArrowLeft");
  assert.notDeepEqual(await readDetail(), last);
  const plot = await chart.evaluate((element) => {
    const line = element.querySelector(".grid");
    const box = element.getBoundingClientRect();
    return { x: Number(line.getAttribute("x2")) / element.viewBox.baseVal.width * box.width, y: box.height / 2 };
  });
  await chart.click({ position: plot });
  assert.deepEqual(await readDetail(), last, "Klick auf das rechte Plotende wählt das letzte Intervall");
  await chart.press("Home");
  let special = null;
  if (test.scenario === "fold") {
    await chart.press("ArrowRight"); await chart.press("ArrowRight");
    const firstFold = await readDetail();
    await chart.press("ArrowRight");
    const secondFold = await readDetail();
    assert.match(firstFold.text, /02:00\s+UTC\+02:00/);
    assert.match(secondFold.text, /02:00\s+UTC\+01:00/);
    assert.notDeepEqual(firstFold, secondFold);
    special = { firstFold, secondFold };
  } else if (test.scenario === "kolkata") {
    const halfHour = await readDetail();
    assert.match(halfHour.text, /UTC\+05:30/);
    assert.equal(Date.parse(halfHour.stamps[1]) - Date.parse(halfHour.stamps[0]), 1_800_000);
    special = { halfHour };
  } else if (test.scenario === "gaps") {
    for (let index = 0; index < 9; index++) await chart.press("ArrowRight");
    const gap = await readDetail();
    assert.match(gap.text, /fehlend|unvollständig|nicht verfügbar/i);
    assert.notEqual(gap.text, first.text);
    special = { gap };
  }
  const selected = await readDetail();
  await card.evaluate((element) => element._render());
  assert.deepEqual(await readDetail(), selected, "Lokale Aktualisierung bewahrt die Intervallauswahl");
  let refreshed = null;
  if (test.scenario !== "gaps") {
    refreshed = await card.evaluate((element) => {
      const original = element._state;
      const selected = element.shadowRoot.getElementById("interval-detail").dataset.start;
      element._state = { ...original, forecast: { ...original.forecast, data: { ...original.forecast.data,
        intervals: original.forecast.data.intervals.map((interval) => interval.start === selected ? { ...interval, energy_kwh: 2.22 } : interval) } } };
      element._render();
      const updated = { start: element.shadowRoot.getElementById("interval-detail").dataset.start, text: element.shadowRoot.getElementById("interval-detail").textContent };
      element._state = original;
      element._render();
      return { selected, updated };
    });
    assert.equal(refreshed.updated.start, refreshed.selected);
    assert.match(refreshed.updated.text, /2,22\s*kWh/, "Ein neuer Prognosewert erscheint im weiter ausgewählten Intervall");
  }
  const inspection = await card.evaluate(inspectCard);
  await card.locator("#interval-next").focus();
  await page.keyboard.press("Escape");
  assert.equal(await card.evaluate((element) => element.shadowRoot.activeElement?.id), "interval-chart");
  assert.equal(await detail.count(), 0);
  await chart.press("Enter");
  await detail.waitFor();
  await card.locator("#interval-close").click();
  assert.equal(await detail.count(), 0);
  let touchScroll = null;
  if (test.touch) {
    await chart.scrollIntoViewIfNeeded();
    const box = await chart.boundingBox();
    await page.touchscreen.tap(box.x + box.width * 0.55, box.y + box.height * 0.5);
    await detail.waitFor();
    await card.locator("#interval-close").click();
    await chart.evaluate((element) => element.scrollIntoView({ block: "center" }));
    const bounds = await chart.boundingBox();
    const before = await page.evaluate(() => document.scrollingElement.scrollTop);
    const session = await page.context().newCDPSession(page);
    const point = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
    await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [point] });
    for (let step = 1; step <= 5; step++) {
      await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ ...point, y: point.y - step * 30 }] });
      await page.evaluate(() => new Promise(requestAnimationFrame));
    }
    await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const after = await page.evaluate(() => document.scrollingElement.scrollTop);
    await chart.evaluate((element) => element.scrollIntoView({ block: "center" }));
    const zoomBounds = await chart.boundingBox();
    const scaleBefore = await page.evaluate(() => visualViewport.scale);
    await session.send("Input.synthesizePinchGesture", { x: zoomBounds.x + zoomBounds.width / 2, y: zoomBounds.y + zoomBounds.height / 2, scaleFactor: 1.5, gestureSourceType: "touch", relativeSpeed: 400 });
    const scaleAfter = await page.evaluate(() => visualViewport.scale);
    await session.detach();
    assert.ok(scaleAfter > scaleBefore + 0.1, "Pinch-Zoom über dem Diagramm bleibt verfügbar");
    assert.ok(after > before + 20, "Vertikale Touchbewegung über dem Diagramm scrollt die Seite");
    touchScroll = { before, after, scaleBefore, scaleAfter };
  }
  assert.equal(await page.evaluate(() => window.demo.calls.length), calls, "Diagrammbedienung verursacht keine zusätzlichen Leseaufrufe");
  if (test.representative) {
    await card.locator("details").evaluateAll((items) => { for (const item of items) item.open = false; });
    await chart.press("Home");
    for (let index = 0; index < 12; index++) await chart.press("ArrowRight");
    await page.evaluate(() => window.scrollTo(0, 0));
    const screenshot = `${prefix}-${test.name}-detail.png`;
    await page.screenshot({ path: path.join(output, screenshot), fullPage: true });
    fs.copyFileSync(path.join(output, screenshot), path.join(root, "docs/images", screenshot));
  }
  const nextDay = test.day === "tomorrow" ? "today" : "tomorrow";
  await card.locator(`[data-day="${nextDay}"]`).click();
  assert.equal(await detail.count(), 0, "Eine Tagesänderung verwirft die alte Intervallauswahl");
  return { first, last, special, refreshed, inspection, touchScroll, readCalls: calls };
}

async function checkDataStates(browser, origin) {
  const page = await browser.newPage({ viewport: { width: 360, height: 900 } });
  try {
    for (const [scenario, expected] of [["no-source", "Keine Messquelle zugeordnet"], ["no-measurement", "Noch keine Messung"], ["archive-off", "Archiv ausgeschaltet"], ["archive-empty", "Archiv noch leer"], ["acl", "Leseberechtigung fehlt"], ["old", "Datenversion nicht kompatibel"], ["outage", "Prognose derzeit nicht erreichbar"], ["zero", "Ist heute"]]) {
      await page.goto(`${origin}/tests/frontend/demo.html?scenario=${scenario}&width=360`);
      const card = page.locator("pv-forecast-card");
      await card.getByText(expected, { exact: false }).first().waitFor();
      const unchanged = await card.evaluate(async (element) => {
        const live = element.shadowRoot.getElementById("data-status");
        let changes = 0;
        const observer = new MutationObserver(() => changes++);
        observer.observe(live, { childList: true, subtree: true, characterData: true });
        element._render(); element._render();
        await Promise.resolve(); observer.disconnect();
        return { changes, same: live === element.shadowRoot.getElementById("data-status"), connected: live.isConnected };
      });
      assert.deepEqual(unchanged, { changes: 0, same: true, connected: true });
      const inspection = await card.evaluate(inspectCard);
      assert.deepEqual(inspection.findings, [], scenario);
      if (prefix === "ui-114" && ["no-source", "acl"].includes(scenario)) {
        const filename = `${prefix}-360-${scenario}.png`;
        await page.screenshot({ path: path.join(output, filename), fullPage: true });
        fs.copyFileSync(path.join(output, filename), path.join(root, "docs/images", filename));
      }
    }
    await page.goto(`${origin}/tests/frontend/demo.html?scenario=sunny&width=360`);
    const card = page.locator("pv-forecast-card");
    await card.locator(".kpis").first().waitFor();
    await page.waitForFunction(() => document.querySelector("pv-forecast-card")._state?.loading === false);
    const transitions = await card.evaluate(async (element) => {
      const { retainReadState, sourceError } = await import("/custom_components/pv_forecast/frontend/pv-forecast-card.js");
      const original = element._state;
      const live = element.shadowRoot.getElementById("data-status");
      const error = { forecast: sourceError({}, "Prognosedaten"), measurement: { status: "idle" }, history: { status: "idle" } };
      element._state = retainReadState(original, error); element._render();
      const retained = element.shadowRoot.textContent.includes("23,14") && element.shadowRoot.textContent.includes("Letzte gelesene Ansicht");
      const first = live.textContent;
      element._state = retainReadState(element._state, error); element._render();
      const repeated = live.textContent === first;
      element._state = retainReadState(element._state, { forecast: sourceError({code: "unauthorized"}, "Prognosedaten") }); element._render();
      const cleared = !element.shadowRoot.querySelector(".kpis");
      element._state = original; element._render();
      return { retained, repeated, cleared, recovered: live.textContent === "Daten verfügbar." };
    });
    assert.deepEqual(transitions, { retained: true, repeated: true, cleared: true, recovered: true });
    console.log("Datenzustände: 8 Fälle, stille Wiederholung und Fehler/Erholung bestanden");
  } finally { await page.close(); }
}

async function checkDelayedRoofFocus(browser, origin) {
  for (const target of ["#values-toggle", ".table-scroll", "#nav-comparison"]) {
    const page = await browser.newPage({ viewport: { width: 360, height: 900 } });
    try {
      await page.goto(`${origin}/tests/frontend/demo.html?width=360`);
      const card = page.locator("pv-forecast-card");
      await page.waitForFunction(() => document.querySelector("pv-forecast-card")._state?.loading === false);
      await card.locator("#values-toggle").click();
      await card.evaluate((element) => {
        const callWS = element._hass.callWS.bind(element._hass);
        element._hass = { ...element._hass, async callWS(message) {
          if (message.service === "get_forecast" && message.service_data.roof_id) {
            await new Promise((resolve) => { element._releaseRoofResponse = resolve; });
          }
          return callWS(message);
        } };
      });
      await card.locator("#roof").selectOption("south");
      await page.waitForFunction(() => document.querySelector("pv-forecast-card")._releaseRoofResponse);
      // Während die Antwort aussteht, navigiert der Anwender bereits weiter.
      await card.locator(target).focus();
      const before = await card.evaluate((element) => {
        element._focusedBeforeRoof = element.shadowRoot.activeElement;
        const result = { scroll: window.scrollY, open: element.shadowRoot.getElementById("values").open };
        element._releaseRoofResponse();
        return result;
      });
      await page.waitForFunction(() => document.querySelector("pv-forecast-card")._state?.forecast?.data?.roof_id === "south");
      const after = await card.evaluate((element) => ({
        same: element._focusedBeforeRoof === element.shadowRoot.activeElement,
        connected: element._focusedBeforeRoof.isConnected,
        scroll: window.scrollY, open: element.shadowRoot.getElementById("values").open,
      }));
      assert.equal(after.same, true, `${target}: Tatsächlicher Fokus bleibt nach dem Verschieben erhaltener Knoten bestehen`);
      assert.equal(after.connected, true);
      assert.equal(after.open, before.open);
      // Bei kürzerer Dachansicht begrenzt der Browser die Dokumentposition nativ.
      const maxScroll = await page.evaluate(() => document.scrollingElement.scrollHeight - innerHeight);
      assert.equal(after.scroll, Math.min(before.scroll, maxScroll));
      assert.equal(await page.evaluate(() => window.demo.calls.length), 4, "Fokuskorrektur erzeugt keine weiteren Leseaufrufe");
    } finally { await page.close(); }
  }
  console.log("Verzögerter Dachwechsel: Summary, Tabellenregion und Bereichsnavigation behalten den Fokus");
}

async function checkAccessibility(browser, origin) {
  const results = [];
  for (const theme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width: 320, height: 900 }, reducedMotion: "reduce" });
    try {
      await page.goto(`${origin}/tests/frontend/demo.html?width=360&theme=${theme}`);
      const card = page.locator("pv-forecast-card");
      await page.waitForFunction(() => document.querySelector("pv-forecast-card")._state?.loading === false);
      const immediateToggle = await card.evaluate((element) => {
        const details = element.shadowRoot.getElementById("planning");
        details.open = true;
        element._render();
        return details.isConnected && details.open;
      });
      assert.equal(immediateToggle, true, "Update vor dem verzögerten toggle-Ereignis schließt keine Details");
      const duration = card.locator("#planning-duration");
      await duration.fill("123");
      await duration.press("ArrowLeft"); await duration.press("ArrowLeft");
      await card.evaluate((element) => {
        element._testInput = element.shadowRoot.getElementById("planning-duration");
        element._state.forecast.data.summary.today_kwh = 24;
        element._render();
      });
      await duration.press("9");
      assert.equal(await duration.inputValue(), "1923", "Cursorposition überlebt das Update während der Eingabe");
      assert.equal(await card.evaluate((element) => element._testInput === element.shadowRoot.activeElement), true);
      // Eine native Auswahl wird weder ersetzt noch werden ihre Optionen verändert.
      const earliest = card.locator("#planning-earliest");
      await earliest.focus(); await earliest.press("Alt+ArrowDown");
      const select = await card.evaluate(async (element) => {
        const select = element.shadowRoot.activeElement;
        const first = select.firstChild;
        element._state.forecast.data.as_of = new Date(Date.parse(element._state.forecast.data.as_of) + 60000).toISOString();
        let mutations = 0;
        const observer = new MutationObserver(() => mutations++);
        observer.observe(select, { childList: true, attributes: true, subtree: true });
        element._render(); await Promise.resolve(); observer.disconnect();
        return { same: select === element.shadowRoot.activeElement, first: first === select.firstChild, mutations };
      });
      assert.deepEqual(select, { same: true, first: true, mutations: 0 });
      await earliest.press("Escape"); await earliest.press("Tab");
      await card.locator("#values-toggle").click();
      const table = card.locator(".table-scroll");
      await table.focus(); await table.press("ArrowRight");
      const continuity = await card.evaluate((element) => {
        const table = element.shadowRoot.querySelector(".table-scroll");
        table.scrollLeft = 80;
        const before = { left: table.scrollLeft, scroll: window.scrollY };
        element._render();
        return { before, after: { left: table.scrollLeft, scroll: window.scrollY }, same: table === element.shadowRoot.activeElement, details: element.shadowRoot.getElementById("values").open };
      });
      assert.deepEqual(continuity.before, continuity.after);
      assert.equal(continuity.same && continuity.details, true);
      await card.locator("#interval-chart").focus(); await card.locator("#interval-chart").press("Enter");
      const announcement = await card.evaluate((element) => {
        const live = element.shadowRoot.getElementById("interaction-status");
        const before = live.textContent;
        element._render();
        return { before, unchanged: before === live.textContent };
      });
      assert.match(announcement.before, /UTC\+02:00/); assert.equal(announcement.unchanged, true);
      await card.locator("#interval-chart").press("Escape");
      assert.equal(await card.locator("#interval-detail").count(), 0);
      // Textvergrößerung verdoppelt tatsächlich die Schrift, nicht nur das Viewportbild.
      await card.evaluate((element) => {
        element._state.forecast.data.summary.tomorrow_kwh = 123456.78;
        element._state.forecast.data.plant_name = "Photovoltaikanlage am Mehrgenerationenhaus mit Werkstattanbau";
        element._render();
      });
      await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
      await card.locator("details").evaluateAll((items) => { for (const item of items) item.open = true; });
      const inspection = await card.evaluate(inspectCard);
      assert.deepEqual(inspection.findings, []);
      assert.ok(inspection.minimumText >= 27.9);
      const controls = await card.locator("button, select, input, summary").evaluateAll((items) => items.filter((item) => item.checkVisibility()).map((item) => ({ id: item.id || item.textContent, width: item.getBoundingClientRect().width, height: item.getBoundingClientRect().height })));
      assert.ok(controls.every((item) => item.width >= 44 && item.height >= 44), JSON.stringify(controls));
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(output, `qa-320-${theme}-top.png`) });
      const screenshot = `${prefix}-320-${theme}-text200.png`;
      await page.screenshot({ path: path.join(output, screenshot), fullPage: true });
      fs.copyFileSync(path.join(output, screenshot), path.join(root, "docs/images", screenshot));
      // Entfernte Dachansicht: ein vorhandenes, sinnvoll benanntes Ziel erhält Fokus.
      await card.locator("#roof").focus();
      const removed = await card.evaluate((element) => {
        element._config.roof_id = "removed";
        element._state = { forecast: { status: "error", reason: "roof_removed", message: "Die ausgewählte Dachfläche ist nicht mehr vorhanden." } };
        element._render();
        return element.shadowRoot.activeElement?.id;
      });
      assert.equal(removed, "reset-roof");
      await card.locator("#reset-roof").click();
      await card.locator("#roof").waitFor();
      assert.equal(await card.evaluate((element) => element.shadowRoot.activeElement?.id), "roof");
      results.push({ theme, continuity, select, announcement, controls, inspection });
    } finally { await page.close(); }
  }
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  try {
    await page.goto(`${origin}/tests/frontend/demo.html?width=360&multiple=1`);
    await page.waitForFunction(() => [...document.querySelectorAll("pv-forecast-card")].every((card) => card._state?.loading === false));
    assert.equal(await page.evaluate(() => window.demo.calls.length), 3);
    await page.locator("pv-forecast-card").evaluateAll((cards) => cards.forEach((card) => { card.style.display = "none"; }));
    await page.waitForFunction(() => [...document.querySelectorAll("pv-forecast-card")].every((card) => !card._visible));
    const stopped = await page.evaluate(async () => {
      const { connectionCache } = await import("/custom_components/pv_forecast/frontend/pv-forecast-card.js");
      const cache = connectionCache(window.demo.hass);
      return { listeners: [...cache.entries.values()].reduce((count, entry) => count + entry.listeners.size, 0), timer: cache.timer };
    });
    assert.deepEqual(stopped, { listeners: 0, timer: null });
    await page.locator("pv-forecast-card").evaluateAll((cards) => cards.forEach((card) => card.remove()));
    results.push({ multiple: true, readCalls: 3, stopped });
  } finally { await page.close(); }
  fs.writeFileSync(path.join(output, "accessibility.json"), JSON.stringify(results, null, 2));
  console.log("Bedienung: Cursor, native Auswahl, Details, Scrollen, 320 px/200 % Text, Fokus bei Dachlöschung und geteilte Abrufe bestanden");
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  fs.mkdirSync(path.join(root, "docs/images"), { recursive: true });
  const server = serverForRepository();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
    const origin = `http://127.0.0.1:${server.address().port}`;
    if (Number(prefix.split("-").at(-1)) >= 114) await checkDataStates(browser, origin);
    if (Number(prefix.split("-").at(-1)) >= 116) {
      await checkDelayedRoofFocus(browser, origin);
      await checkAccessibility(browser, origin);
    }
    const matrix = [360, 768, 1440].flatMap((viewport) => ["light", "dark", "custom"].map((theme) => ({
      name: `${viewport}-${theme}`, viewport, cardWidth: viewport, theme,
      representative: (viewport === 360 && theme === "light") || (viewport === 768 && theme === "custom") || (viewport === 1440 && theme === "dark"),
    })));
    matrix.push(
      { name: "desktop-card360", viewport: 1440, cardWidth: 360, theme: "light" },
      { name: "360-long-large", viewport: 360, cardWidth: 360, theme: "light", stress: true },
      { name: "1440-long-large", viewport: 1440, cardWidth: 1440, theme: "dark", stress: true },
      { name: "360-panel", viewport: 360, cardWidth: 360, theme: "light", panel: true },
      { name: "360-panel-kpi-boundaries", viewport: 360, cardWidth: 360, theme: "light", panel: true, stress: true, largeToday: 123.45, largeTomorrow: 123456 },
    );
    if (Number(prefix.split("-").at(-1)) >= 112) matrix.push(
      { name: "360-tomorrow", viewport: 360, cardWidth: 360, theme: "light", day: "tomorrow" },
      { name: "360-roof", viewport: 360, cardWidth: 360, theme: "dark", scenario: "roof" },
    );
    if (Number(prefix.split("-").at(-1)) >= 113) matrix.push(
      { name: "360-fold", viewport: 360, cardWidth: 360, theme: "light", scenario: "fold" },
      { name: "360-kolkata", viewport: 360, cardWidth: 360, theme: "light", scenario: "kolkata" },
      { name: "360-gaps", viewport: 360, cardWidth: 360, theme: "light", scenario: "gaps" },
      { name: "360-touch", viewport: 360, cardWidth: 360, theme: "light", touch: true },
    );
    const selectedCases = matrix.filter((test) => !option("case") || test.name === option("case"));
    assert.ok(selectedCases.length, "Unbekannter Browserfall");
    const results = [];
    for (const test of selectedCases) {
      const result = await runCase(browser, origin, test);
      results.push(result);
      const failures = result.errors.length + result.closed.findings.length + result.expanded.findings.length + (result.chart?.inspection.findings.length || 0);
      console.log(`${test.name}: ${failures ? `${failures} Befunde` : "bestanden"}`);
    }
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify({ limitation: "Synthetische Offline-Demo; keine Prüfung des nativen HA-Frontends oder realer Messdaten.", results }, null, 2));
    const failures = results.flatMap((result) => [...result.errors, ...result.closed.findings, ...result.expanded.findings, ...(result.chart?.inspection.findings || [])].map((finding) => ({ case: result.name, finding })));
    assert.equal(failures.length, 0, JSON.stringify(failures.slice(0, 25), null, 2));
    console.log(`${selectedCases.length} Browserfälle bestanden. Messwerte und alle Bilder: ${output}`);
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
