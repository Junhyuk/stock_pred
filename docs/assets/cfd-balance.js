(() => {
  'use strict';
  const status = document.getElementById('cfd-status');
  const charts = document.getElementById('cfd-charts');
  if (!status || !charts) return;
  const labels = { KOSPI: '코스피', KOSDAQ: '코스닥', with_margin: '증거금 포함', without_margin: '증거금 차감' };
  const colors = { KOSPI: '#2156a6', KOSDAQ: '#087e70', with_margin: '#2156a6', without_margin: '#8250a7', buy: '#9f352d', sell: '#2156a6' };
  const scopes = {
    all: { label: '전체 보고 시장 (해외 포함)', fields: ['buy_krw', 'sell_krw', 'total_krw'] },
    KOSPI: { label: '코스피', fields: ['kospi_buy_krw', 'kospi_sell_krw', 'kospi_krw'] },
    KOSDAQ: { label: '코스닥', fields: ['kosdaq_buy_krw', 'kosdaq_sell_krw', 'kosdaq_krw'] },
  };
  const states = {
    source_current: '공시 확인',
    source_delayed: '이전 공시 표시',
    source_unavailable: '공시 조회 지연',
    source_permission_unconfirmed: '자료 확인 중',
    pdf_parser_unavailable: '잔고 PDF 읽기 대기',
    stale: '마지막 정상 잔고 표시',
    unavailable: '잔고 자료를 확인하지 못했습니다.'
  };
  const datePattern = /^\d{4}-\d{2}-\d{2}$/;
  const dayMs = 86400000;
  const trillions = (value) => (value / 1e12).toLocaleString('ko-KR', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const exactEok = new Intl.NumberFormat('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 8 });
  const roundedEok = new Intl.NumberFormat('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const amount = value => `${exactEok.format(value / 1e8)}억 원`;
  function balanceSummary(value) {
    if (value > 0 && value < 1e6) return `${value.toLocaleString('ko-KR')}원`;
    return `${roundedEok.format(value / 1e8)}억 원`;
  }
  const changeText = (value) => (value / 1e8).toLocaleString('ko-KR', {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero'
  });
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

  function line(canvas, points, key, signed = false, eok = signed) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const { width, height } = canvas;
    const left = eok ? 76 : 58;
    const right = width - 20;
    const top = 20;
    const bottom = height - 27;
    const values = points.map(row => row.value / (eok ? 1e8 : 1e12));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) * 0.12, signed ? 0.05 : 0.005);
    const high = max + padding;
    const low = signed ? min - padding : Math.max(0, min - padding);
    const days = points.map(row => Date.parse(row.date));
    const duration = days.at(-1) - days[0];
    const x = (i) => duration > 0 ? left + (right - left) * (days[i] - days[0]) / duration : (left + right) / 2;
    const y = (value) => bottom - (bottom - top) * (value - low) / (high - low);
    ctx.font = '16px system-ui';
    for (const part of [0, 0.5, 1]) {
      const value = low + (high - low) * part;
      ctx.beginPath();
      ctx.strokeStyle = '#d9e1ed';
      ctx.lineWidth = 1;
      ctx.moveTo(left, y(value));
      ctx.lineTo(right, y(value));
      ctx.stroke();
      ctx.fillStyle = '#49586f';
      ctx.fillText(value.toFixed(eok && Math.max(Math.abs(low), Math.abs(high)) < 1 ? 4 : 2), 9, y(value) + 4, left - 14);
    }
    if (signed) {
      ctx.beginPath();
      ctx.strokeStyle = '#8592a5';
      ctx.moveTo(left, y(0));
      ctx.lineTo(right, y(0));
      ctx.stroke();
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

  function drawChart(points, key, title, signed = false, eok = signed) {
    const canvas = document.createElement('canvas');
    canvas.width = 520;
    canvas.height = 170;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', title);
    line(canvas, points, key, signed, eok);
    return canvas;
  }

  function detailTable(title, headings, rows) {
    const details = element('details');
    details.append(element('summary', title));
    const table = element('table', null, 'cfd-value-table');
    const head = element('tr');
    for (const value of headings) {
      const heading = element('th', value);
      heading.scope = 'col';
      head.append(heading);
    }
    table.createTHead().append(head);
    const body = table.createTBody();
    for (const row of rows) {
      body.insertRow().append(...row.map(value => element('td', value)));
    }
    details.append(table);
    return details;
  }

  function card(key, list, field, state) {
    if (!Array.isArray(list) || !list.length || !list.every((row, i) =>
      datePattern.test(row?.date) && Number.isSafeInteger(row[field]) && row[field] >= 0 &&
      (i === 0 || list[i - 1].date < row.date))) return null;
    const points = list.slice(-90).map(row => ({ date: row.date, value: row[field] }));
    const latest = points.at(-1);
    const article = element('article', null, 'cfd-chart-card');
    article.append(element('h4', labels[key]), element('p', `${trillions(latest.value)}조 원`, 'cfd-value'),
      element('p', `기준일 ${latest.date}${state === 'stale' ? ' · 지난 자료' : state === 'ready' ? '' : ' · 갱신 상태 확인 불가'} · 최근 ${points.length}개 관측`, 'cfd-caption'),
      drawChart(points, key, `${labels[key]} 잔고 추이, 단위 조 원`),
      detailTable('날짜별 잔고', ['기준일', '잔고 (조 원)'],
        points.slice().reverse().map(point => [point.date, trillions(point.value)])));
    return article;
  }

  function group(title, contents, keys, field, state) {
    const cards = keys.map((key) => card(key, contents?.[key], field, state)).filter(Boolean);
    if (!cards.length) return false;
    const grid = element('div', null, 'cfd-chart-grid');
    grid.append(...cards);
    charts.append(element('h3', title), grid);
    return true;
  }

  function twoYearWindow(rows) {
    if (!Array.isArray(rows) || rows.length < 2 || rows.length > 2000 || !rows.every((point, i) =>
      validDate(point?.date) &&
      (i === 0 || rows[i - 1].date < point.date))) return null;
    const end = rows.at(-1).date;
    const year = String(Number(end.slice(0, 4)) - 2).padStart(4, '0');
    const anniversary = `${year}${end.endsWith('-02-29') ? '-02-28' : end.slice(4)}`;
    const start = rows.findLastIndex(row => row.date <= anniversary);
    if (start < 0 || Date.parse(anniversary) - Date.parse(rows[start].date) > dayMs * 14) return null;
    const window = rows.slice(start);
    if (window.some((row, i) => i && Date.parse(row.date) - Date.parse(window[i - 1].date) > dayMs * 14)) return null;
    return window;
  }

  function validDate(value) {
    if (!datePattern.test(value)) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value;
  }

  function validPoints(rows, scope) {
    if (!Array.isArray(rows) || rows.length > 2000) return [];
    const [buy, sell, total] = scope.fields;
    const result = [];
    for (const row of rows) {
      if (!validDate(row?.date) || !scope.fields.every(field => Number.isSafeInteger(row[field]) && row[field] >= 0)
          || !Number.isSafeInteger(row[buy] + row[sell]) || row[buy] + row[sell] !== row[total]) continue;
      const fields = Object.values(scopes).flatMap(item => item.fields);
      if (!fields.every(field => Number.isSafeInteger(row[field]) && row[field] >= 0)
          || Object.values(scopes).some(item => row[item.fields[0]] + row[item.fields[1]] !== row[item.fields[2]])
          || row.kospi_buy_krw + row.kosdaq_buy_krw > row.buy_krw
          || row.kospi_sell_krw + row.kosdaq_sell_krw > row.sell_krw) continue;
      if (result.length && result.at(-1).date >= row.date) return [];
      result.push(row);
    }
    return result;
  }

  function balanceCard(definition, points, scope, info) {
    const article = element('article', null, 'cfd-chart-card');
    const latest = points.at(-1);
    const state = info?.frees_is_series_status?.[definition] || info?.frees_is_status;
    const freshness = state === 'stale' ? ' · 지난 자료' : state === 'ready' ? '' : ' · 갱신 상태 확인 불가';
    article.append(element('h4', labels[definition]),
      element('p', `기준일 ${latest.date}${freshness} · ${points[0].date} ~ ${latest.date} · ${points.length}개 관측`, 'cfd-caption'));
    for (const [field, label, color] of [[scope.fields[0], '매수', 'buy'], [scope.fields[1], '매도', 'sell']]) {
      const figure = element('figure', null, 'cfd-side-figure');
      figure.append(element('figcaption', `${label} ${balanceSummary(latest[field])}`, 'cfd-value'),
        drawChart(points.map(row => ({ date: row.date, value: row[field] })), color,
          `${scope.label} ${labels[definition]} ${label} 명목잔고, 단위 억 원`, false, true));
      article.append(figure);
    }
    article.append(detailTable('날짜별 잔고', ['기준일', '매수', '매도'],
      points.slice().reverse().map(row => [row.date, amount(row[scope.fields[0]]), amount(row[scope.fields[1]])])));
    return article;
  }

  function balances(series, info) {
    if (!['with_margin', 'without_margin'].some(key => validPoints(series?.[key], scopes.all).length)) return null;
    const grid = element('div', null, 'cfd-chart-grid');
    const picker = element('label', '시장 ', 'cfd-scope');
    const input = element('select');
    for (const [key, scope] of Object.entries(scopes)) {
      const option = element('option', scope.label);
      option.value = key;
      input.append(option);
    }
    function update() {
      const scope = scopes[input.value] || scopes.all;
      const cards = ['with_margin', 'without_margin'].map(key => {
        const points = validPoints(series?.[key], scope);
        return points.length ? balanceCard(key, points, scope, info) : null;
      }).filter(Boolean);
      grid.replaceChildren(...(cards.length ? cards : [element('p', '확인된 매수·매도 잔고가 없습니다.')]));
    }
    input.addEventListener('change', update);
    picker.append(input);
    charts.append(element('h3', '매수·매도 명목잔고'),
      element('p', '차트 단위는 억 원입니다. 매수·매도 잔고는 각각의 세로축을 사용합니다.', 'cfd-caption'), picker, grid);
    update();
    return input;
  }

  function sideCard(definition, window, scope) {
    const [buyField, sellField, totalField] = scope.fields;
    if (validPoints(window, scope).length !== window.length) return null;
    const first = window[0];
    const last = window.at(-1);
    const article = element('article', null, 'cfd-chart-card');
    article.append(element('h4', labels[definition]),
      element('p', `${first.date} ~ ${last.date} · ${window.length}개 관측`, 'cfd-caption'));
    for (const [field, label, color] of [[buyField, '매수', 'buy'], [sellField, '매도', 'sell']]) {
      const figure = element('figure', null, 'cfd-side-figure');
      figure.append(element('figcaption', `${label} ${changeText(last[field] - first[field])}억 원`, 'cfd-value'),
        drawChart(window.map(row => ({ date: row.date, value: row[field] - first[field] })),
          color, `${scope.label} ${labels[definition]} ${label} 잔고 누적 변화, 단위 억 원`, true));
      article.append(figure);
    }
    article.append(detailTable('날짜별 누적 변화', ['기준일', '매수 (억 원)', '매도 (억 원)'],
      window.slice().reverse().map(row => [row.date,
        changeText(row[buyField] - first[buyField]), changeText(row[sellField] - first[sellField])])));
    return article;
  }

  function cumulativeGroup(series, input) {
    const windows = ['with_margin', 'without_margin']
      .map(key => [key, twoYearWindow(series?.[key])]).filter(([, window]) => window);
    const cardsFor = (scope) => windows.map(([key, window]) => sideCard(key, window, scope)).filter(Boolean);
    const initial = cardsFor(scopes.all);
    if (!initial.length) return;
    const grid = element('div', null, 'cfd-chart-grid');
    input.addEventListener('change', () => {
      const cards = cardsFor(scopes[input.value] || scopes.all);
      grid.replaceChildren(...(cards.length ? cards : [element('p', '이 시장의 2년 잔고 이력이 아직 없습니다.')]));
    });
    grid.append(...initial);
    charts.append(element('h3', '2년 매수·매도 명목잔고 누적 변화'),
      element('p', '시작일과 비교한 잔고 증감입니다. 매수·매도 차트의 세로축 범위는 다릅니다.', 'cfd-caption'), grid);
  }

  async function start() {
    const [statusResult, domestic, allReported] = await Promise.allSettled([
      read('cfd_status'), read('cfd_market_history'), read('cfd_frees_is_history')
    ]);
    const info = statusResult.value?.schema_version === 'cfd_status_v1' ? statusResult.value : null;
    if (info) {
      const date = new Date(info.checked_at);
      const checked = Number.isNaN(date.valueOf()) ? '' : ` · 조회 ${dateTime.format(date)} KST`;
      const latest = validDate(info.latest?.observation_date) && validDate(info.latest?.posted_date)
        ? ` · 국내 공시 게시판 기준일 ${info.latest.observation_date} · 게시일 ${info.latest.posted_date}` : '';
      const freeStale = info.frees_is_status === 'stale' ? ' · FreeSIS 지난 자료' : '';
      status.textContent = `${states[info.status] || '공시 상태 확인 중'}${latest}${checked}${freeStale}`;
    } else {
      status.textContent = '공시 조회 상태를 확인하지 못했습니다. 아래 잔고의 기준일을 확인하세요.';
    }
    let displayed = false;
    if (allReported.value?.schema_version === 'cfd_frees_is_history_v1') {
      const series = allReported.value.series;
      const input = balances(series, info);
      if (input) {
        displayed = true;
        cumulativeGroup(series, input);
      } else {
        displayed = group('FreeSIS 전체 명목잔고 · 해외 포함', series,
          ['with_margin', 'without_margin'], 'total_krw', info?.frees_is_status) || displayed;
      }
    }
    if (domestic.value?.schema_version === 'cfd_market_history_v1') {
      displayed = group('국내 공시 종목 잔고 합계 · 매수·매도 구분 없음', domestic.value.markets,
        ['KOSPI', 'KOSDAQ'], 'value_krw', info?.data_status) || displayed;
    }
    if (!displayed) charts.append(element('p', '확인된 잔고 자료가 없습니다.', 'cfd-caption'));
  }
  start();
})();
