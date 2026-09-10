// Offline-Nachweis der Erklärung; nur synthetische Werte und lokale Leseaktionen.
const fs=require('node:fs'), http=require('node:http'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PV_PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const server=http.createServer((req,res)=>{const file=path.resolve(root,'.'+new URL(req.url,'http://localhost').pathname);if(!file.startsWith(root+path.sep)){res.writeHead(403).end();return;}fs.readFile(file,(error,data)=>{if(error){res.writeHead(404).end();return;}res.setHeader('content-type',/\.(js|mjs)$/.test(file)?'text/javascript':'text/html');res.end(data);});});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:process.env.PV_CHROMIUM_EXECUTABLE});
  try {
    for(const theme of ['light','dark']) {
      const page=await browser.newPage({viewport:{width:360,height:900},colorScheme:theme,hasTouch:true});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}/tests/frontend/demo.html?width=360&theme=${theme}`);
      const card=page.locator('pv-forecast-card');
      await card.locator('#explanation-toggle').waitFor();
      assert.equal(await page.evaluate(()=>demo.calls.filter(c=>c.service_data.include_explanation).length),0);
      await card.locator('#explanation-toggle').focus();await page.keyboard.press('Enter');
      await card.getByText('Angewendeter Anlagenfaktor: 1,2',{exact:true}).waitFor();
      await card.locator('#raw-toggle').focus();await page.keyboard.press('Space');
      assert.equal(await card.locator('.raw-line').count(),0);
      await card.locator('#raw-toggle:checked').waitFor();
      await card.locator('#interval-chart').focus();await page.keyboard.press('Home');await page.keyboard.press('ArrowRight');
      await card.locator('#interval-detail').getByText('Grundmodell ohne Selbstkalibrierung',{exact:true}).waitFor();
      await card.locator('#roof').selectOption('south');
      await page.waitForFunction(()=>document.querySelector('pv-forecast-card')._state?.forecast?.data?.roof_id==='south');
      assert.equal(await card.locator('.raw-line').count(),0);
      await card.locator('#interval-chart').tap();
      await card.locator('#interval-detail').waitFor();
      assert.equal(await card.locator('#interval-detail').getByText('Grundmodell ohne Selbstkalibrierung',{exact:true}).count(),0);
      await card.locator('#interval-chart').focus();await page.keyboard.press('Home');await page.keyboard.press('ArrowRight');
      assert.equal(await card.evaluate(c=>c.shadowRoot.activeElement?.id),'interval-chart');
      await card.locator('#roof').selectOption('');
      assert.equal(await card.locator('.raw-line').count(),0);
      await card.locator('#raw-toggle:checked').waitFor();
      assert.equal(await card.locator('#raw-toggle').isChecked(),true);
      await card.locator('#interval-chart').focus();await page.keyboard.press('Home');
      await card.locator('#interval-detail').getByText('Grundmodell ohne Selbstkalibrierung',{exact:true}).waitFor();
      await card.locator('[data-day="tomorrow"]').click();
      await page.waitForFunction(()=>{const c=document.querySelector('pv-forecast-card');return c._state?.forecast.envelope?.explanation?.start===c._state?.forecast.data.start&&c._state?.forecast.data.day==='tomorrow';});
      await card.locator('#explanation').evaluate(e=>e.scrollIntoView({block:'start'}));
      assert.equal(await card.evaluate(card=>[...card.shadowRoot.querySelectorAll('#explanation *')].some(e=>e.checkVisibility()&&e.getBoundingClientRect().right>card.getBoundingClientRect().right+1)),false);
      await page.screenshot({path:path.join(process.env.PV_SCREENSHOT_DIR||'docs/images',`ui-133-360-${theme}.png`)});
      await card.locator('#raw-toggle').uncheck();await card.locator('#explanation-toggle').click();
      const count=await page.evaluate(()=>demo.calls.filter(c=>c.service_data.include_explanation).length);
      await page.evaluate(()=>{document.querySelector('pv-forecast-card')._bind();});
      assert.equal(await page.evaluate(()=>demo.calls.filter(c=>c.service_data.include_explanation).length),count);
      assert.deepEqual(errors,[]);await page.close();console.log(`360 ${theme}: Erklärung, Grundmodell, Dach-/Tageswechsel, Touch und Tastatur bestanden.`);
    }
  } finally {await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
