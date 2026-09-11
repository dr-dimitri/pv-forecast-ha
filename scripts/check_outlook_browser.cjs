// Offline-Prüfung der Tagesaussicht mit synthetischen Daten; keine echte HA-Abnahme.
// PV_PLAYWRIGHT_MODULE und PV_CHROMIUM_EXECUTABLE wählen vorhandene Browserwerkzeuge.
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PV_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const output = process.env.PV_OUTLOOK_OUTPUT || "/tmp/pv-outlook-browser";
const server = http.createServer((request, response) => {
  const file = path.resolve(root, `.${new URL(request.url, "http://localhost").pathname}`);
  if (!file.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
  fs.readFile(file, (error, data) => {
    if (error) { response.writeHead(404).end(); return; }
    response.setHeader("content-type", /\.(js|mjs)$/.test(file) ? "text/javascript" : "text/html");
    response.end(data);
  });
});
(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, executablePath: process.env.PV_CHROMIUM_EXECUTABLE });
    fs.mkdirSync(output, { recursive: true });
    for (const width of [360, 1440]) for (const theme of ["light", "dark"]) {
      for (const scenario of ["partial-outlook", "unresolved-outlook", "derived-outlook", "no-source", "acl", "restored", "empty"]) {
        const page = await browser.newPage({ viewport: { width, height: 1000 }, colorScheme: theme });
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/tests/frontend/demo.html?width=${width}&theme=${theme}&scenario=${scenario}`, { waitUntil: "networkidle" });
        const card = page.locator("pv-forecast-card");
        const outlook = card.locator("#outlook");
        await outlook.waitFor();
        await card.locator("#outlook-toggle").focus();
        await page.keyboard.press("Enter");
        assert.equal(await outlook.getAttribute("open"), "");
        const text = await outlook.innerText();
        assert.doesNotMatch(text, /Noch offen|noch nicht verfügbar/);
        if (scenario === "empty") assert.match(text, /Keine Prognosedaten/);
        else assert.match(text, /Heute voraussichtlich insgesamt/);
        if (scenario === "partial-outlook") {
          assert.match(text, /21,16 kWh/);
          assert.match(text, /Bisheriger Tag geschätzt/);
        }
        if (scenario === "unresolved-outlook") {
          assert.match(text, /Zuordnung mindestens einer Messquelle/);
          assert.match(text, /Integrationsoptionen/);
          assert.match(text, /erneut bestätigen/);
          assert.doesNotMatch(text, /Verwertbare Messwerte werden automatisch/);
        }
        if (scenario === "derived-outlook") {
          assert.match(text, /Berücksichtigte Messung/);
          assert.doesNotMatch(text, /Qualitätsmarkierungen|teilweise Ersatzwerte|aus Leistung berechnet/);
        }
        if (scenario === "restored") assert.match(text, /Wetterabruf ist nicht aktuell/);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        const overflow = await outlook.evaluate((element) => [...element.querySelectorAll("p, dd, dt")].some((item) => item.scrollWidth > item.clientWidth + 1));
        assert.equal(overflow, false);
        assert.deepEqual(errors, []);
        await outlook.screenshot({ path: path.join(output, `${scenario}-${width}-${theme}.png`) });
        await page.close();
      }
    }
    console.log("28 Browserfälle bestanden: Messlücke, ungeklärte Quellenidentität, abgeleitete Messung, fehlende Quelle, Quellenrechte, alter Wetterstand und fehlende Prognose; 360/1440 px, Hell/Dunkel und Tastatur.");
  } finally {
    await browser?.close();
    server.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
