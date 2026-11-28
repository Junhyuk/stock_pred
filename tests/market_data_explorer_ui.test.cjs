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

const marketPoint = (unit, dateBasis, points) => ({ ...source, unit, date_basis: dateBasis, points });
const longHistory = {
  ...history,
  as_of_date: '2026-11-28',
  series: {
    ...history.series,
    kr_policy_rate: {
      ...marketPoint('percent', 'effective_date', [['2023-10-19', 3.5], ['2024-10-11', 3.25], ['2025-05-29', 2.5]]),
      verified_through: '2026-10-15',
    },
    jp_policy_rate: marketPoint('percent', 'effective_date', [['2024-03-21', 0.05], ['2025-01-24', 0.5]]),
    jp_negative_policy_balances: marketPoint('percent', 'effective_date', [['2023-11-28', -0.1], ['2024-03-20', -0.1]]),
    ea_inflation: marketPoint('percent', 'reference_month', [
      ['2023-10-01', 2.9], ['2023-11-01', 2.4], ['2024-01-01', 2.8], ['2024-02-01', 2.6], ['2026-10-01', 2.1],
    ]),
    us_pce: {
      ...marketPoint('percent', 'reference_month', [['2024-07-01', 2.5], ['2024-08-01', 2.3]]),
      source: '미국 경제분석국', source_url: 'https://www.bea.gov/data/personal-consumption-expenditures-price-index',
      release_dates: { '2024-07-01': '2024-08-30' },
      release_urls: { '2024-07-01': 'https://www.bea.gov/news/2024/personal-income-and-outlays-july-2024' },
    },
    kr_1y: marketPoint('percent', 'observation_date', [
      ['2023-11-27', 99], ['2023-11-28', 3.7], ['2026-11-27', 3.2], ['2026-11-29', 99],
    ]),
    kr_5y: marketPoint('percent', 'observation_date', [['2026-11-27', 3.8]]),
    us_5y: marketPoint('percent', 'observation_date', [['2026-11-27', 4.3]]),
    jp_5y: marketPoint('percent', 'observation_date', [['2026-11-27', 1.5]]),
    margin_credit_total: marketPoint('KRW', 'settlement_date', [['2024-02-01', 17750000000000], ['2026-11-26', 22300000000000]]),
    usdkrw: marketPoint('KRW', 'session_date', [['2026-11-27', 1400]]),
    btc: marketPoint('USD', 'session_date', [['2026-11-27', 90000]]),
    eth: marketPoint('USD', 'session_date', [['2026-11-27', 3000]]),
  },
};

async function checkLongHistory(page, base, explorer, category, chart, observedDate, readings, width) {
  await page.goto(`${base}?history=3y#economic-calendar`, { waitUntil: 'networkidle' });
  const range = explorer.getByRole('combobox', { name: '기간', exact: true });
  const country = explorer.getByRole('combobox', { name: '국가·지역' });
  const go = async (section, categoryId) => {
    const link = page.locator(section).getByRole('link', { name: /3년 추이 보기/ });
    await link.focus();
    await link.press('Enter');
    assert.equal(await range.inputValue(), '3y');
    assert.equal(await category.inputValue(), categoryId);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'cross-assets-title');
    assert.equal(await explorer.getByRole('img').count(), 1);
  };

  await go('#economic-calendar', 'economics');
  await country.selectOption('KR');
  await chart.selectOption('kr_policy_rate');
  assert.match(await readings.innerText(), /적용일\s*2025-05-29/);
  assert.match(await explorer.getByRole('img').locator('path').first().getAttribute('d'), /H.+V.+ H[\d.]+$/);
  await explorer.getByRole('combobox', { name: '적용일' }).selectOption({ label: '2023-11-28 · 조회 시작일' });
  assert.match(await readings.innerText(), /3\.5%.*적용일\s*2023-10-19/s);
  await country.selectOption('JP');
  await chart.selectOption('jp_policy_rate');
  assert.doesNotMatch(await readings.innerText(), /−0\.1|\-0\.1/);
  await chart.selectOption('jp_negative_policy_balances');
  assert.match(await readings.innerText(), /2024-03-20/);
  assert.doesNotMatch(await readings.innerText(), /2025-01-24/);
  await country.selectOption('EA');
  await chart.selectOption('ea_inflation');
  assert.match(await readings.innerText(), /2026-10/);
  const reference = explorer.getByRole('combobox', { name: '기준 기간' });
  assert.equal(await reference.locator('option[value="2023-11-01"]').count(), 1);
  assert.equal(await reference.locator('option[value="2023-10-01"]').count(), 0);
  assert.match(await explorer.getByRole('img').locator('path').first().getAttribute('d'), /^M[^ML]+ M[^ML]+ L/);
  await country.selectOption('US');
  await chart.selectOption('us_pce');
  assert.doesNotMatch(await readings.innerText(), /2024-08-30/);
  const plot = explorer.getByRole('img');
  await plot.click({ position: { x: 1, y: 20 } });
  assert.equal(await explorer.getByRole('combobox', { name: '기준 기간' }).inputValue(), '2024-07-01');
  assert.match(await readings.innerText(), /기준 기간 2024-07/);
  assert.match(await readings.innerText(), /발표일 2024-08-30/);
  assert.match(await readings.innerText(), /최근 관측.*2024-08/);
  assert.match(await readings.getByRole('link', { name: '미국 경제분석국' }).getAttribute('href'), /personal-income-and-outlays-july-2024/);

  await page.locator('#market-indicators-title').click();
  await go('#market-indicators', 'kr-rates');
  await chart.selectOption('kr_1y');
  assert.deepEqual(await observedDate.locator('option').evaluateAll(options => options.map(option => option.value)), ['2026-11-27', '2023-11-28']);
  assert.match(await explorer.locator('#market-explorer-description').innerText(), /2023-11-28 ~ 2026-11-28/);
  for (const [group, series] of [['kr-rates', 'kr_5y'], ['us-rates', 'us_5y'], ['jp-rates', 'jp_5y']]) {
    await category.selectOption(group);
    await chart.selectOption(series);
    assert.equal(await explorer.getByRole('img').count(), 1);
  }
  await category.selectOption('margin-credit');
  assert.match(await readings.innerText(), /22\.3 조 원/);
  assert.match(await readings.innerText(), /관측 범위 2024-02-01 ~ 2026-11-26/);
  assert.match(await readings.innerText(), /결제일 2026-11-26/);
  assert.equal(await explorer.getByRole('img').locator('path').count(), 0, 'gapped credit only marks observations');

  await explorer.locator('details.market-explorer-snapshot summary').click();
  await go('#cross-assets details', 'cross-snapshot');
  for (const key of ['usdkrw', 'dxy', 'wti', 'brent', 'btc', 'eth']) await chart.selectOption(key);
  assert.match(await readings.innerText(), /3,000 USD/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `3y page overflow at ${width}`);
  await page.reload({ waitUntil: 'networkidle' });
  assert.equal(await category.inputValue(), 'cross-snapshot');
  assert.equal(await range.inputValue(), '3y');
  await page.goto(`${base}?history=3y#cross-assets`, { waitUntil: 'networkidle' });
  assert.equal(await category.inputValue(), 'domestic');
  assert.equal(await range.inputValue(), '1y');
}

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
      servedHistory = longHistory;
      await checkLongHistory(page, base, explorer, category, chart, observedDate, readings, width);
      await page.close();
    }
    console.log('Market data explorer passed at mobile and desktop widths.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
