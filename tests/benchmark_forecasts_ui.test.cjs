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
const model = {
  status: 'ready', p_up: .61, p_down: .39, expected_index: 755.2,
  interval: { low: 730, high: 780, level: .8 },
  evaluation: { n: 252, hit_rate: .56, baseline_hit_rate: .52, retrospective_n: 252, terminal_n: 63, live_n: 0,
    windows: { last_30: { n: 30, hit_rate: .6 } }, direction_baseline: 'majority',
    hit_rate_gain_ci95: { mean: .04, low: -.01, high: .09 }, mae_return: .008, coverage80: .81,
    up_recall: .67, down_recall: .44,
  },
};
const payload = {
  schema_version: 1, issued_at: '2026-11-28T08:12:00+09:00', cutoff: '2026-11-28T08:00:00+09:00',
  asof_date: '2026-11-27', target_date: '2026-11-30', issue_kind: 'preliminary', c_trigger: { status: 'not_triggered' },
  markets: {
    KOSPI200: {
      status: 'ready', close: 758.51, source: 'KRX', asof_date: '2026-11-27', target_date: '2026-11-30',
      cutoff: '2026-11-28T08:00:00+09:00', history: { start: '2020-01-02', end: '2026-11-27', count: 1700 },
      chart: { source: 'KRX', asof_date: '2026-11-27', points: [
        { date: '2024-11-27', close: 300 }, { date: '2026-01-02', close: 650 },
        { date: '2026-11-26', close: 750 }, { date: '2026-11-27', close: 758.51 },
        { date: '2026-11-30', close: 999 },
      ] },
      models: { A: model, B: { ...model, p_up: .48, p_down: .52 }, C: { ...model, p_up: .15, p_down: .25, p_normal: .6 } },
    },
    KOSDAQ150: { status: 'not_ready', reason: '정확한 지수 종가 미확인', close: null, source: null, chart: null, models: {} },
  },
};

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const url = `http://127.0.0.1:${server.address().port}/reliable-index-forecast.html#benchmark-indices`;
  async function open(data, width = 1280) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    await page.route('**/data/benchmark_forecasts.json', route => data === null
      ? route.fulfill({ status: 404, body: 'not found' }) : route.fulfill({ json: data }));
    await page.goto(url, { waitUntil: 'networkidle' });
    return page;
  }
  try {
    for (const width of [375, 1280]) {
      const page = await open(payload, width);
      const kospi = page.locator('[data-benchmark="KOSPI200"]');
      assert.equal(await kospi.count(), 1, 'exact KOSPI200 forecast is mounted');
      assert.match(await kospi.innerText(), /758\.51/);
      assert.match(await kospi.innerText(), /2026-11-30/);
      assert.match(await kospi.innerText(), /08:00.*KST/);
      assert.match(await page.locator('#benchmark-forecast-status').innerText(), /사전 예측/);
      assert.match(await kospi.locator('[data-model="A"]').innerText(), /방향.*상승/);
      assert.match(await kospi.locator('[data-model="A"]').innerText(), /예상 종가.*755\.20/);
      const evidence = kospi.locator('[data-model="A"] details');
      await evidence.locator('summary').click();
      assert.match(await evidence.innerText(), /과거 재구성.*252/);
      assert.match(await evidence.innerText(), /최종 검증.*63/);
      assert.match(await evidence.innerText(), /실제 발행.*0/);
      assert.match(await evidence.innerText(), /최근 30거래일.*60\.0%.*최종 검증.*56\.0%/);
      assert.match(await evidence.innerText(), /다수 방향.*52\.0%/);
      assert.match(await evidence.innerText(), /95%.*−1\.0.*\+9\.0.*%p/);
      assert.match(await evidence.innerText(), /종가 수익률 MAE.*0\.80.*%p/);
      assert.match(await evidence.innerText(), /80% 예측 구간.*81\.0%/);
      assert.match(await evidence.innerText(), /상승일 재현율.*67\.0%.*하락일 재현율.*44\.0%/);
      assert.equal(await kospi.locator('[data-model="C"]').count(), 0, 'untriggered C stays absent even if payload has numbers');
      assert.equal(await kospi.getByRole('img', { name: /1년 종가/ }).count(), 1);
      const chartData = kospi.locator('.benchmark-chart-data');
      await chartData.locator('summary').click();
      assert.match(await chartData.innerText(), /758\.51/);
      assert.doesNotMatch(await chartData.innerText(), /2024-11-27|999\.00/, 'chart excludes old and future observations');
      const missing = page.locator('[data-benchmark="KOSDAQ150"]');
      assert.match(await missing.innerText(), /정확한 지수 종가 미확인/);
      assert.doesNotMatch(await missing.innerText(), /0\.00|0\.0%/);
      assert.equal(await missing.getByRole('link', { name: /정적 참고 차트/ }).count(), 1);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `no overflow at ${width}`);
      if (process.env.BENCHMARK_SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.BENCHMARK_SCREENSHOT_DIR, { recursive: true });
        await page.locator('#benchmark-indices').screenshot({ path: path.join(process.env.BENCHMARK_SCREENSHOT_DIR, `benchmark-${width}.png`) });
      }
      await page.close();
    }
    const c = structuredClone(payload);
    c.c_trigger.status = 'triggered';
    c.issue_kind = 'final';
    c.markets.KOSPI200.models.C.evaluation = { n: 48, class_accuracy: .625, metric_scope: 'prior_broad_triggered_sessions',
      class_counts: { down_extreme: 10, normal: 28, up_extreme: 10 }, class_recall: { down_extreme: .4, normal: .8, up_extreme: .3 },
      windows: { last_30: { n: 4, class_accuracy: .75 } } };
    const triggered = await open(c);
    const cPanel = triggered.locator('[data-benchmark="KOSPI200"] [data-model="C"]');
    assert.match(await cPanel.innerText(), /급락.*25\.0%/);
    assert.match(await cPanel.innerText(), /중립.*60\.0%/);
    assert.match(await cPanel.innerText(), /급등.*15\.0%/);
    assert.doesNotMatch(await cPanel.innerText(), /방향/);
    await cPanel.locator('summary').click();
    assert.match(await cPanel.innerText(), /조건 충족.*48건/);
    assert.match(await cPanel.innerText(), /3분류 적중률.*62\.5%/);
    assert.match(await cPanel.innerText(), /최근 30거래일.*75\.0%.*4건/);
    assert.match(await cPanel.innerText(), /급락 재현율.*40\.0%.*중립 재현율.*80\.0%.*급등 재현율.*30\.0%/);
    assert.doesNotMatch(await cPanel.innerText(), /MAE|기준선|미확인/);
    assert.match(await triggered.locator('#benchmark-forecast-status').innerText(), /최종 예측/);
    await triggered.close();

    const derived = structuredClone(payload);
    derived.markets.KOSPI200.close = null;
    derived.markets.KOSPI200.chart = null;
    derived.markets.KOSPI200.forward_validation = { A: { n: 12, hit_rate: .5 } };
    derived.markets.KOSPI200.models.A.evaluation.status = 'unconfirmed_research';
    derived.markets.KOSPI200.models.A.cutoff = '2026-11-28T08:01:00+09:00';
    derived.markets.KOSPI200.models.A.issued_at = '2026-11-28T08:02:00+09:00';
    derived.markets.KOSDAQ150.reason = 'credential_missing';
    derived.c_trigger.status = 'unknown';
    const restricted = await open(derived);
    const derivedCard = restricted.locator('[data-benchmark="KOSPI200"]');
    assert.match(await derivedCard.locator('.benchmark-close').innerText(), /미확인/);
    assert.match(await derivedCard.locator('[data-model="A"]').innerText(), /61\.0%/);
    assert.match(await derivedCard.locator('[data-model="A"]').innerText(), /검증 기준 미충족/);
    assert.match(await derivedCard.locator('[data-model="A"]').innerText(), /08:01:00.*KST/);
    await derivedCard.locator('[data-model="A"] summary').click();
    assert.match(await derivedCard.locator('[data-model="A"] details').innerText(), /실제 발행 12건/);
    assert.match(await restricted.locator('[data-benchmark="KOSDAQ150"]').innerText(), /데이터 연결.*설정/);
    assert.equal(await restricted.locator('[data-model="C"]').count(), 0, 'unknown trigger never reveals stale C');
    const fallbackLink = derivedCard.getByRole('link', { name: /정적 참고 차트/ });
    await restricted.locator('#benchmark-static-fallback summary').click();
    await fallbackLink.focus();
    await fallbackLink.press('Enter');
    assert.equal(await restricted.locator('#benchmark-static-fallback').getAttribute('open'), '');
    assert.match(restricted.url(), /#benchmark-kospi200$/);
    await restricted.close();

    const corrupt = structuredClone(payload);
    corrupt.markets.KOSPI200.models.A.p_up = null;
    corrupt.markets.KOSPI200.models.B.status = 'not_ready';
    corrupt.markets.KOSPI200.reason = '<img src=x onerror=alert(1)>';
    corrupt.markets.KOSPI200.chart = { source: 'KRX', asof_date: '2026-11-27', points: [{ date: '2026-11-27', close: null }] };
    const invalid = await open(corrupt);
    assert.doesNotMatch(await invalid.locator('[data-model="A"]').first().innerText(), /0\.0%|상승 우세/);
    assert.doesNotMatch(await invalid.locator('[data-model="B"]').first().innerText(), /48\.0%|755\.20/);
    assert.equal(await invalid.locator('#benchmark-forecast-cards img').count(), 0, 'source text cannot inject markup');
    await invalid.close();

    for (const old of [null, { schema_version: 0, markets: {} }]) {
      const unavailable = await open(old);
      assert.match(await unavailable.locator('#benchmark-forecast-status').innerText(), /확인하지 못했습니다/);
      assert.equal(await unavailable.locator('#benchmark-static-fallback').getAttribute('open'), '');
      const image = unavailable.locator('#benchmark-static-fallback img').first();
      await image.scrollIntoViewIfNeeded();
      await image.evaluate(el => el.decode());
      assert.ok(await image.evaluate(el => el.naturalWidth > 0));
      assert.match(await unavailable.locator('#benchmark-static-fallback').innerText(), /정적 참고.*2026-11-27/);
      await unavailable.close();
    }
    console.log('Benchmark forecasts UI: exact indices, partial/missing data, chart window, evidence, C gate, mobile/desktop passed.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
