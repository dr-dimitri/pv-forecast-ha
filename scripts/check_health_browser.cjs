// Nativer lokaler HA-Betriebscheck; Fixture-Zugangsdaten ausschließlich über stdin.
const fs = require("node:fs");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PV_PLAYWRIGHT_MODULE || "playwright");
(async () => {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const { origin, token, theme } = JSON.parse(input);
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PV_CHROMIUM_EXECUTABLE });
  const page = await browser.newPage({ viewport: { width: 360, height: 900 }, colorScheme: theme });
  try {
    await page.addInitScript(({ origin, token }) => {
      localStorage.setItem("hassTokens", JSON.stringify({ hassUrl: origin, access_token: token, token_type: "Bearer", expires: Date.now() + 3600000, expires_in: 3600, refresh_token: "fixture", clientId: origin }));
      localStorage.setItem("selectedLanguage", '"de"');
    }, { origin, token });
    await page.goto(`${origin}/config/integrations/integration/pv_forecast`);
    await page.getByRole("button", { name: "Konfigurieren", exact: true }).click();
    await page.getByText("Betrieb prüfen", { exact: true }).click();
    await page.getByText("Anbieterpause aktiv", { exact: true }).waitFor();
    const again = page.getByText("Erneut prüfen", { exact: true });
    await again.focus();
    await page.keyboard.press("Enter");
    await page.getByText("Anbieterpause aktiv", { exact: true }).waitFor();
    assert.match(await page.locator("body").ariaSnapshot(), /90 Minuten/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.getByText("Anbieterpause aktiv", { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `docs/images/ui-131-360-${theme}.png`, fullPage: true });
    await page.getByText("Zurück zur Übersicht", { exact: true }).click();
    await page.getByText("PV-Anlage konfigurieren", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Schließen", exact: true }).click();
    console.log(`Betriebscheck 360 px ${theme}: lokale Pause, Tastaturprüfung und Rücksprung bestanden.`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
