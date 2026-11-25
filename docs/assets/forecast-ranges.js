(() => {
  'use strict';

  // The local GPU publishers replace the marked section with one card per market.
  // Leave that source readable if either market cannot be projected into model rows.
  const source = document.getElementById('model-bc-range');
  const overview = source?.querySelector(':scope > p');
  const grid = source?.querySelector('.bc-range-grid');
  const target = source?.dataset.targetDate;
  const asof = overview?.textContent.match(/기준 종가일 (\d{4}-\d{2}-\d{2})/)?.[1];
  function openAnchoredDetails() {
    const destination = document.getElementById(location.hash.slice(1));
    const details = destination === source ? destination?.querySelector('details') : destination?.closest('#model-compare');
    if (details) details.open = true;
  }
  window.addEventListener('hashchange', openAnchoredDetails);
  openAnchoredDetails();
  if (!grid || !asof || !/^\d{4}-\d{2}-\d{2}$/.test(target)) return;

  function buildCard(market, dates, content) {
    const article = document.createElement('article');
    article.className = 'forecast-card research-card';
    article.innerHTML = `<h3>${marketLabelMarkup(market)}</h3>`;
    article.append(dates.cloneNode(true), ...content.map(node => node.cloneNode(true)));
    article.querySelector('.hero-metric span').textContent = '종가 예측 범위 · 명목 80%';
    return article;
  }

  function projectMarket(market, label) {
    const original = [...grid.children].find(card => card.querySelector('h3')?.textContent.trim() === label);
    if (!original) return null;
    const parts = [...original.children];
    const dates = original.querySelector('.card-dates');
    const ranges = parts.filter(part => part.matches('.hero-metric') && part.querySelector('span') && part.querySelector('strong'));
    if (!dates || ranges.length !== 2 || !ranges[0].textContent.includes('모델 B') || !ranges[1].textContent.includes('모델 C')) return null;
    const bStart = parts.indexOf(ranges[0]);
    const cStart = parts.indexOf(ranges[1]);
    const b = buildCard(market, dates, parts.slice(bStart, cStart));
    const c = buildCard(market, dates, parts.slice(cStart));

    const bProbabilities = b.querySelector('.prob-row');
    const expectedLine = b.querySelector('.detail-line');
    const expected = expectedLine?.textContent.match(/^예상 종가 ([\d,.]+) · (.+)$/);
    const cProbabilities = c.querySelector('.detail-line');
    const events = cProbabilities?.textContent.match(/^다음 거래일 C 예측 · 3% 이상 하락 (\d+(?:\.\d+)?%) · 중립 (\d+(?:\.\d+)?%) · 3% 이상 상승 (\d+(?:\.\d+)?%)$/);
    if (!bProbabilities || !expected || !events) return null;

    const estimate = document.createElement('div');
    estimate.className = 'expected-close';
    estimate.innerHTML = `예상 종가 <strong>${expected[1]}</strong>`;
    expectedLine.textContent = expected[2].replace('범위 적중', '재구성 범위 적중');
    b.querySelector('.hero-metric').after(estimate, bProbabilities);
    bProbabilities.querySelectorAll('span').forEach(span => { span.textContent = span.textContent.replace(/^B /, ''); });

    const [, down, neutral, up] = events;
    const probabilities = document.createElement('div');
    probabilities.className = 'prob-row three-way';
    probabilities.innerHTML = `
      <div class="prob-box up"><span>3% 이상 상승</span><strong>${up}</strong></div>
      <div class="prob-box neutral"><span>중립</span><strong>${neutral}</strong></div>
      <div class="prob-box down"><span>3% 이상 하락</span><strong>${down}</strong></div>`;
    cProbabilities.replaceWith(probabilities);
    const coverage = c.querySelector('.detail-line');
    if (coverage) coverage.textContent = coverage.textContent.replace('범위 적중', '재구성 범위 적중');
    const followup = [...c.querySelectorAll('.detail-line')].find(line => line.textContent.includes('급등 다음 날'));
    if (followup) {
      const details = document.createElement('details');
      details.className = 'market-evaluation';
      const summary = document.createElement('summary');
      summary.textContent = '급등 다음 거래일 과거 사례';
      followup.replaceWith(details);
      details.append(summary, followup);
    }
    return { b, c };
  }

  const cards = [projectMarket('KOSPI', '코스피'), projectMarket('KOSDAQ', '코스닥')];
  const bands = ['model-b', 'model-c'].map(id => document.getElementById(id));
  if (cards.some(card => !card) || bands.some(band => !band)) return;

  const completed = overview.querySelector('time');
  for (const band of bands) {
    const model = band.id.slice(-1);
    const dates = band.querySelector('[data-range-dates]');
    dates.innerHTML = `
      <span>기준 종가일 <time datetime="${asof}">${asof}</time></span>
      <span>대상 거래일 <time datetime="${target}">${target}</time>${relativeDateMarkup(target, Date.now(), 'target')}
        <span class="session-ended" data-target-date="${target}" data-kr-session hidden>이미 종료</span></span>`;
    if (model === 'c' && completed) {
      const inference = document.createElement('span');
      inference.append('추론 완료 ', completed.cloneNode(true));
      dates.append(inference);
    }
    band.querySelector('[data-range-cards]').replaceChildren(...cards.map(card => card[model]));
    band.hidden = false;
  }

  const details = document.createElement('details');
  details.className = 'forecast-support';
  const summary = document.createElement('summary');
  summary.id = source.firstElementChild.id;
  summary.textContent = '모델 B·C 입력 데이터와 검증 결과';
  details.append(summary, ...[...source.children].filter(node => node !== source.firstElementChild && node !== grid));
  source.replaceChildren(details);
  source.classList.add('range-enhanced');
  document.querySelectorAll('[data-range-nav]').forEach(link => { link.href = `#${link.dataset.rangeNav}`; });

  openAnchoredDetails();
  refreshKstLabels();
})();
