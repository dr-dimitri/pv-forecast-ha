// Echter HA-Dialog im lokalen Fixture-Server; Zugangsdaten nur über stdin.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PV_PLAYWRIGHT_MODULE || "playwright");
(async () => {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const { origin, token, hasDevice } = JSON.parse(input);
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PV_CHROMIUM_EXECUTABLE });
  const page = await browser.newPage({ viewport: { width: 360, height: 900 } });

  try {
    await page.addInitScript(({ origin, token }) => {
      localStorage.setItem("hassTokens", JSON.stringify({ hassUrl: origin, access_token: token, token_type: "Bearer", expires: Date.now() + 3600000, expires_in: 3600, refresh_token: "fixture", clientId: origin }));
      localStorage.setItem("selectedLanguage", '"de"');
    }, { origin, token });
    await page.goto(`${origin}/config/integrations/integration/pv_forecast`);
    await page.getByRole("button", { name: "Konfigurieren", exact: true }).click();
    await page.getByText("PV-Anlage konfigurieren", { exact: true }).waitFor();
    const dialog = page.locator("body");
    const capture = async (name) => {
      await page.waitForTimeout(350); // Native Öffnungsanimation vollständig abwarten.
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.join("docs/images", `ui-115-360-${name}.png`), fullPage: true });
    };
    await capture("menue");
    await dialog.getByText("Anlage", { exact: true }).click();
    await dialog.getByText("Dachfläche hinzufügen", { exact: true }).waitFor();
    await dialog.getByText("Zurück zur Übersicht", { exact: true }).click();
    await dialog.getByText("PV-Erzeugung", { exact: true }).click();
    await dialog.getByText("PV-Erzeugung hinzufügen", { exact: true }).click();
    await dialog.getByText(hasDevice ? /Bereits eingerichtete, unterstützte/ : /Kein unterstütztes Gerät gefunden/).waitFor();
    await capture(hasDevice ? "geraet" : "kein-geraet");
    await page.getByRole("button", { name: "OK", exact: true }).click();
    if (hasDevice) {
      await page.getByText("Keine Batterie", { exact: false }).first().waitFor();
      assert.match(await dialog.ariaSnapshot(), /KSEM Garage und Werkstatt am Mehrgenerationenhaus mit langem Namen/);
      assert.equal(await page.getByRole("checkbox", { checked: true }).count(), 0);
      await capture("bestaetigung");
    } else {
      await page.getByText("Messquelle auswählen", { exact: true }).waitFor();
      await capture("manuell");
    }
    await page.getByRole("button", { name: "Schließen", exact: true }).click();
    console.log(`Native Dialoge 360 px: ${hasDevice ? "KSEM mit langem Namen, unvorbelegte Bestätigung" : "fehlendes Gerät und manuelle Auswahl"}; Menürücksprung und Abbruch bestanden.`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
