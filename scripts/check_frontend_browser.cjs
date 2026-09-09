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
    if ((!isSvg || element.matches("text, tspan")) && bounds.width > 0 && (bounds.left < cardBounds.left - 1 || bounds.right > cardBounds.right + 1)) findings.push({ kind: "overflow", element: label(element), left: bounds.left, right: bounds.right, cardRight: cardBounds.right });
    const directText = [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
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
  for (const element of shadow.querySelectorAll(".forecast-line, .history-line, .actual-bar")) {
    if (!visible(element)) continue;
    const back = background(element);
    const contrast = ratio(blend(color(getComputedStyle(element).stroke), back), back);
    minimumLineContrast = Math.min(minimumLineContrast, contrast);
    if (contrast < 2.99) findings.push({ kind: "chart-contrast", element: label(element), contrast });
  }
  if (document.documentElement.scrollWidth > innerWidth + 1) findings.push({ kind: "page-overflow", width: document.documentElement.scrollWidth, viewport: innerWidth });
  return { findings, textCount, minimumText, minimumChartText, minimumContrast, minimumControlContrast, minimumLineContrast, cardWidth: cardBounds.width };
}

async function runCase(browser, origin, test) {
  const page = await browser.newPage({ viewport: { width: test.viewport, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // Nur synthetische Fixture-Werte werden für den Belastungsfall verändert.
  if (test.stress) await page.route("**/fixtures.mjs", (route) => {
    const source = fs.readFileSync(path.join(root, "tests/frontend/fixtures.mjs"), "utf8")
      .replaceAll('"Sonnenhaus"', '"Photovoltaikanlage am Mehrgenerationenhaus mit Werkstatt und außergewöhnlich langem Anlagennamen"')
      .replaceAll('"Süddach"', '"Südostdachfläche des Mehrgenerationenhauses mit Werkstattanbau"')
      .replaceAll("23.14", "12345.67").replaceAll("26.9", "23456.78").replaceAll("10.76", "9876.54")
      .replace(/const HOURS = (\[[^\n]+\]);/, "const HOURS = $1.map((value) => value * 1000);");
    return route.fulfill({ contentType: "text/javascript", body: source });
  });
  try {
    const params = new URLSearchParams({ theme: test.theme === "dark" ? "dark" : "light", width: String(test.cardWidth), scenario: test.scenario || "sunny" });
    if (test.panel) params.set("panel", "1");
    await page.goto(`${origin}/tests/frontend/demo.html?${params}`, { waitUntil: "networkidle" });
    if (test.theme === "custom") await page.evaluate((theme) => {
      for (const [name, value] of Object.entries(theme)) document.documentElement.style.setProperty(name, value);
    }, customTheme);
    const card = page.locator("pv-forecast-card").first();
    await card.locator(".kpis").waitFor();
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const closed = await card.evaluate(inspectCard);
    const screenshot = `${prefix}-${test.name}.png`;
    await page.screenshot({ path: path.join(output, screenshot), fullPage: true });
    if (test.representative) fs.copyFileSync(path.join(output, screenshot), path.join(root, "docs/images", screenshot));
    await card.locator("details").evaluateAll((items) => { for (const item of items) item.open = true; });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const expanded = await card.evaluate(inspectCard);
    return { name: test.name, fixtureOnly: true, errors, closed, expanded, screenshot: path.join(output, screenshot) };
  } finally { await page.close(); }
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
    const matrix = [360, 768, 1440].flatMap((viewport) => ["light", "dark", "custom"].map((theme) => ({
      name: `${viewport}-${theme}`, viewport, cardWidth: viewport, theme,
      representative: (viewport === 360 && theme === "light") || (viewport === 768 && theme === "custom") || (viewport === 1440 && theme === "dark"),
    })));
    matrix.push(
      { name: "desktop-card360", viewport: 1440, cardWidth: 360, theme: "light" },
      { name: "360-long-large", viewport: 360, cardWidth: 360, theme: "light", stress: true },
      { name: "1440-long-large", viewport: 1440, cardWidth: 1440, theme: "dark", stress: true },
      { name: "360-panel", viewport: 360, cardWidth: 360, theme: "light", panel: true },
    );
    const results = [];
    for (const test of matrix) {
      const result = await runCase(browser, origin, test);
      results.push(result);
      const failures = result.errors.length + result.closed.findings.length + result.expanded.findings.length;
      console.log(`${test.name}: ${failures ? `${failures} Befunde` : "bestanden"}`);
    }
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify({ limitation: "Synthetische Offline-Demo; keine Prüfung des nativen HA-Frontends oder realer Messdaten.", results }, null, 2));
    const failures = results.flatMap((result) => [...result.errors, ...result.closed.findings, ...result.expanded.findings].map((finding) => ({ case: result.name, finding })));
    assert.equal(failures.length, 0, JSON.stringify(failures.slice(0, 25), null, 2));
    console.log(`${matrix.length} Browserfälle bestanden. Messwerte und alle Bilder: ${output}`);
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
