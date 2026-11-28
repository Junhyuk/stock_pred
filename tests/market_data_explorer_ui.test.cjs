const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright-core');

const docs = path.resolve(__dirname, '../docs');
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json' };
const server = http.createServer(async (request, response) => {
  const filename = path.resolve(docs, `.${new URL(request.url, 'http://localhost').pathname}`);
  if (!filename.startsWith(docs + path.sep)) return response.writeHead(403).end();
  try {
    const content = await fs.readFile(filename);
    response.writeHead(200, { 'Content-Type': mime[path.extname(filename)] || 'application/octet-stream' }).end(content);
  } catch {
    response.writeHead(404).end();
  }
});

const source = { unit: 'pt', source: 'KRX', source_url: 'https://data.krx.co.kr/' };
const history = {
  schema_version: 'market_chart_history_v1',
  series: {
    kospi: { ...source, label: '코스피', points: [['2026-01-01', 100], ['2026-01-02', 110], ['2026-01-03', 99], ['2026-01-04', 99]] },
    kosdaq: { ...source, label: '코스닥', points: [['2026-01-02', 80], ['2026-01-03', 72], ['2026-01-04', 72]] },
    vix: { ...source, label: 'VIX', points: [['2026-01-04', 99]] },
    kr_inflation: {
      unit: '%', source: '통계청', source_url: 'https://kostat.go.kr/', label: '한국 소비자물가 전년비',
      date_basis: 'release_date', points: [['2026-01-05', 2.1]], reference_periods: { '2026-01-05': '2025-12' },
    },
  },
};
const modelInputs = {
  schema_version: 'market_chart_model_inputs_v1',
  generated_at: '2026-01-05T00:00:00Z',
  models: [{
    key: 'B', label: '모델 B', market: 'KOSPI', status: 'ready',
    target_date: '2026-01-05', asof_date: '2026-01-04', issue_stage: 'premarket',
    prediction_cutoff: '2026-01-05T00:00:00Z', run_id: 'run-fixture', publication_id: 'fixture', provenance: 'frozen',
    rows: [{
      key: 'return_1', label: '전 거래일 수익률', date: '2026-01-04', value: -0.0314159265, unit: 'ratio',
      chart_series_key: 'kospi', chart_transform: 'return1', derivation: '동결 소스 기준', availability: 'reconstructed', heads: [],
    }, {
      key: 'vix', label: 'VIX', date: null, value: null, unit: 'pt', chart_series_key: 'vix', chart_transform: 'level',
      derivation: null, availability: 'unavailable', reason: '입력 기록 없음', heads: [],
    }],
  }],
};

const continuousDays = Array.from({ length: 48 }, (_, index) =>
  new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10));
const domesticClose = continuousDays.flatMap((date, index) => date === '2026-01-24' ? [] : [[date, 100 + index]]);
const publishedReturns = domesticClose.flatMap(([date]) => date === '2026-01-25' ? [] : [[date, date === '2026-01-01' ? -3.5 : 1]]);
const gapHistory = {
  ...history,
  series: {
    ...history.series,
    kospi: { ...history.series.kospi, points: domesticClose },
    kospi_return: { ...source, unit: 'percent', points: publishedReturns },
    kospi_volatility: { ...source, unit: 'percent', points: [['2026-01-01', 13.25], ['2026-01-23', 12.75]] },
  },
};

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const base = `http://127.0.0.1:${server.address().port}/reliable-index-forecast.html`;
  try {
    for (const width of [360, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      page.setDefaultTimeout(4000);
      let servedHistory = history;
      let servedInputs = modelInputs;
      await page.route('**/data/market-chart-history.json', route => route.fulfill({ json: servedHistory }));
      await page.route('**/data/market-chart-model-inputs.json', route => route.fulfill({ json: servedInputs }));
      let activePublication = 'fixture';
      await page.route('**/data/model_b_forecast.json', route => route.fulfill({ json: {
        schema_version: 'model_b_forecast_v1', publication_id: activePublication,
        markets: [modelInputs.models[0]],
      } }));
      await page.goto(`${base}#cross-assets`, { waitUntil: 'networkidle' });
      const explorer = page.locator('#cross-assets');
      const category = explorer.getByRole('combobox', { name: '지표 분류' });
      const chart = explorer.getByRole('combobox', { name: '세부 지표' });
      const observedDate = explorer.getByRole('combobox', { name: '관측일' });
      const readings = explorer.locator('#market-explorer-readings');
      assert.equal(await explorer.getByRole('combobox', { name: '기간', exact: true }).inputValue(), '1y');
      assert.equal(await explorer.getByRole('img').count(), 1);
      assert.match(await readings.innerText(), /99 pt/);
      assert.match(await readings.innerText(), /72 pt/);
      assert.match(await explorer.locator('#market-explorer-description').innerText(), /2026-01-02/);
      await chart.selectOption('kr-returns-1');
      assert.match(await readings.innerText(), /0(?:\.0+)?%/);
      await explorer.getByRole('button', { name: '이전 관측일' }).click();
      assert.equal(await observedDate.inputValue(), '2026-01-03');
      assert.match(await readings.innerText(), /[-−]10(?:\.0+)?%/);
      assert.equal(await explorer.getByRole('img').count(), 1);

      await category.selectOption('economics');
      await chart.selectOption('kr_inflation');
      assert.match(await readings.innerText(), /발표일\s*2026-01-05/);
      assert.match(await readings.innerText(), /기준 기간\s*2025-12/);
      assert.equal(await readings.getByRole('link', { name: '통계청' }).count(), 1);

      await explorer.getByRole('combobox', { name: '예측 입력' }).selectOption('B:KOSPI');
      const model = explorer.locator('#market-explorer-model-inputs');
      await model.getByRole('table').waitFor();
      assert.match(await model.innerText(), /-3\.1416%/);
      assert.match(await model.innerText(), /원본 비율 -0\.0314159265/);
      assert.match(await model.innerText(), /입력 기록 없음/);
      assert.equal(await chart.locator('option').count(), 2);
      assert.equal(await observedDate.inputValue(), '2026-01-04');
      assert.equal(await explorer.getByRole('img').count(), 1);
      await chart.selectOption('vix:level');
      assert.match(await readings.innerText(), /99 pt/);
      assert.match(await explorer.locator('#market-explorer-description').innerText(), /참고 시장 이력/);
      assert.doesNotMatch(await model.getByRole('row').filter({ hasText: 'VIX' }).innerText(), /99/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `page overflow at ${width}`);

      const filter = explorer.getByRole('combobox', { name: '예측 입력' });
      await filter.selectOption('');
      activePublication = 'newer-publication';
      await filter.selectOption('B:KOSPI');
      await model.getByText('현재 게시된 예측과 다른 실행의 입력 기록입니다.').waitFor();
      assert.equal(await model.getByRole('table').count(), 0);
      assert.equal(await explorer.getByRole('img').count(), 0);

      await page.getByRole('tab', { name: '미국 시장' }).click();
      await explorer.getByRole('link', { name: '시장 수급', exact: true }).click();
      await page.waitForFunction(() => document.getElementById('market-tab-kr').getAttribute('aria-selected') === 'true');
      assert.equal(await page.locator('#kospi-flow').isVisible(), true);

      servedHistory = gapHistory;
      servedInputs = { ...modelInputs, models: [{ ...modelInputs.models[0], rows: [...modelInputs.models[0].rows, {
        key: 'volatility_20d', label: '20일 일간 변동성', value: null, unit: 'ratio', chart_series_key: 'kospi',
        chart_transform: 'volatility20_daily', availability: 'unavailable',
      }] }] };
      activePublication = 'fixture';
      await page.goto(`${base}?history=gap#cross-assets`, { waitUntil: 'networkidle' });
      const availableDate = date => observedDate.locator(`option[value="${date}"]`).count();
      for (const [chart, expected] of [['kr-returns-1', /-3\.5%/], ['kr-volatility', /13\.25%/]]) {
        await explorer.getByRole('combobox', { name: '세부 지표' }).selectOption(chart);
        assert.equal(await availableDate('2026-01-01'), 1, 'published warm-up must retain first close');
        await observedDate.selectOption('2026-01-01');
        assert.match(await readings.innerText(), /100 pt/);
        assert.match(await readings.innerText(), expected);
        assert.equal(await availableDate('2026-01-25'), 0, 'published gap must remain empty');
      }
      for (const [key, gap, resumed] of [['kr-returns-5', '2026-01-29', '2026-01-30'], ['kr-returns-20', '2026-02-13', '2026-02-14']]) {
        await chart.selectOption(key);
        assert.equal(await availableDate(gap), 0, 'multi-session return cannot span the gap');
        assert.equal(await availableDate(resumed), 1, 'multi-session return resumes after a complete window');
      }
      await filter.selectOption('B:KOSPI');
      await model.getByRole('table').waitFor();
      await chart.selectOption('kospi:volatility20_daily');
      assert.equal(await availableDate('2026-02-13'), 0, 'daily volatility cannot span the gap');
      assert.equal(await availableDate('2026-02-14'), 1, 'daily volatility resumes after a complete window');
      await page.close();
    }
    console.log('Market data explorer passed at mobile and desktop widths.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
