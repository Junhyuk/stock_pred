(() => {
  'use strict';
  const host = document.getElementById('benchmark-forecast-cards');
  const status = document.getElementById('benchmark-forecast-status');
  const fallback = document.getElementById('benchmark-static-fallback');
  if (!host || !status) return;
  const markets = [['KOSPI200', '코스피 200'], ['KOSDAQ150', '코스닥 150']];
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const positive = value => number(value) && value > 0;
  const probability = value => number(value) && value >= 0 && value <= 1;
  const count = value => Number.isInteger(value) && value >= 0 ? value.toLocaleString('ko-KR') + '건' : '미확인';
  const percent = value => probability(value) ? (value * 100).toFixed(1) + '%' : '미확인';
  const level = value => positive(value) ? value.toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '미확인';
  const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && !Number.isNaN(Date.parse(value)) ? value : null;
  const kst = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });
  function time(value) {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) return '미확인';
    const stamp = new Date(value);
    return Number.isNaN(stamp.valueOf()) ? '미확인' : kst.format(stamp) + ' KST';
  }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function source(value) {
    const label = typeof value === 'string' && value.trim() ? value : value?.name || value?.id || '미확인';
    return { krx_openapi: 'KRX OpenAPI', fsc_openapi: '공공데이터포털 금융위원회' }[label] || label;
  }
  function disclosure(summary, body, className) {
    const details = element('details', undefined, className);
    details.append(element('summary', summary), body);
    return details;
  }
  function reason(row) {
    const messages = {
      credential_missing: '공식 지수 데이터 연결이 설정되지 않았습니다.',
      authentication_failed: '공식 지수 데이터 연결 인증을 확인해야 합니다.',
      rate_limited: '데이터 제공처의 요청 한도에 도달했습니다.',
      network_error: '데이터 제공처에 연결하지 못했습니다.',
      invalid_response: '응답에서 정확한 지수 값을 확인하지 못했습니다.',
      no_exact_index_history: '정확한 지수의 과거 종가가 없습니다.',
      no_exact_index_rows: '정확한 지수의 종가를 확인하지 못했습니다.',
      derived_publication_unavailable: '공개 가능한 예측 자료가 없습니다.',
      outside_premarket_window: '장전 발행 시간이 지나 새 예측을 발행하지 않았습니다.',
      latest_close_unavailable: '기준 거래일의 종가를 확인하지 못했습니다.',
      benchmark_processing_error: '지수 예측 자료를 확인하지 못했습니다.',
      insufficient_history: '예측에 필요한 과거 자료가 부족합니다.',
      insufficient_training_history: '학습에 필요한 과거 자료가 부족합니다.',
    };
    if (messages[row?.reason]) return messages[row.reason];
    return typeof row?.reason === 'string' && /[가-힣]/.test(row.reason)
      ? row.reason : '공개 가능한 예측 자료가 없습니다.';
  }
  function evidence(row, forward, letter) {
    const data = row.evaluation || {};
    const terminal = data.terminal || data;
    const recent = data.windows?.last_30;
    const isC = letter === 'C';
    const metric = isC ? 'class_accuracy' : 'hit_rate';
    const body = element('div');
    const add = text => body.append(element('p', text));
    const counts = [
      ['과거 재구성', data.retrospective_n],
      [isC ? '최종 검증 조건 충족 표본' : '최종 검증', data.terminal_n ?? terminal.n],
      ['실제 발행', (isC ? forward?.class_n : forward?.n) ?? data.live_n],
    ].filter(([, n]) => Number.isInteger(n) && n >= 0).map(([label, n]) => `${label} ${count(n)}`);
    if (counts.length) add(counts.join(' · '));
    if (probability(terminal[metric])) add(`최종 검증 ${isC ? '3분류' : '방향'} 적중률 ${percent(terminal[metric])}`);
    if (probability(recent?.[metric])) {
      add(`최근 30거래일 ${isC ? '조건 충족일 3분류' : '방향'} 적중률 ${percent(recent[metric])}${Number.isInteger(recent.n) ? ` (${count(recent.n)})` : ''}${probability(terminal[metric]) ? ` · 최종 검증 ${percent(terminal[metric])}` : ''}`);
    }
    if (!isC) {
      const baselineNames = { majority: '다수 방향', previous_direction: '직전 방향', simple_trend: '단순 추세', logistic: '로지스틱' };
      if (probability(terminal.baseline_hit_rate)) add(`기준선${baselineNames[data.direction_baseline] ? ` (${baselineNames[data.direction_baseline]})` : ''} 적중률 ${percent(terminal.baseline_hit_rate)}`);
      const ci = data.hit_rate_gain_ci95;
      if (ci && number(ci.low) && number(ci.high) && ci.low <= ci.high) {
        const pp = value => `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value * 100).toFixed(1)}`;
        add(`기준선 대비 적중률 개선 95% 구간 ${pp(ci.low)}~${pp(ci.high)}%p`);
      }
      const recall = [['상승일 재현율', terminal.up_recall], ['하락일 재현율', terminal.down_recall]]
        .filter(([, value]) => probability(value)).map(([label, value]) => `${label} ${percent(value)}`);
      if (recall.length) add(recall.join(' · '));
    }
    const errors = [];
    if (number(terminal.close_mae) && terminal.close_mae >= 0) errors.push(`종가 MAE ${terminal.close_mae.toFixed(2)}pt`);
    else if (number(terminal.mae_return) && terminal.mae_return >= 0) errors.push(`종가 수익률 MAE ${(terminal.mae_return * 100).toFixed(2)}%p`);
    if (probability(terminal.coverage80)) errors.push(`80% 예측 구간 실제 포함률 ${percent(terminal.coverage80)}`);
    if (errors.length) add(errors.join(' · '));
    if (isC && terminal.class_counts) {
      const classes = [['급락', 'down_extreme'], ['중립', 'normal'], ['급등', 'up_extreme']]
        .filter(([, key]) => Number.isInteger(terminal.class_counts[key]) && terminal.class_counts[key] >= 0)
        .map(([label, key]) => `${label} ${count(terminal.class_counts[key])}`);
      if (classes.length) add(`조건 충족 표본의 실제 결과 · ${classes.join(' · ')}`);
    }
    if (isC && terminal.class_recall) {
      const recalls = [['급락', 'down_extreme'], ['중립', 'normal'], ['급등', 'up_extreme']]
        .filter(([, key]) => probability(terminal.class_recall[key]))
        .map(([label, key]) => `${label} 재현율 ${percent(terminal.class_recall[key])}`);
      if (recalls.length) add(recalls.join(' · '));
    }
    const forwardHit = isC ? forward?.class_hit_rate : forward?.hit_rate;
    if (probability(forwardHit)) add(`실제 발행 ${isC ? '3분류' : '방향'} 적중률 ${percent(forwardHit)}`);
    if (number(forward?.close_mae) && forward.close_mae >= 0) add(`실제 발행 종가 MAE ${forward.close_mae.toFixed(2)}pt`);
    if (!body.childElementCount) return null;
    body.append(element('p', '과거 재구성과 최종 검증은 실제 발행 후의 성과가 아닙니다.'));
    return disclosure(isC ? '검증 표본·3분류 성과' : '검증 표본·방향 적중률', body, 'benchmark-evidence');
  }
  function model(letter, row, forward) {
    row = row || {};
    const block = element('section', undefined, `benchmark-model model-${letter.toLowerCase()}`);
    block.dataset.model = letter;
    const heading = element('h4');
    heading.append(element('span', letter, 'model-letter'), document.createTextNode(letter === 'C' ? '급등·중립·급락' : '방향과 종가'));
    block.append(heading);
    const allowed = ['ready', 'research_candidate'].includes(row.status);
    const values = letter === 'C' ? [row.p_down, row.p_normal, row.p_up] : [row.p_up, row.p_down];
    const valid = allowed && values.every(probability) && Math.abs(values.reduce((a, b) => a + b, 0) - 1) < 1e-6;
    if (!valid) {
      block.append(element('p', reason(row), 'benchmark-empty'));
      return block;
    }
    if (row.evaluation?.status === 'unconfirmed_research') {
      block.append(element('p', '연구 예측 · 검증 기준 미충족', 'benchmark-evidence'));
    } else if (row.evaluation?.status === 'accepted_research') {
      block.append(element('p', '연구 예측 · 과거 검증 기준 충족', 'benchmark-evidence'));
    }
    if (row.cutoff || row.issued_at) {
      block.append(element('p', `이 예측의 입력 마감 ${time(row.cutoff)} · 최초 발행 ${time(row.issued_at)}`, 'benchmark-evidence'));
    }
    if (letter === 'C') {
      block.append(element('p', `급락 ${percent(row.p_down)} · 중립 ${percent(row.p_normal)} · 급등 ${percent(row.p_up)}`, 'benchmark-model-values'));
      block.append(element('p', '다음 거래일 수익률: 급락 ≤ −3% · 중립 −3% 초과, +3% 미만 · 급등 ≥ +3%', 'benchmark-evidence'));
    } else {
      const direction = row.p_up === .5 ? '동률' : row.p_up > .5 ? '상승 우세' : '하락 우세';
      block.append(element('p', `방향 ${direction} · 상승 ${percent(row.p_up)} / 하락 ${percent(row.p_down)}`, 'benchmark-model-values benchmark-direction'));
    }
    block.append(element('p', `예상 종가 ${level(row.expected_index)}`, 'benchmark-model-values'));
    const interval = row.interval;
    if (interval && positive(interval.low) && positive(interval.high) && interval.low <= interval.high
        && probability(interval.level) && interval.level > 0) {
      block.append(element('p', `${(interval.level * 100).toFixed(0)}% 예측 구간 ${level(interval.low)}–${level(interval.high)}`, 'benchmark-evidence'));
    }
    const evaluation = evidence(row, forward, letter);
    if (evaluation) block.append(evaluation);
    return block;
  }
  function chart(code, name, row) {
    const data = row.chart;
    const end = date(data?.asof_date);
    const anchor = date(row.asof_date);
    let points = [];
    if (end && anchor && end <= anchor && data?.publish_allowed !== false && source(data.source) !== '미확인' && Array.isArray(data.points)) {
      const since = new Date(end + 'T00:00:00Z');
      since.setUTCFullYear(since.getUTCFullYear() - 1);
      const start = since.toISOString().slice(0, 10);
      const seen = new Set();
      points = data.points.filter(point => date(point?.date) && point.date >= start && point.date <= end && positive(point.close))
        .sort((a, b) => a.date.localeCompare(b.date));
      if (points.some(point => { const duplicate = seen.has(point.date); seen.add(point.date); return duplicate; })) points = [];
    }
    if (points.length < 2) {
      const missing = element('p', '공개 가능한 1년 종가 이력 미확인 · ', 'benchmark-empty');
      const link = element('a', '정적 참고 차트 보기');
      link.href = `#benchmark-${code.toLowerCase()}`;
      link.addEventListener('click', () => { if (fallback) fallback.open = true; });
      missing.append(link);
      return { node: missing, ready: false };
    }
    const figure = element('figure', undefined, 'benchmark-chart');
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    const titleId = `benchmark-chart-${code.toLowerCase()}`;
    svg.setAttribute('viewBox', '0 0 520 180');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-labelledby', titleId);
    const title = document.createElementNS(ns, 'title');
    title.id = titleId;
    title.textContent = `${name} 1년 종가 · ${points[0].date}부터 ${points.at(-1).date} · 마지막 ${level(points.at(-1).close)}`;
    svg.append(title);
    const lows = points.map(point => point.close);
    const min = Math.min(...lows), max = Math.max(...lows), spread = max - min || Math.max(max * .02, 1);
    const bottom = min - spread * .08, top = max + spread * .08;
    const first = Date.parse(points[0].date), last = Date.parse(points.at(-1).date);
    const x = point => 60 + (Date.parse(point.date) - first) / (last - first) * 445;
    const y = value => 145 - (value - bottom) / (top - bottom) * 130;
    function svgNode(tag, attrs, text) {
      const node = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
      if (text !== undefined) node.textContent = text;
      svg.append(node);
    }
    for (const value of [min, max]) {
      svgNode('line', { x1: 60, x2: 505, y1: y(value), y2: y(value), stroke: '#dce3e8' });
      svgNode('text', { x: 53, y: y(value) + 5, 'text-anchor': 'end' }, value.toLocaleString('ko-KR', { maximumFractionDigits: 0 }));
    }
    svgNode('polyline', { points: points.map(point => `${x(point).toFixed(2)},${y(point.close).toFixed(2)}`).join(' '), fill: 'none', stroke: '#165750', 'stroke-width': 2.5 });
    svgNode('circle', { cx: x(points.at(-1)), cy: y(points.at(-1).close), r: 3.5, fill: '#165750' });
    svgNode('text', { x: 60, y: 171 }, points[0].date);
    svgNode('text', { x: 505, y: 171, 'text-anchor': 'end' }, points.at(-1).date);
    figure.append(svg, element('figcaption', `최근 1년 중 공개된 ${points.length}개 종가 · 기준 ${end} · 출처 ${source(data.source)}`));
    const tableWrap = element('div', undefined, 'benchmark-chart-table');
    tableWrap.tabIndex = 0;
    tableWrap.setAttribute('aria-label', `${name} 종가 표 스크롤`);
    const table = element('table');
    const caption = element('caption', `${name} 차트에 사용한 종가`, 'visually-hidden');
    const head = element('thead'), header = element('tr');
    for (const label of ['거래일', '종가']) { const cell = element('th', label); cell.scope = 'col'; header.append(cell); }
    head.append(header);
    const body = element('tbody');
    for (const point of [...points].reverse()) {
      const tr = element('tr');
      tr.append(element('td', point.date), element('td', level(point.close)));
      body.append(tr);
    }
    table.append(caption, head, body);
    tableWrap.append(table);
    figure.append(disclosure('차트 종가 표', tableWrap, 'benchmark-chart-data'));
    return { node: figure, ready: true };
  }
  function render(data) {
    host.replaceChildren();
    let charts = 0;
    for (const [code, name] of markets) {
      const row = data.markets[code] || {};
      const card = element('article', undefined, 'benchmark-card benchmark-forecast-card');
      card.dataset.benchmark = code;
      const title = element('h3', name);
      title.id = `benchmark-forecast-${code.toLowerCase()}`;
      card.setAttribute('aria-labelledby', title.id);
      card.append(title, element('p', `확인 종가 ${level(row.close)}`, 'benchmark-close'));
      card.append(element('p', `종가 기준 ${date(row.asof_date) || '미확인'} → 예측 대상 ${date(row.target_date) || date(data.target_date) || '미확인'}`, 'benchmark-forecast-meta'));
      card.append(element('p', `출처 ${source(row.source)} · 실제 입력 마감 ${time(row.cutoff)}`, 'benchmark-forecast-meta'));
      card.append(element('p', `데이터 상태 ${row.status === 'ready' ? '확인됨' : row.status === 'stale' ? '갱신 지연' : '미확인'}${row.reason ? ' · ' + reason(row) : ''}`, 'benchmark-forecast-meta'));
      const history = chart(code, name, row);
      charts += Number(history.ready);
      card.append(history.node);
      for (const letter of ['A', 'B']) card.append(model(letter, row.models?.[letter], row.forward_validation?.[letter]));
      if (data.c_trigger?.status === 'triggered') {
        card.append(model('C', row.models?.C, row.forward_validation?.C));
        card.append(element('p', 'C 표시 조건: 종합 코스피 또는 코스닥 기준일 수익률의 절댓값 3% 이상', 'benchmark-evidence'));
      }
      host.append(card);
    }
    if (fallback) fallback.open = charts < markets.length;
    const kind = data.issue_kind === 'preliminary' ? '사전 예측' : data.issue_kind === 'final' ? '최종 예측' : '발행 구분 미확인';
    status.textContent = `${kind} · 발행 ${time(data.issued_at)} · 실제 입력 마감 ${time(data.cutoff)} · 연구용`;
    host.setAttribute('aria-busy', 'false');
  }
  fetch('data/benchmark_forecasts.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error('unavailable'); return response.json(); })
    .then(data => {
      if (data?.schema_version !== 1 || !data.markets || typeof data.markets !== 'object' || Array.isArray(data.markets)) throw new Error('schema');
      render(data);
    })
    .catch(() => {
      render({ markets: {} });
      status.textContent = '지수별 예측 자료를 확인하지 못했습니다. 날짜가 표시된 정적 참고 차트를 이용할 수 있습니다.';
    });
})();
