// Offline-Nachweis der historischen Navigation mit synthetischen Archivständen.
const fs = require('node:fs'), http = require('node:http'), path = require('node:path'), assert = require('node:assert/strict');
const { chromium } = require(process.env.PV_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const server = http.createServer((req, res) => {
  const file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
  if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, data) => { if (err) { res.writeHead(404).end(); return; } res.setHeader('content-type', /\.(mjs|js)$/.test(file) ? 'text/javascript' : 'text/html'); res.end(data); });
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, executablePath: process.env.PV_CHROMIUM_EXECUTABLE});
  try {
    for (const width of [360,768,1440]) for (const theme of ['light','dark']) {
      const page = await browser.newPage({viewport: {width, height:900}, hasTouch: true, colorScheme: theme});
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}/tests/frontend/demo.html?scenario=gaps&width=${width}&theme=${theme}`);
      const card = page.locator('pv-forecast-card');
      await card.locator('#archive-day-toggle').waitFor();
      assert.equal(await page.evaluate(() => demo.calls.filter(c => c.service_data.day_view).length), 0);
      await card.locator('#archive-day-toggle').focus(); await page.keyboard.press('Enter');
      await card.locator('#archive-interval-chart').waitFor();
      await card.locator('#archive-interval-chart').focus(); await page.keyboard.press('Home'); await page.keyboard.press('ArrowRight');
      await card.locator('#archive-interval-detail').waitFor();
      await card.locator('#archive-prev').click();
      await page.waitForFunction(() => document.querySelector('pv-forecast-card')._archiveDay?.data?.date === '2026-09-08');
      await card.locator('#archive-horizon').selectOption('hourly_3h');
      await page.waitForFunction(() => document.querySelector('pv-forecast-card')._archiveDay?.data?.horizon === 'hourly_3h');
      await card.locator('#archive-context').selectOption('synthetic-old');
      await page.waitForFunction(() => document.querySelector('pv-forecast-card')._archiveDay?.data?.configuration_id === 'synthetic-old');
      await card.locator('#archive-interval-chart').tap({position: {x:width > 400 ? 280 : 180,y:100}});
      await card.locator('#archive-interval-detail').waitFor();
      const overflow = await card.evaluate(card => [...card.shadowRoot.querySelectorAll('#archive-day *')].filter(e => e.checkVisibility() && !e.closest('.table-scroll') && !(e instanceof SVGElement)).filter(e => e.getBoundingClientRect().right > card.getBoundingClientRect().right+1).map(e=>e.id||e.tagName));
      assert.deepEqual(overflow, []); assert.deepEqual(errors, []);
      await card.locator('#archive-day').evaluate(e => e.scrollIntoView({block:'start'}));
      await page.screenshot({path:`docs/images/ui-132-${width}-${theme}.png`});
      await page.close(); console.log(`${width} ${theme}: Datum, Horizont, Kontext, Tastatur/Touch und Breite bestanden.`);
    }
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode=1; });
