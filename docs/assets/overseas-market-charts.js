(() => {
  'use strict';
  const section = document.getElementById('overseas-market-charts');
  if (!section) return;

  const range = document.getElementById('overseas-chart-range');
  const priceStatus = document.getElementById('overseas-price-status');
  const stocks = {
    '000660': { label: 'SK하이닉스', file: 'sk-hynix-investor-flow', content: document.getElementById('sk-hynix-flow-content') },
    '005930': { label: '삼성전자', file: 'samsung-electronics-investor-flow', content: document.getElementById('samsung-electronics-flow-content') },
  };
  const holdingsContent = document.getElementById('overseas-holdings-content');
  const priceCards = {
    SKHY: document.getElementById('overseas-price-SKHY'),
    KORU: document.getElementById('overseas-price-KORU'),
  };
  const groups = [['foreign', '외국인'], ['institution', '기관'], ['individual', '개인']];
  const units = { shares: '주', KRW: '원' };
  const investorColors = { foreign: '#096866', institution: '#634099', individual: '#8f590e' };
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
  let pricePlots = [];
  const flowPlots = [];

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
    if (canvas) pricePlots.push(() => drawPrices(canvas, points));
  }

  function usableFlow(symbol, data) {
    return data?.schema_version === 1 && data.symbol === symbol && data.market === 'KRX' &&
      ['ready', 'partial', 'stale'].includes(data.status) && Object.hasOwn(units, data.unit) &&
      ordered(data.series) && data.series.every(row => groups.every(([key]) => row[key] === null || Number.isFinite(row[key])));
  }

  function renderFlow(symbol, stock, data) {
    const content = stock.content;
    if (!content) return;
    content.setAttribute('aria-busy', 'false');
    if (!usableFlow(symbol, data) || !data.series.length) {
      content.innerHTML = emptyMarkup(data ? '일별 순매수 자료가 없습니다.' : '국내 순매수 자료를 불러오지 못했습니다.', data?.source);
      return;
    }

    const rows = data.series;
    const totals = Object.fromEntries(groups.map(([key]) => [key, { sum: 0, count: 0 }]));
    const first = sessionDate(data.window_start) ? data.window_start : rows[0].date;
    const latest = rows.at(-1);
    const target = sessionDate(data.window_end) ? data.window_end : latest.date;
    const missing = Array.isArray(data.coverage?.missing_sessions)
      ? data.coverage.missing_sessions.filter(day => sessionDate(day) && day >= first && day <= target)
      : [];
    const byDate = new Map(rows.map(row => [row.date, row]));
    const dates = [...new Set([...byDate.keys(), ...missing])].sort();
    const points = dates.map(date => {
      const row = byDate.get(date);
      const point = { date };
      for (const [key] of groups) {
        const value = row?.[key];
        if (Number.isFinite(value)) {
          totals[key].sum += value;
          totals[key].count += 1;
          point[key] = totals[key].sum;
        } else {
          point[key] = null;
        }
      }
      return point;
    });
    const values = points.flatMap(row => groups.map(([key]) => row[key]).filter(Number.isFinite));
    if (!values.length) {
      content.innerHTML = emptyMarkup('순매수 자료가 없습니다.', data.source);
      return;
    }

    const unit = units[data.unit];
    const expected = Math.max(rows.length, data.coverage?.expected_sessions || rows.length);
    const incomplete = groups.some(([key]) => totals[key].count < expected);
    const credit = sourceMarkup(data.source);
    const generated = snapshotTime(data.generated_at);
    const format = value => signed(value, n => n.toLocaleString('ko-KR'));
    const legend = groups.map(([key, label]) => {
      const { sum, count } = totals[key];
      const value = count ? `<strong class="overseas-${direction(sum)}">${format(sum)}${unit}</strong>` : '자료 없음';
      return `<span class="flow-${key}">${label} ${value}${count < expected ? ` <small>${count}/${expected}거래일</small>` : ''}</span>`;
    }).join('');
    const table = rows.slice().reverse().map(row => {
      const cells = groups.map(([key]) => `<td>${row[key] === null ? '자료 없음' : format(row[key])}</td>`).join('');
      return `<tr><th scope="row">${row.date}</th>${cells}</tr>`;
    }).join('');

    content.innerHTML = `
      <p class="overseas-market-meta">집계 기간 ${first}–${target} · 관측 끝 ${latest.date} · ${rows.length}거래일 · 순매수 (${unit})${statusMarkup(data.status)}</p>
      <p class="overseas-market-legend">${legend}</p>
      ${incomplete ? '<p class="overseas-market-meta">확인된 순매수만 합산했습니다.</p>' : ''}
      ${chartMarkup(`${stock.label} 투자자별 누적 순매수 (${unit}), ${first}–${target}`)}
      ${credit || generated ? `<p class="overseas-market-meta">${[credit, generated].filter(Boolean).join(' · ')}</p>` : ''}
      ${tableMarkup('날짜별 순매수 보기', ['한국 거래일', ...groups.map(([, label]) => label)], table)}`;
    const canvas = content.querySelector('canvas');
    if (canvas) {
      const bound = niceStep(Math.max(1, ...values.map(Math.abs)));
      flowPlots.push(() => drawCumulativeFlow(canvas, points, bound));
    }
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

  function chartAxes(canvas, rows, ticks, formatter, centered, cumulative = false) {
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
      const baseline = (centered || cumulative) && value === 0;
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
      ctx.fillText(rows[position].date.slice(cumulative ? 2 : 5), labelX, height - 8);
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

  function drawCumulativeFlow(canvas, rows, bound) {
    const plot = chartAxes(canvas, rows, [-bound, -bound / 2, 0, bound / 2, bound],
      value => signed(value, number => compact.format(number)), rows.length === 1, true);
    if (!plot) return;

    const { ctx, x, y } = plot;
    for (const [key] of groups) {
      ctx.strokeStyle = investorColors[key];
      ctx.fillStyle = investorColors[key];
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let previous = null;
      let latest = null;
      rows.forEach((row, index) => {
        const value = row[key];
        if (value !== null) {
          if (previous === null) ctx.moveTo(x(index), y(value));
          else ctx.lineTo(x(index), y(value));
          latest = { index, value };
        }
        previous = value;
      });
      ctx.stroke();
      if (latest) {
        ctx.beginPath();
        ctx.arc(x(latest.index), y(latest.value), 2, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  function drawPlots() {
    [...pricePlots, ...flowPlots].forEach(draw => draw());
  }

  function renderPrices() {
    const limit = Number(range.value);
    pricePlots = [];
    Object.keys(priceCards).forEach(symbol => renderPrice(symbol, prices?.instruments?.[symbol], limit));
    pricePlots.forEach(draw => draw());
  }

  async function load(path) {
    try {
      const response = await fetch(`data/${path}.json`, { cache: 'no-store' });
      return response.ok ? await response.json() : null;
    } catch {
      return null;
    }
  }

  range.addEventListener('change', renderPrices);
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(drawPlots).observe(section);
  } else {
    window.addEventListener('resize', drawPlots);
  }
  load('overseas-market-prices').then(priceData => {
    prices = priceData?.schema_version === 1 ? priceData : null;
    const generated = snapshotTime(prices?.generated_at);
    priceStatus.textContent = prices ? `미국 현지 거래일 · 정규장 종가 (USD) · 주식 분할 반영·배당 제외${generated ? ` · ${generated}` : ''}` : '미국 종가 자료를 불러오지 못했습니다.';
    range.disabled = !Object.keys(priceCards).some(symbol => usablePrice(symbol, prices?.instruments?.[symbol]));
    renderPrices();
  });
  for (const [symbol, stock] of Object.entries(stocks)) {
    if (stock.content) load(stock.file).then(data => {
      renderFlow(symbol, stock, data);
      flowPlots.forEach(draw => draw());
    });
  }
  if (holdingsContent) load('overseas-institutional-holdings').then(renderHoldings);
})();
