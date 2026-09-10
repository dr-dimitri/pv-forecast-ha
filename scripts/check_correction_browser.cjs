// Lokale Kartenprüfung mit synthetischer Tageskorrektur; keine echte Anlage.
const fs = require("node:fs"), http = require("node:http"), path = require("node:path"), assert = require("node:assert/strict");
const { chromium } = require(process.env.PV_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const server = http.createServer((req, res) => {
  const file = path.resolve(root, "." + new URL(req.url, "http://localhost").pathname);
  if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, data) => { if (err) { res.writeHead(404).end(); return; } res.setHeader("content-type", /\.(mjs|js)$/.test(file) ? "text/javascript" : "text/html"); res.end(data); });
});
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PV_CHROMIUM_EXECUTABLE });
  try {
    for (const theme of ["light", "dark"]) {
      const page = await browser.newPage({ viewport: { width: 360, height: 900 }, colorScheme: theme });
      const errors = []; page.on("pageerror", error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}/tests/frontend/demo.html?scenario=sunny&width=360&theme=${theme}`);
      const card = page.locator("pv-forecast-card");
      await card.locator("#archive-day-toggle").waitFor();
      await page.evaluate(() => {
        const original = demo.hass.callWS;
        demo.hass.callWS = async message => {
          const response = await original(message);
          if (response.response?.day_view) response.response.day_view.daily_measurement = {
            ...response.response.day_view.daily_measurement,
            energy_kwh: 18.125, measured_energy_kwh: 16,
            manual_correction: true, assessed_at: "2026-09-10T10:00:00Z",
          };
          return response;
        };
      });
      await card.locator("#archive-day-toggle").focus(); await page.keyboard.press("Enter");
      const title = card.getByText("Bestätigter Tagesertrag · korrigiert", { exact: true });
      await title.waitFor();
      await card.getByText("Automatisch erfasster Tageswert", { exact: true }).waitFor();
      await title.scrollIntoViewIfNeeded();
      const overflow = await card.evaluate(element => [...element.shadowRoot.querySelectorAll("#archive-day *")].filter(e => e.checkVisibility() && !e.closest(".table-scroll") && !(e instanceof SVGElement)).filter(e => e.getBoundingClientRect().right > element.getBoundingClientRect().right + 1).map(e => e.id || e.tagName));
      assert.deepEqual(overflow, []); assert.deepEqual(errors, []);
      await page.screenshot({ path: `docs/images/tageskorrektur-360-${theme}.png` });
      await page.close(); console.log(`Tageskorrektur 360 px ${theme}: Original, Bestätigung, Tastatur und Breite bestanden.`);
    }
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
