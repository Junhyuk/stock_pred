(() => {
  'use strict';
  const status = document.getElementById('cfd-status');
  const charts = document.getElementById('cfd-charts');
  if (!status || !charts) return;
  const labels = { KOSPI: '코스피', KOSDAQ: '코스닥', with_margin: '증거금 포함', without_margin: '증거금 차감' };
  const colors = { KOSPI: '#2156a6', KOSDAQ: '#087e70', with_margin: '#2156a6', without_margin: '#8250a7' };
  const states = {
    source_current: '공시 확인',
    source_delayed: '이전 공시 표시',
    source_unavailable: '공시 조회 지연',
    source_permission_unconfirmed: '잔고 차트는 원천의 이용 범위를 확인한 뒤 표시됩니다.',
    pdf_parser_unavailable: '잔고 PDF 읽기 대기',
    stale: '마지막 정상 잔고 표시',
    unavailable: '잔고 자료를 확인하지 못했습니다.'
  };
  const datePattern = /^\d{4}-\d{2}-\d{2}$/;
  const trillions = (value) => (value / 1e12).toLocaleString('ko-KR', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const dateTime = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short', hourCycle: 'h23'
  });

  function element(type, text, className) {
    const node = document.createElement(type);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  async function read(name) {
    const response = await fetch(`data/${name}.json`, { cache: 'no-store' });
    if (!response.ok) throw new Error('CFD snapshot unavailable');
    return response.json();
  }

  function line(canvas, points, key) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const { width, height } = canvas;
    const left = 58;
    const right = width - 20;
    const top = 20;
    const bottom = height - 27;
    const values = points.map(row => row.value / 1e12);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) * 0.12, 0.005);
    const high = max + padding;
    const low = Math.max(0, min - padding);
    const x = (i) => points.length > 1 ? left + (right - left) * i / (points.length - 1) : (left + right) / 2;
    const y = (value) => bottom - (bottom - top) * (value - low) / (high - low);
    ctx.font = '12px system-ui';
    for (const part of [0, 0.5, 1]) {
      const value = low + (high - low) * part;
      ctx.beginPath();
      ctx.strokeStyle = '#d9e1ed';
      ctx.lineWidth = 1;
      ctx.moveTo(left, y(value));
      ctx.lineTo(right, y(value));
      ctx.stroke();
      ctx.fillStyle = '#49586f';
      ctx.fillText(value.toFixed(2), 9, y(value) + 4);
    }
    ctx.beginPath();
    ctx.strokeStyle = colors[key];
    ctx.lineWidth = 2.5;
    values.forEach((value, i) => i ? ctx.lineTo(x(i), y(value)) : ctx.moveTo(x(i), y(value)));
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x(values.length - 1), y(values.at(-1)), 4, 0, 2 * Math.PI);
    ctx.fillStyle = colors[key];
    ctx.fill();
    ctx.fillStyle = '#49586f';
    ctx.fillText(points[0].date, left, height - 7);
    if (points.length > 1) {
      ctx.textAlign = 'right';
      ctx.fillText(points.at(-1).date, right, height - 7);
    }
  }

  function card(key, list, field) {
    if (!Array.isArray(list) || !list.length || !list.every((row, i) =>
      datePattern.test(row?.date) && Number.isSafeInteger(row[field]) && row[field] >= 0 &&
      (i === 0 || list[i - 1].date < row.date))) return null;
    const points = list.slice(-90).map(row => ({ date: row.date, value: row[field] }));
    const latest = points.at(-1);
    const article = element('article', null, 'cfd-chart-card');
    article.append(element('h4', labels[key]), element('p', `${trillions(latest.value)}조 원`, 'cfd-value'),
      element('p', `기준일 ${latest.date} · 최근 ${points.length}개 관측`, 'cfd-caption'));
    const canvas = document.createElement('canvas');
    canvas.width = 520;
    canvas.height = 170;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', `${labels[key]} 잔고 추이, 단위 조 원`);
    article.append(canvas);
    line(canvas, points, key);
    const details = element('details');
    details.append(element('summary', '날짜별 잔고'));
    const table = element('table', null, 'cfd-value-table');
    const head = element('tr');
    for (const title of ['기준일', '잔고 (조 원)']) {
      const heading = element('th', title);
      heading.scope = 'col';
      head.append(heading);
    }
    table.createTHead().append(head);
    const body = table.createTBody();
    for (const point of points.slice().reverse()) {
      body.insertRow().append(element('td', point.date), element('td', trillions(point.value)));
    }
    details.append(table);
    article.append(details);
    return article;
  }

  function group(title, contents, keys, field) {
    const cards = keys.map((key) => card(key, contents?.[key], field)).filter(Boolean);
    if (!cards.length) return;
    const grid = element('div', null, 'cfd-chart-grid');
    grid.append(...cards);
    charts.append(element('h3', title), grid);
  }

  async function start() {
    try {
      const info = await read('cfd_status');
      if (info.schema_version !== 'cfd_status_v1') throw new Error('invalid CFD status');
      const date = new Date(info.checked_at);
      const checked = Number.isNaN(date.valueOf()) ? '' : ` · 조회 ${dateTime.format(date)} KST`;
      const latest = datePattern.test(info.latest?.observation_date) && datePattern.test(info.latest?.posted_date)
        ? ` · 기준일 ${info.latest.observation_date} · 게시일 ${info.latest.posted_date}` : '';
      const decision = states[info.data_status] || '';
      const freeStale = info.frees_is_status === 'stale' ? ' · FreeSIS 지난 자료' : '';
      status.textContent = `${states[info.status] || '공시 상태 확인 중'}${latest}${checked}${decision ? ` · ${decision}` : ''}${freeStale}`;
      const [domestic, allReported] = await Promise.allSettled([
        ['ready', 'stale'].includes(info.data_status) ? read('cfd_market_history') : null,
        ['ready', 'stale'].includes(info.frees_is_status) ? read('cfd_frees_is_history') : null
      ]);
      if (domestic.value?.schema_version === 'cfd_market_history_v1') {
        group('국내 공시 종목 합계', domestic.value.markets, ['KOSPI', 'KOSDAQ'], 'value_krw');
      }
      if (allReported.value?.schema_version === 'cfd_frees_is_history_v1') {
        group('FreeSIS 전체 명목잔고 · 해외 포함', allReported.value.series,
          ['with_margin', 'without_margin'], 'total_krw');
      }
    } catch {
      status.textContent = '공시 조회 결과를 불러오지 못했습니다. 아래 원천에서 확인할 수 있습니다.';
    }
  }
  start();
})();
