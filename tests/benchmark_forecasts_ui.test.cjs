const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright-core');

const docs = path.resolve(__dirname, '../docs');
const server = http.createServer((req, res) => {
  const file = path.resolve(docs, '.' + new URL(req.url, 'http://localhost').pathname);
  if (!file.startsWith(docs + path.sep)) return res.writeHead(403).end();
  fs.readFile(file, (err, body) => {
    if (err) return res.writeHead(404).end();
    const types = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css', '.png': 'image/png' };
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' }).end(body);
  });
});
const codes = ['KOSPI200', 'KOSDAQ150'];
const forecastWords = /예측|적중률|검증|상승 확률|하락 확률|데이터 연결|모델/;
const baseModel = {
  status: 'ready', model_version: 'version-1', p_up: .61, p_down: .39,
  expected_index: 755.2, interval: { low: 730, high: 780, level: .8 },
  evaluation: { n: 252, hit_rate: .56, retrospective_n: 252, terminal_n: 63 },
};
const baseForward = { model_version: 'version-1', n: 12, hit_rate: .5, class_n: 8, class_hit_rate: .375 };
const payload = {
  schema_version: 1, issued_at: '2026-11-28T08:12:00+09:00', cutoff: '2026-11-28T08:00:00+09:00',
  asof_date: '2026-11-27', target_date: '2026-11-30', issue_kind: 'preliminary', c_trigger: { status: 'triggered' },
  markets: Object.fromEntries(codes.map((code, index) => [code, {
    status: 'ready', close: index ? 1200 : 760, source: 'KRX', asof_date: '2026-11-27', target_date: '2026-11-30',
    chart: { source: 'KRX', raw_display_permitted: true, asof_date: '2026-11-27', points: [
      { date: '2024-11-27', close: 300 }, { date: '2026-01-02', close: 650 },
      { date: '2026-11-26', close: 750 }, { date: '2026-11-27', close: 760 },
      { date: '2026-11-30', close: 999 },
    ] },
    models: { A: { ...baseModel }, B: { ...baseModel }, C: { ...baseModel, p_up: .15, p_normal: .6, p_down: .25 } },
    forward_validation: { A: { ...baseForward }, B: { ...baseForward }, C: { ...baseForward } },
  }])),
};

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const url = `http://127.0.0.1:${server.address().port}/reliable-index-forecast.html#benchmark-indices`;
  async function open(data, width = 1280, javaScriptEnabled = true) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, javaScriptEnabled });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    if (data !== undefined) await page.route('**/data/benchmark_forecasts.json', route => data === null
      ? route.fulfill({ status: 404, body: 'not found' }) : route.fulfill({ json: data }));
    await page.goto(url, { waitUntil: 'networkidle' });
    return { page, close: () => context.close() };
  }
  async function assertStatic(page) {
    const section = page.locator('#benchmark-indices');
    assert.equal(await section.locator('#benchmark-forecast-cards > article').count(), 2);
    assert.equal(await section.locator('#benchmark-static-fallback, #benchmark-forecast-status').count(), 0);
    assert.doesNotMatch(await section.innerText(), forecastWords, 'no forecast-only copy with no eligible model');
    for (const [code, name, close] of [['KOSPI200', /코스피\s*200/, /758\.51/], ['KOSDAQ150', /코스닥\s*150/, /1,198\.25/]]) {
      const card = section.locator(`[data-benchmark="${code}"]`);
      assert.equal(await card.count(), 1);
      assert.match(await card.innerText(), name);
      assert.match(await card.innerText(), close);
      assert.match(await card.innerText(), /2026-11-27/);
      const image = card.locator('img');
      assert.equal(await image.count(), 1);
      await image.evaluate(el => el.decode());
      assert.ok(await image.evaluate(el => el.naturalWidth > 0));
      const link = card.getByRole('link', { name: /최신 차트/ });
      assert.equal(await link.count(), 1);
      assert.match(await link.getAttribute('href'), code === 'KOSPI200' ? /KOSPI200/ : /KOSDAQ150/);
      await link.focus();
      assert.equal(await link.evaluate(el => document.activeElement === el), true);
    }
  }
  try {
    for (const width of [360, 1440]) {
      for (const data of [undefined, null, { schema_version: 0, markets: {} }]) {
        const view = await open(data, width);
        await assertStatic(view.page);
        assert.equal(await view.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await view.close();
      }
      const noJs = await open(undefined, width, false);
      await assertStatic(noJs.page);
      await noJs.close();
    }

    const mixed = structuredClone(payload);
    mixed.markets.KOSPI200.forward_validation.B.model_version = 'different';
    mixed.markets.KOSPI200.forward_validation.C.class_n = 0;
    mixed.markets.KOSDAQ150.forward_validation.B.n = 1;
    mixed.markets.KOSDAQ150.forward_validation.B.hit_rate = 0;
    mixed.markets.KOSDAQ150.forward_validation.A.n = 0;
    mixed.markets.KOSDAQ150.forward_validation.C.class_n = 1;
    mixed.markets.KOSDAQ150.forward_validation.C.class_hit_rate = 0;
    const view = await open(mixed);
    const kospi = view.page.locator('[data-benchmark="KOSPI200"]');
    const kosdaq = view.page.locator('[data-benchmark="KOSDAQ150"]');
    assert.equal(await kospi.locator('[data-model="A"]').count(), 1);
    assert.equal(await kospi.locator('[data-model="B"], [data-model="C"]').count(), 0);
    assert.equal(await kosdaq.locator('[data-model="A"]').count(), 0);
    assert.equal(await kosdaq.locator('[data-model="B"]').count(), 1, 'zero percent and one issued sample are valid');
    assert.match(await kosdaq.locator('[data-model="B"]').innerText(), /0(?:\.0)?%/);
    assert.equal(await kosdaq.locator('[data-model="C"]').count(), 1);
    assert.match(await kosdaq.locator('[data-model="C"]').innerText(), /급락|중립|급등/);
    assert.match(await kosdaq.locator('[data-model="C"]').innerText(), /0(?:\.0)?%/, 'zero class accuracy with one issued sample is valid');
    assert.equal(await kospi.getByRole('img', { name: /1년 종가/ }).count(), 1, 'permitted current chart upgrades card');
    const chartData = kospi.locator('.benchmark-chart-data');
    await chartData.locator('summary').click();
    assert.doesNotMatch(await chartData.innerText(), /2024-11-27|999\.00/);
    await view.close();

    const badCases = [
      ['missing model', p => delete p.markets.KOSPI200.models.A],
      ['not ready', p => { p.markets.KOSPI200.models.A.status = 'not_ready'; }],
      ['missing probability', p => { p.markets.KOSPI200.models.A.p_up = null; }],
      ['probabilities do not sum to one', p => { p.markets.KOSPI200.models.A.p_up = .9; }],
      ['invalid count', p => { p.markets.KOSPI200.forward_validation.A.n = 1.5; }],
      ['zero count', p => { p.markets.KOSPI200.forward_validation.A.n = 0; }],
      ['missing hit rate', p => { p.markets.KOSPI200.forward_validation.A.hit_rate = null; }],
      ['invalid hit rate', p => { p.markets.KOSPI200.forward_validation.A.hit_rate = 1.01; }],
      ['empty version', p => { p.markets.KOSPI200.models.A.model_version = ''; }],
      ['missing forward version', p => { delete p.markets.KOSPI200.forward_validation.A.model_version; }],
      ['mismatched version', p => { p.markets.KOSPI200.forward_validation.A.model_version = 'other'; }],
      ['historic evaluation is not issued accuracy', p => { delete p.markets.KOSPI200.forward_validation.A; }],
    ];
    for (const [name, mutate] of badCases) {
      const data = structuredClone(payload);
      mutate(data);
      const page = await open(data);
      assert.equal(await page.page.locator('[data-benchmark="KOSPI200"] [data-model="A"]').count(), 0, name);
      assert.equal(await page.page.locator('[data-benchmark="KOSPI200"] [data-model="B"]').count(), 1, 'other eligible model remains visible');
      await page.close();
    }
    for (const change of [
      p => { p.c_trigger.status = 'not_triggered'; },
      p => { p.c_trigger.status = 'unknown'; },
      p => { p.markets.KOSPI200.forward_validation.C.class_n = 0; },
      p => { p.markets.KOSPI200.forward_validation.C.class_hit_rate = null; },
      p => { p.markets.KOSPI200.forward_validation.C.class_n = 1.5; },
      p => { p.markets.KOSPI200.forward_validation.C.class_hit_rate = -0.1; },
      p => { p.markets.KOSPI200.forward_validation.C.model_version = 'other'; },
      p => { p.markets.KOSPI200.models.C.p_normal = .8; },
    ]) {
      const data = structuredClone(payload); change(data);
      const page = await open(data);
      assert.equal(await page.page.locator('[data-benchmark="KOSPI200"] [data-model="C"]').count(), 0, 'C requires trigger, valid probabilities and matching issued class evidence');
      await page.close();
    }
    for (const change of [
      p => { p.markets.KOSPI200.chart.raw_display_permitted = false; },
      p => { p.markets.KOSPI200.chart.publish_allowed = false; },
      p => { p.markets.KOSPI200.chart.asof_date = '2026-11-26'; },
      p => { p.markets.KOSPI200.chart.points.pop(); p.markets.KOSPI200.chart.points.pop(); },
    ]) {
      const data = structuredClone(payload); change(data);
      const page = await open(data);
      const card = page.page.locator('[data-benchmark="KOSPI200"]');
      assert.equal(await card.locator('img').count(), 1, 'unpermitted, stale or incomplete chart preserves snapshot');
      await page.close();
    }
    console.log('Benchmark index UI: static and accessible fallback, forecast eligibility and per-market chart upgrades passed.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
