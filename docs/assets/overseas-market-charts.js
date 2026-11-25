(() => {
  'use strict';
  const section = document.getElementById('overseas-market-charts');
  if (!section) return;

  const range = document.getElementById('overseas-chart-range');
  const priceStatus = document.getElementById('overseas-price-status');
  const flowContent = document.getElementById('sk-hynix-flow-content');
  const holdingsContent = document.getElementById('overseas-holdings-content');
  const priceCards = {
    SKHY: document.getElementById('overseas-price-SKHY'),
    KORU: document.getElementById('overseas-price-KORU'),
  };
  const groups = [['foreign', '외국인'], ['institution', '기관'], ['individual', '개인']];
  const units = { shares: '주', KRW: '원' };
  const colors = { up: '#a3201b', down: '#174b9a', flat: '#66717d', grid: '#d9dee5' };
  const dollars = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4,
  });
  const compact = new Intl.NumberFormat('ko-KR', { notation: 'compact', maximumFractionDigits: 1 });
  const kst = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  let prices;
  let flow;
  let plots = [];

  function esc(text) {
    return String(text).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  }

  function httpsUrl(text) {
    try {
      const url = new URL(text);
      return url.protocol === 'https:' && !url.username && !url.password ? url : null;
    } catch {
      return null;
    }
  }

  function sessionDate(text) {
    if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
    const date = new Date(`${text}T00:00:00Z`);
    return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === text;
  }

  function ordered(rows) {
    return Array.isArray(rows) && rows.every((row, index) => row && sessionDate(row.date) &&
      (index === 0 || rows[index - 1].date < row.date));
  }

  function snapshotTime(text) {
    if (typeof text !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(text)) return '';
    const date = new Date(text);
    return Number.isFinite(date.valueOf()) ? `파일 생성 ${kst.format(date)} KST` : '';
  }

  function sourceMarkup(source) {
    if (typeof source?.label !== 'string' || !source.label.trim()) return '';
    const label = esc(source.label);
    const url = httpsUrl(source.url);
    return url ? `출처 <a href="${esc(url.href)}">${label}</a>` : `출처 ${label}`;
  }

  function emptyMarkup(message, source) {
    const credit = sourceMarkup(source);
    return `<p class="overseas-chart-empty">${message}</p>${credit ? `<p class="overseas-market-meta">${credit}</p>` : ''}`;
  }

  function direction(value) {
    return value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
  }

  function signed(value, formatter) {
    return `${value > 0 ? '+' : value < 0 ? '−' : ''}${formatter(Math.abs(value))}`;
  }

  function statusMarkup(status) {
    if (status === 'stale') return ' · <span class="overseas-market-stale">지난 자료 · 최신 거래일 미확인</span>';
    if (status === 'partial') return ' · <span class="overseas-market-stale">일부 자료</span>';
    return '';
  }

  function chartMarkup(description, attribute = '') {
    return `<canvas class="overseas-market-canvas" ${attribute} role="img" aria-label="${esc(description)}">아래 날짜별 표에서 값을 확인할 수 있습니다.</canvas>`;
  }

  function tableMarkup(title, headers, htmlRows) {
    return `<details class="overseas-market-details"><summary>${title}</summary>
      ${tableRegion(title, headers, htmlRows)}</details>`;
  }

  function tableRegion(title, headers, htmlRows) {
    return `<div class="overseas-market-table" role="region" aria-label="${title}" tabindex="0"><table>
      <thead><tr>${headers.map(label => `<th scope="col">${label}</th>`).join('')}</tr></thead>
      <tbody>${htmlRows}</tbody></table></div>`;
  }

  function usablePrice(symbol, data) {
    return data?.symbol === symbol && data.currency === 'USD' && ['ready', 'stale'].includes(data.status) &&
      ordered(data.points) && data.points.length > 0 && data.points.at(-1).date === data.last_session &&
      data.points.every(point => Number.isFinite(point.close) && point.close > 0);
  }

  function renderPrice(symbol, data, limit) {
    const card = priceCards[symbol];
    if (!usablePrice(symbol, data)) {
      card.innerHTML = emptyMarkup('종가 자료가 없습니다.', data?.source);
      return;
    }

    const points = data.points.slice(-limit);
    const first = points[0];
    const latest = points.at(-1);
    const change = Number.isFinite(data.previous_close) && data.previous_close > 0
      ? (latest.close - data.previous_close) / data.previous_close * 100 : null;
    const changeText = change === null ? '전 거래일 대비 자료 없음' :
      `${signed(change, value => value.toFixed(2))}% 전 거래일 대비`;
    const credit = sourceMarkup(data.source);
    const table = points.slice().reverse().map(point => `<tr><th scope="row">${point.date}</th><td>${dollars.format(point.close)}</td></tr>`).join('');
    card.innerHTML = `
      <p class="overseas-market-stats"><strong>${dollars.format(latest.close)}</strong>
        <span class="overseas-${direction(change)}">${changeText}</span></p>
      <p class="overseas-market-meta">정규장 기준일 <time datetime="${latest.date}">${latest.date}</time> · ${points.length}거래일${statusMarkup(data.status)}</p>
      <p class="overseas-market-legend"><span class="overseas-up">상승</span><span class="overseas-down">하락</span><span class="overseas-flat">보합</span></p>
      ${chartMarkup(`${symbol} 미국 정규장 종가(USD), ${first.date}–${latest.date}, 최근 종가 ${dollars.format(latest.close)}`)}
      ${credit ? `<p class="overseas-market-meta">${credit}</p>` : ''}
      ${tableMarkup('날짜별 종가 보기', ['미국 거래일', '종가 (USD)'], table)}`;
    const canvas = card.querySelector('canvas');
    if (canvas) plots.push(() => drawPrices(canvas, points));
  }

  function usableFlow(data) {
    return data?.schema_version === 1 && data.symbol === '000660' && data.market === 'KRX' &&
      ['ready', 'partial', 'stale'].includes(data.status) && Object.hasOwn(units, data.unit) &&
      ordered(data.series) && data.series.every(row => groups.every(([key]) => row[key] === null || Number.isFinite(row[key])));
  }

  function renderFlow(data, limit) {
    flowContent.setAttribute('aria-busy', 'false');
    if (!usableFlow(data) || !data.series.length) {
      flowContent.innerHTML = emptyMarkup(data ? '일별 순매수 자료가 없습니다.' : '국내 순매수 자료를 불러오지 못했습니다.', data?.source);
      return;
    }

    const rows = data.series.slice(-limit);
    const values = rows.flatMap(row => groups.map(([key]) => row[key]).filter(Number.isFinite));
    if (!values.length) {
      flowContent.innerHTML = emptyMarkup('선택 기간의 순매수 자료가 없습니다.', data.source);
      return;
    }

    const unit = units[data.unit];
    const latest = rows.at(-1);
    const credit = sourceMarkup(data.source);
    const generated = snapshotTime(data.generated_at);
    const cards = groups.map(([key, label]) => {
      const value = latest[key];
      const chart = rows.some(row => Number.isFinite(row[key]))
        ? chartMarkup(`SK하이닉스 ${label} 일별 순매수 (${unit}), ${rows[0].date}–${latest.date}`, `data-investor="${key}"`)
        : '<p class="overseas-chart-empty">선택 기간 자료가 없습니다.</p>';
      return `<article><h4>${label}</h4>
        <p class="overseas-market-meta">기준일 <strong class="overseas-${direction(value)}">${value === null ? '자료 없음' : `${signed(value, v => v.toLocaleString('ko-KR'))}${unit}`}</strong></p>
        ${chart}</article>`;
    }).join('');
    const table = rows.slice().reverse().map(row => {
      const cells = groups.map(([key]) => `<td>${row[key] === null ? '자료 없음' : signed(row[key], v => v.toLocaleString('ko-KR'))}</td>`).join('');
      return `<tr><th scope="row">${row.date}</th>${cells}</tr>`;
    }).join('');

    flowContent.innerHTML = `
      <p class="overseas-market-meta">기준일 <time datetime="${latest.date}">${latest.date}</time> · ${rows.length}거래일 · 순매수 (${unit})${statusMarkup(data.status)}</p>
      <p class="overseas-market-legend"><span class="overseas-up">순매수 (+)</span><span class="overseas-down">순매도 (−)</span></p>
      <div class="overseas-flow-grid">${cards}</div>
      ${credit || generated ? `<p class="overseas-market-meta">${[credit, generated].filter(Boolean).join(' · ')}</p>` : ''}
      ${tableMarkup('날짜별 국내 순매수 보기', ['한국 거래일', ...groups.map(([, label]) => label)], table)}`;
    const bound = niceStep(Math.max(1, ...values.map(Math.abs)));
    flowContent.querySelectorAll('canvas').forEach(canvas => {
      plots.push(() => drawFlow(canvas, rows, canvas.dataset.investor, bound));
    });
  }

  function renderHoldings(data) {
    holdingsContent.setAttribute('aria-busy', 'false');
    if (data?.schema_version !== 1 || data.status !== 'ready' || data.coverage !== 'selected_filings') {
      holdingsContent.innerHTML = emptyMarkup('보유 공시 자료를 불러오지 못했습니다.');
      return;
    }

    const rows = Object.keys(priceCards).map(symbol => {
      const filings = data.securities?.[symbol]?.filings;
      if (!Array.isArray(filings)) return '';
      return filings.map(entry => {
        const url = httpsUrl(entry?.table_url);
        const filedDate = typeof entry?.filed_at === 'string' ? entry.filed_at.slice(0, 10) : '';
        if (!entry || typeof entry.manager !== 'string' || !sessionDate(entry.period_end) ||
            !sessionDate(filedDate) || !Number.isFinite(Date.parse(entry.filed_at)) ||
            !Number.isSafeInteger(entry.shares) || entry.shares < 0 ||
            url?.hostname !== 'www.sec.gov') return '';
        return `<tr><th scope="row">${symbol}</th><td>${esc(entry.manager)}</td><td>${entry.period_end}</td>
          <td>${entry.shares.toLocaleString('ko-KR')}</td><td><a href="${esc(url.href)}" aria-label="${esc(`${symbol} ${entry.manager} SEC 공시 ${filedDate}`)}">${filedDate}</a></td></tr>`;
      }).join('');
    }).join('');
    const asOf = sessionDate(data.as_of) ? `<p class="overseas-market-meta">확인 기준일 ${data.as_of}</p>` : '';
    holdingsContent.innerHTML = rows ? asOf + tableRegion('SEC 13F 보유 공시',
      ['종목', '운용사', '분기 말', '보유 (주)', 'SEC 공시일'], rows) : emptyMarkup('확인된 보유 공시 자료가 없습니다.');
  }

  function niceStep(value) {
    const scale = 10 ** Math.floor(Math.log10(value));
    const fraction = value / scale;
    return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * scale;
  }

  function chartAxes(canvas, rows, ticks, formatter, centered) {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx || width < 90 || height < 40) return null;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillStyle = colors.flat;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';

    const left = Math.max(26, ...ticks.map(value => ctx.measureText(formatter(value)).width)) + 8;
    const right = width - 8;
    const top = 10;
    const bottom = height - 22;
    const space = right - left;
    const low = ticks[0];
    const high = ticks.at(-1);
    const y = value => top + (high - value) / (high - low) * (bottom - top);
    const x = index => left + space * (centered ? (index + 0.5) / rows.length : index / (rows.length - 1));
    ticks.forEach(value => {
      const baseline = centered && value === 0;
      ctx.strokeStyle = baseline ? colors.flat : colors.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y(value));
      ctx.lineTo(right, y(value));
      ctx.stroke();
      ctx.fillText(formatter(value), left - 6, y(value));
    });

    const labelCount = Math.min(rows.length, width >= 420 ? 4 : 2);
    for (let index = 0; index < labelCount; index += 1) {
      const position = labelCount === 1 ? 0 : Math.round(index * (rows.length - 1) / (labelCount - 1));
      const first = labelCount > 1 && index === 0;
      const last = labelCount > 1 && index === labelCount - 1;
      ctx.textAlign = first ? 'left' : last ? 'right' : 'center';
      const labelX = first ? left : last ? right : x(position);
      ctx.fillText(rows[position].date.slice(5), labelX, height - 8);
    }
    return { ctx, x, y, space };
  }

  function drawPrices(canvas, points) {
    const values = points.map(point => point.close);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const step = niceStep((max - min || max * 0.08) / 4);
    const start = Math.max(0, Math.floor(min / step) * step - (min === max ? step : 0));
    const end = Math.max(start + step * 2, Math.ceil(max / step) * step + (min === max ? step : 0));
    const ticks = Array.from({ length: Math.round((end - start) / step) + 1 }, (_, index) => start + index * step);
    const plot = chartAxes(canvas, points, ticks,
      value => value.toLocaleString('en-US', { maximumFractionDigits: 3 }), points.length === 1);
    if (!plot) return;

    const { ctx, x, y } = plot;
    ctx.lineWidth = 1.6;
    ctx.lineCap = 'round';
    values.slice(1).forEach((value, index) => {
      ctx.strokeStyle = colors[direction(value - values[index])];
      ctx.beginPath();
      ctx.moveTo(x(index), y(values[index]));
      ctx.lineTo(x(index + 1), y(value));
      ctx.stroke();
    });
    const latest = values.length - 1;
    ctx.fillStyle = colors[direction(latest ? values[latest] - values[latest - 1] : 0)];
    ctx.beginPath();
    ctx.arc(x(latest), y(values[latest]), 2.8, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawFlow(canvas, rows, investor, bound) {
    const plot = chartAxes(canvas, rows, [-bound, -bound / 2, 0, bound / 2, bound],
      value => value < 0 ? `−${compact.format(-value)}` : compact.format(value), true);
    if (!plot) return;

    const { ctx, x, y, space } = plot;
    const width = Math.min(12, space / rows.length * 0.72);
    const zero = y(0);
    rows.forEach((row, index) => {
      const value = row[investor];
      if (value === null) return;
      ctx.fillStyle = colors[direction(value)];
      const top = Math.min(y(value), zero);
      ctx.fillRect(x(index) - width / 2, top, width, Math.max(1, Math.abs(y(value) - zero)));
    });
  }

  function drawPlots() {
    plots.forEach(draw => draw());
  }

  function render() {
    const limit = Number(range.value);
    plots = [];
    Object.keys(priceCards).forEach(symbol => renderPrice(symbol, prices?.instruments?.[symbol], limit));
    renderFlow(flow, limit);
    drawPlots();
  }

  async function load(path) {
    try {
      const response = await fetch(`data/${path}.json`, { cache: 'no-store' });
      return response.ok ? await response.json() : null;
    } catch {
      return null;
    }
  }

  range.addEventListener('change', render);
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(drawPlots).observe(section);
  } else {
    window.addEventListener('resize', drawPlots);
  }
  Promise.all([load('overseas-market-prices'), load('sk-hynix-investor-flow')]).then(([priceData, flowData]) => {
    prices = priceData?.schema_version === 1 ? priceData : null;
    flow = flowData;
    const generated = snapshotTime(prices?.generated_at);
    priceStatus.textContent = prices ? `미국 현지 거래일 · 정규장 종가 (USD) · 주식 분할 반영·배당 제외${generated ? ` · ${generated}` : ''}` : '미국 종가 자료를 불러오지 못했습니다.';
    range.disabled = !Object.keys(priceCards).some(symbol => usablePrice(symbol, prices?.instruments?.[symbol])) &&
      !(usableFlow(flow) && flow.series.length);
    render();
  });
  if (holdingsContent) load('overseas-institutional-holdings').then(renderHoldings);
})();
