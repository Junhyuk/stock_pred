const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright-core');

const docs = path.resolve(__dirname, '../docs');
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const server = http.createServer((req, res) => {
  const filename = path.resolve(docs, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname));
  if (!filename.startsWith(docs + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(filename, (err, content) => {
    if (err) { res.writeHead(404).end(); return; }
    res.writeHead(200, { 'Content-Type': mime[path.extname(filename)] || 'application/octet-stream' });
    res.end(content);
  });
});

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const base = `http://127.0.0.1:${server.address().port}/reliable-index-forecast.html`;
  try {
    const modelC = {
      schema_version: 'model_c_forecast_v1', status: 'ready', production_weight: 0,
      probability_mode: 'three_way', threshold: 0.03, issue_stage: 'premarket',
      asof_date: '2026-11-27', target_date: '2026-11-30',
      target_close_at: '2026-11-30T06:30:00Z', generated_at: '2026-11-28T00:00:00Z',
      markets: [
        { market: 'KOSPI', asof_date: '2026-11-27', target_date: '2026-11-30', close: 2500,
          p_up: 0.1, p_normal: 0.7, p_down: 0.2, expected_index: 2490, expected_return: -0.004,
          interval: { level: 0.8, low: 2350, high: 2600 } },
        { market: 'KOSDAQ', asof_date: '2026-11-27', target_date: '2026-11-30', close: 800,
          p_up: 0.2, p_normal: 0.6, p_down: 0.2 },
      ],
    };
    for (const width of [360, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      page.setDefaultTimeout(5000);
      await page.route('**/data/model_c_forecast.json', route => route.fulfill({ json: {
        ...modelC, trigger_returns: { KOSPI: 0.03, KOSDAQ: -0.029 },
      } }));
      await page.goto(base, { waitUntil: 'networkidle' });
      const kr = page.locator('#market-tab-kr');
      const us = page.locator('#market-tab-us');
      await kr.waitFor();
      assert.equal(await kr.getAttribute('aria-selected'), 'true', `KR default at ${width}`);
      assert.equal(await kr.getAttribute('aria-controls'), 'market-kr');
      assert.equal(await us.getAttribute('aria-controls'), 'market-us');
      assert.equal(await page.locator('#market-kr').isVisible(), true);
      assert.equal(await page.locator('#market-us').isVisible(), false);
      assert.equal(await page.locator('#model-b').isVisible(), true);
      assert.equal(await page.locator('#model-b').evaluate(el => el.compareDocumentPosition(document.querySelector('#model-a')) & Node.DOCUMENT_POSITION_FOLLOWING), 4);
      assert.equal(await page.locator('#model-c').isVisible(), true, 'C is eligible at 3%');
      assert.match(await page.locator('#model-c-target').innerText(), /2026-11-30/);
      assert.doesNotMatch(await page.locator('#model-c-cards').innerText(), /대상 거래일/);
      assert.match(await page.locator('#model-c-cards').innerText(), /예상 종가/);
      assert.match(await page.locator('#model-c-cards').innerText(), /80% 예측 구간/);
      const images = page.locator('#benchmark-indices img');
      assert.ok(await images.count() >= 2, 'both benchmark charts present');
      for (const image of await images.all()) {
        await image.scrollIntoViewIfNeeded();
        await image.evaluate(el => el.decode());
        assert.ok(await image.evaluate(el => el.naturalWidth > 0), 'benchmark image loads');
      }
      assert.match(await page.locator('main').innerText(), /투자[^\n]*책임/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `page overflow at ${width}`);
      await us.click();
      assert.equal(await us.getAttribute('aria-selected'), 'true');
      assert.equal(await page.locator('#market-us').isVisible(), true);
      assert.equal(await page.locator('#market-kr').isVisible(), false);
      assert.equal(await page.locator('#model-b').isVisible(), false);
      assert.equal(await page.locator('#model-a-us').isVisible(), true);
      assert.match(await page.locator('main').innerText(), /투자[^\n]*책임/, 'research disclosure visible in US');
      await us.press('ArrowLeft');
      assert.equal(await kr.getAttribute('aria-selected'), 'true');
      await page.goto(`${base}#model-b`, { waitUntil: 'networkidle' });
      assert.equal(await page.locator('#model-b').isVisible(), true, `model B deep link at ${width}`);
      await page.goto(`${base}#model-c`, { waitUntil: 'networkidle' });
      assert.equal(await page.locator('#model-c').isVisible(), true, `eligible C deep link at ${width}`);
      await page.goto(`${base}#model-a-us`, { waitUntil: 'networkidle' });
      assert.equal(await page.locator('#market-tab-us').getAttribute('aria-selected'), 'true');
      assert.equal(await page.locator('#model-a-us').isVisible(), true);
      await page.goto(`${base}#model-a-us-title`, { waitUntil: 'networkidle' });
      assert.equal(await page.locator('#market-tab-us').getAttribute('aria-selected'), 'true');
      assert.equal(await page.locator('#model-a-us-title').isVisible(), true);
      await page.goto(`${base}#model-compare`, { waitUntil: 'networkidle' });
      assert.equal(await page.locator('#market-tab-kr').getAttribute('aria-selected'), 'true');
      assert.equal(await page.locator('#model-compare').isVisible(), true);
      await page.goto(`${base}#benchmark-indices`, { waitUntil: 'networkidle' });
      assert.equal(await page.locator('#benchmark-indices').isVisible(), true);
      await page.close();
    }
    const gated = await browser.newPage();
    await gated.route('**/data/model_c_forecast.json', route => route.fulfill({ json: {
      ...modelC, trigger_returns: { KOSPI: 0.029, KOSDAQ: -0.029 },
    } }));
    await gated.goto(base, { waitUntil: 'networkidle' });
    assert.equal(await gated.locator('#model-c').isVisible(), false, 'C remains gated without a 3% move');
    assert.equal(await gated.locator('#model-c-nav').isVisible(), false);
    await gated.close();
    console.log('Forecast market UI passed at mobile and desktop widths.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
