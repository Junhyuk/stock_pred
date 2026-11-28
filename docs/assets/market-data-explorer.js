(() => {
  'use strict';

  const pick = id => document.getElementById(id);
  const section = pick('cross-assets');
  if (!section) return;

  const nodes = Object.fromEntries([
    'status', 'controls', 'model', 'category', 'chart', 'range', 'custom', 'start', 'end', 'panel',
    'chart-title', 'description', 'plot', 'observation', 'date', 'prev', 'next', 'readings', 'model-inputs',
  ].map(key => [key, pick('market-explorer-' + key)]));
  const dayMs = 86400000;
  const colors = [
    { ink: '#0d655e', dash: '' }, { ink: '#72408f', dash: '6 3' }, { ink: '#a35315', dash: '2 3' },
    { ink: '#2253a0', dash: '9 3 2 3' }, { ink: '#953343', dash: '10 4' },
  ];
  const unitNames = {
    percent: '%', index_points: 'pt', index_point: 'pt', points: 'pt', KRW: '원', USD: 'USD',
    thousand_persons: '천 명', persons: '명', million_persons: '백만 명',
  };
  const domestic = ['kospi', 'kosdaq'];
  const american = ['sp500', 'nasdaq', 'sox', 'ewy', 'koru'];
  const horizons = [1, 5, 20];
  const labels = {
    kospi: '코스피', kosdaq: '코스닥', sp500: 'S&P 500', nasdaq: '나스닥 종합', sox: '필라델피아 반도체',
    ewy: 'EWY', koru: 'KORU', usdkrw: '원/달러', dxy: '달러 인덱스', vix: 'VIX',
    kr_1y: '한국 국채 1년', kr_3y: '한국 국채 3년', kr_10y: '한국 국채 10년',
    us_1y: '미국 국채 1년', us_3y: '미국 국채 3년', us_10y: '미국 국채 10년',
    wti: 'WTI', brent: '브렌트유', gold: '금', copper: '구리',
    es_futures: 'S&P 500 선물', nq_futures: '나스닥 100 선물', kospi200_basis: '코스피 200 선물 베이시스',
    kr_inflation: '한국 소비자물가', us_inflation: '미국 소비자물가',
    kr_unemployment: '한국 실업률', us_unemployment: '미국 실업률',
    kr_employment: '한국 고용', us_employment: '미국 고용',
    kr_policy_rate: '한국 기준금리', us_policy_rate: '미국 정책금리 중간값',
    us_policy_rate_lower: '미국 정책금리 하단', us_policy_rate_upper: '미국 정책금리 상단',
  };
  const level = key => ({ id: key, keys: [key], label: labels[key], transform: 'level' });
  const group = (id, label, charts) => ({ id, label, charts });
  const chart = (id, label, keys, transform = 'level', note = '') => ({ id, label, keys, transform, note });
  const groups = [
    group('domestic', '국내 지수', [
      chart('kr-rebased', '코스피·코스닥 비교', domestic, 'rebased'),
      ...domestic.map(level),
      ...horizons.map(days => chart('kr-returns-' + days, days + '거래일 수익률', domestic, 'return' + days)),
      chart('kr-volatility', '20거래일 변동성', domestic, 'volatility20'),
    ]),
    group('us', '미국 지수·ETF', [
      chart('us-rebased', '미국 지수·한국 ETF 비교', american, 'rebased', '미국 현지 거래일 기준입니다.'),
      chart('us-returns-1', '미국 세션 수익률', american, 'return1', '미국 현지 거래일의 종가를 직전 종가와 비교합니다.'),
      ...american.map(level),
    ]),
    group('fx-volatility', '환율·변동성', ['usdkrw', 'dxy', 'vix'].map(level)),
    group('kr-rates', '한국 국채 금리', [
      chart('kr-rates', '한국 국채 1·3·10년', ['kr_1y', 'kr_3y', 'kr_10y']),
      ...['kr_1y', 'kr_3y', 'kr_10y'].map(level),
    ]),
    group('us-rates', '미국 국채 금리', [
      chart('us-rates', '미국 국채 1·3·10년', ['us_1y', 'us_3y', 'us_10y']),
      ...['us_1y', 'us_3y', 'us_10y'].map(level),
    ]),
    group('oil', '국제 원유', [
      chart('oil-comparison', 'WTI·브렌트유 비교', ['wti', 'brent']), ...['wti', 'brent'].map(level),
    ]),
    group('metals', '금속', [
      chart('metals-comparison', '금·구리 비교', ['gold', 'copper'], 'rebased'), ...['gold', 'copper'].map(level),
    ]),
    group('futures', '선물·베이시스', [
      chart('us-futures', '미국 지수 선물 비교', ['es_futures', 'nq_futures'], 'rebased'),
      ...['es_futures', 'nq_futures', 'kospi200_basis'].map(level),
    ]),
    group('economics', '경제지표·정책금리', [
      ...['kr_inflation', 'us_inflation', 'kr_unemployment', 'us_unemployment', 'kr_employment', 'us_employment',
        'kr_policy_rate', 'us_policy_rate', 'us_policy_rate_lower', 'us_policy_rate_upper'].map(level),
    ]),
  ];
  const transforms = {
    return1: '1거래일 수익률', return5: '5거래일 수익률', return20: '20거래일 수익률',
    volatility20: '20거래일 연율화 변동성', volatility20_daily: '20거래일 일간 변동성',
  };
  const stageNames = { premarket: '장전', morning: '장전', postclose: '장후', after_close: '장후', intraday: '장중' };
  const modelStatuses = { not_ready: '예측 미공개', not_triggered: '급변 조건 미충족', unavailable: '예측 확인 불가' };
  const availabilityNames = {
    observed_in_forecast: '예측에 기록된 값', reconstructed: '동결 입력으로 재계산',
    unavailable: '확인 불가', selected_not_ready: '선택된 입력 · 예측 미공개',
  };
  const headNames = { close: '종가', direction: '방향', interval: '범위', probability: '확률', trigger: '급변 조건' };
  const dateLabels = { release_date: '발표일', reference_month: '기준 기간', effective_date: '적용일' };
  const kst = new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  let history = {};
  let models = new Map();
  let chartOptions = [];
  let verifiedModel = null;
  let modelRequest = 0;
  let frame = null;
  let selectedDate = '';
  let plotWidth = 0;

  function element(tag, text, className) {
    const value = document.createElement(tag);
    if (text != null) value.textContent = text;
    if (className) value.className = className;
    return value;
  }

  function svgElement(tag, attributes, text) {
    const value = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, attribute] of Object.entries(attributes)) value.setAttribute(key, attribute);
    if (text != null) value.textContent = text;
    return value;
  }

  function isoDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const date = new Date(value);
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
  }

  function parseHistory(series) {
    return Object.fromEntries(Object.entries(series || {}).map(([key, source]) => {
      const points = new Map();
      for (const point of source?.points || []) {
        if (Array.isArray(point) && isoDate(point[0]) && Number.isFinite(point[1])) points.set(point[0], point[1]);
      }
      return [key, { ...source, points: Array.from(points).sort((left, right) => left[0].localeCompare(right[0])) }];
    }));
  }

  function format(value, unit, signed = false, precision = 4) {
    if (!Number.isFinite(value)) return '—';
    const amount = new Intl.NumberFormat('ko-KR', {
      maximumFractionDigits: precision,
      signDisplay: signed ? 'exceptZero' : 'auto',
    }).format(value);
    const name = unitNames[unit] || unit || '';
    return amount + (name === '%' ? '%' : name ? ' ' + name : '');
  }

  function sourceLink(url, text) {
    try {
      const address = new URL(url);
      if (address.protocol !== 'https:' && address.protocol !== 'http:') return element('span', text);
      const link = element('a', text);
      link.href = address.href;
      return link;
    } catch {
      return element('span', text);
    }
  }

  function setOptions(select, entries, selected) {
    select.replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
    if (entries.some(([value]) => value === selected)) select.value = selected;
  }

  function transformPoints(points, transform) {
    if (!Object.hasOwn(transforms, transform)) return points.map(([date, value]) => ({ date, value, actual: value }));
    const days = Number(transform.match(/\d+/)[0]);
    if (transform.startsWith('return')) {
      return points.slice(days).flatMap(([date, actual], index) => {
        const prior = points[index][1];
        return actual > 0 && prior > 0 ? [{ date, actual, value: (actual / prior - 1) * 100 }] : [];
      });
    }
    const returns = points.map(([, close], index) => {
      const previous = points[index - 1]?.[1];
      return previous > 0 && close > 0 ? close / previous - 1 : null;
    });
    return points.slice(days).flatMap(([date, actual], index) => {
      const window = returns.slice(index + 1, index + days + 1);
      if (window.includes(null)) return [];
      const mean = window.reduce((sum, value) => sum + value, 0) / days;
      const variance = window.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (days - 1);
      const scale = transform === 'volatility20' ? 252 : 1;
      return [{ date, actual, value: 100 * Math.sqrt(scale * variance) }];
    });
  }

  function periodStart(end, months) {
    const target = new Date(end);
    const day = target.getUTCDate();
    target.setUTCDate(1);
    target.setUTCMonth(target.getUTCMonth() - months);
    const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
    target.setUTCDate(Math.min(day, lastDay));
    return target.toISOString().slice(0, 10);
  }

  function makeFrame(config) {
    const series = config.keys.map((key, index) => {
      const source = history[key] || { points: [] };
      return {
        key, source, label: source.label || labels[key] || key, style: colors[index % colors.length],
        points: transformPoints(source.points, config.transform),
      };
    });
    const allDates = Array.from(new Set(series.flatMap(entry => entry.points.map(point => point.date)))).sort();
    const first = allDates[0];
    const last = allDates.at(-1);
    for (const node of [nodes.start, nodes.end]) {
      node.min = first || '';
      node.max = last || '';
    }
    const months = { '1m': 1, '3m': 3, '6m': 6, '1y': 12 }[nodes.range.value];
    const custom = nodes.range.value === 'custom';
    const start = custom && nodes.start.value ? nodes.start.value : last && months ? periodStart(last, months) : first;
    const end = custom && nodes.end.value ? nodes.end.value : last;
    const dateError = Boolean(start && end && start > end);
    for (const entry of series) {
      entry.visible = new Map(entry.points.filter(point => !dateError && point.date >= start && point.date <= end)
        .map(point => [point.date, point]));
    }
    const available = series.filter(entry => entry.visible.size);
    let baseline;
    if (config.transform === 'rebased') {
      baseline = Array.from(available[0]?.visible.keys() || []).find(date =>
        available.every(entry => (entry.visible.get(date)?.value ?? 0) > 0));
      for (const entry of series) {
        const initial = entry.visible.get(baseline)?.value;
        entry.visible = new Map(Array.from(entry.visible).flatMap(([date, point]) =>
          initial && date >= baseline ? [[date, { ...point, value: 100 * point.value / initial }]] : []));
      }
    }
    const visibleDates = Array.from(new Set(series.flatMap(entry => Array.from(entry.visible.keys())))).sort();
    const transform = config.transform;
    const unit = transform === 'rebased' ? '' : Object.hasOwn(transforms, transform) ? '%' : series.find(entry => entry.visible.size)?.source.unit;
    return { config, series, visibleDates, baseline, unit, first, last, start, end, dateError, requiresBaseline: available.length > 0 && !baseline && transform === 'rebased' };
  }

  function description(current) {
    const { config, baseline, unit, series } = current;
    const notes = [];
    if (config.transform === 'rebased' && baseline) notes.push('공통 관측일 ' + baseline + '의 실제 값을 100으로 환산했습니다.');
    if (config.transform.startsWith('return')) notes.push('각 지표의 ' + transforms[config.transform] + ' · 단위 %');
    if (config.transform === 'volatility20') notes.push('최근 20개 일간 수익률의 표본 표준편차 × √252 · 단위 %');
    if (config.transform === 'volatility20_daily') notes.push('최근 20개 일간 수익률의 표본 표준편차 · 단위 %');
    if (config.transform === 'level' && unit) notes.push('단위 ' + (unitNames[unit] || unit));
    const basis = dateLabels[series[0]?.source.date_basis];
    if (basis) notes.push('가로축 ' + basis);
    if (config.note) notes.push(config.note);
    return notes.join(' ');
  }

  function emptyPlot(message) {
    nodes.plot.replaceChildren(element('p', message, 'market-explorer-empty'));
  }

  function renderPlot() {
    if (!frame?.visibleDates.length) return;
    const { series, visibleDates, unit, config } = frame;
    const width = Math.max(250, Math.floor(nodes.plot.getBoundingClientRect().width));
    plotWidth = width;
    const height = width < 500 ? 220 : 300;
    const values = series.flatMap(entry => Array.from(entry.visible.values(), point => point.value));
    const crossesZero = Math.min(...values) < 0 && Math.max(...values) > 0;
    const showZero = config.transform.startsWith('return') || config.keys.includes('kospi200_basis') || crossesZero;
    const centerline = config.transform === 'rebased' ? 100 : showZero ? 0 : null;
    if (centerline != null) values.push(centerline);
    const low = Math.min(...values);
    const high = Math.max(...values);
    const padding = (high - low) * .07 || Math.max(Math.abs(high) * .02, .02);
    const lower = low - padding;
    const upper = high + padding;
    const tickCount = 4;
    const tickPrecision = Math.max(0, Math.min(5, 1 - Math.floor(Math.log10((upper - lower) / tickCount))));
    const gridValues = Array.from({ length: tickCount + 1 }, (_, index) => lower + (upper - lower) * index / tickCount);
    const y0 = 11;
    const y1 = height - 29;
    const spacing = 17 * (upper - lower) / (y1 - y0);
    const yTicks = centerline == null ? gridValues :
      [...gridValues.filter(tick => Math.abs(tick - centerline) > spacing), centerline];
    const x0 = Math.max(53, 10 + Math.max(...yTicks.map(value => format(value, '', false, tickPrecision).length)) * 7);
    const x1 = width - 17;
    const firstDay = Date.parse(visibleDates[0]);
    const lastDay = Date.parse(visibleDates.at(-1));
    const x = date => firstDay === lastDay ? (x0 + x1) / 2 : x0 + (Date.parse(date) - firstDay) * (x1 - x0) / (lastDay - firstDay);
    const y = value => y1 - (value - lower) * (y1 - y0) / (upper - lower);
    const svg = svgElement('svg', {
      viewBox: '0 0 ' + width + ' ' + height, role: 'img',
      'aria-labelledby': 'market-explorer-svg-title', 'aria-describedby': 'market-explorer-svg-desc',
    });
    svg.append(
      svgElement('title', { id: 'market-explorer-svg-title' }, config.label),
      svgElement('desc', { id: 'market-explorer-svg-desc' },
        visibleDates[0] + ' ~ ' + visibleDates.at(-1) + ' · ' + (unitNames[unit] || unit || '공통 시작일 = 100') + ' · 아래 관측일에서 날짜별 실제 값을 확인할 수 있습니다.'),
    );
    for (const tick of yTicks) {
      const lineClass = tick === centerline ? 'market-explorer-centerline' : 'market-explorer-grid';
      svg.append(
        svgElement('line', { x1: x0, y1: y(tick), x2: x1, y2: y(tick), class: lineClass }),
        svgElement('text', { x: x0 - 7, y: y(tick) + 4, 'text-anchor': 'end' }, format(tick, '', false, tickPrecision)),
      );
    }
    const dateMarkers = firstDay === lastDay ? [visibleDates[0]] :
      width < 520 ? [visibleDates[0], visibleDates.at(-1)] :
        [visibleDates[0], new Date(firstDay + Math.round((lastDay - firstDay) / dayMs / 2) * dayMs).toISOString().slice(0, 10), visibleDates.at(-1)];
    dateMarkers.forEach((date, index) => svg.append(svgElement('text', {
      x: x(date), y: height - 8, 'text-anchor': dateMarkers.length === 1 || index === 1 && dateMarkers.length === 3 ? 'middle' : index === 0 ? 'start' : 'end',
    }, series[0]?.source.date_basis === 'reference_month' ? date.slice(0, 7) : date)));
    svg.append(svgElement('line', { x1: x(selectedDate), y1: y0, x2: x(selectedDate), y2: y1, class: 'market-explorer-cursor' }));
    for (const entry of series) {
      const path = Array.from(entry.visible.values()).map(({ date, value }, index) =>
        (index === 0 ? 'M' : entry.source.date_basis === 'effective_date' ? 'H' : 'L') +
          (entry.source.date_basis === 'effective_date' && index > 0 ? x(date) + 'V' + y(value) : x(date) + ' ' + y(value))).join(' ');
      if (path) svg.append(svgElement('path', {
        d: path, fill: 'none', stroke: entry.style.ink, 'stroke-dasharray': entry.style.dash, 'stroke-width': 2,
      }));
      const point = entry.visible.get(selectedDate) || (entry.visible.size === 1 ? entry.visible.values().next().value : null);
      if (point) svg.append(svgElement('circle', {
        cx: x(point.date), cy: y(point.value), r: point.date === selectedDate ? 4 : 3,
        fill: entry.style.ink, stroke: 'white', 'stroke-width': 1,
      }));
    }
    svg.addEventListener('click', event => {
      if (visibleDates.length < 2) return;
      const bounds = svg.getBoundingClientRect();
      const pressed = (event.clientX - bounds.left) * width / bounds.width;
      const nearest = visibleDates.reduce((closest, date) =>
        Math.abs(x(date) - pressed) < Math.abs(x(closest) - pressed) ? date : closest);
      chooseDate(nearest);
    });
    nodes.plot.replaceChildren(svg);
  }

  function renderReadings() {
    const transformed = frame.config.transform !== 'level';
    nodes.readings.replaceChildren(...frame.series.map(entry => {
      const item = element('li', null, 'market-explorer-reading');
      const label = element('span', entry.label, 'market-explorer-series');
      const swatch = element('span', null, 'market-explorer-swatch');
      swatch.setAttribute('aria-hidden', 'true');
      swatch.style.color = entry.style.ink;
      if (entry.style.dash) swatch.dataset.dashed = 'true';
      label.prepend(swatch);
      item.append(label);
      const point = entry.visible.get(selectedDate);
      if (point) {
        item.append(element('strong', format(point.actual, entry.source.unit), 'market-explorer-value'));
        if (transformed) {
          const value = format(point.value, frame.unit, frame.config.transform.startsWith('return'), 2);
          const label = frame.config.transform === 'rebased' ? '환산값' : transforms[frame.config.transform];
          item.append(element('span', label + ' ' + value, 'market-explorer-reading-note'));
        }
        const period = entry.source.reference_periods?.[point.date];
        const release = entry.source.release_dates?.[point.date];
        const dateLabel = dateLabels[entry.source.date_basis] || '기준일';
        item.append(element('span', dateLabel + ' ' + (dateLabel === '기준 기간' ? period || point.date.slice(0, 7) : point.date), 'market-explorer-reading-note'));
        if (period && dateLabel !== '기준 기간') item.append(element('span', '기준 기간 ' + period, 'market-explorer-reading-note'));
        if (release && dateLabel !== '발표일') item.append(element('span', '발표일 ' + release, 'market-explorer-reading-note'));
        if (!release && entry.source.date_basis === 'reference_month') item.append(element('span', '발표일 미확인', 'market-explorer-reading-note'));
      } else {
        item.append(element('span', '선택일 관측 없음', 'market-explorer-reading-note'));
        const latest = entry.points.at(-1);
        if (latest) item.append(element('span', '이력의 마지막 관측 ' + latest.date, 'market-explorer-reading-note'));
      }
      if (entry.source.source) {
        item.append(sourceLink(entry.source.release_urls?.[selectedDate] || entry.source.source_url, entry.source.source));
      }
      if (entry.source.note) item.append(element('span', entry.source.note, 'market-explorer-reading-note'));
      return item;
    }));
  }

  function chooseDate(date) {
    if (!frame?.visibleDates.includes(date)) return;
    selectedDate = date;
    nodes.date.value = date;
    nodes.prev.disabled = date === frame.visibleDates[0];
    nodes.next.disabled = date === frame.visibleDates.at(-1);
    renderPlot();
    renderReadings();
  }

  function renderChart(preferredDate = selectedDate) {
    const config = chartOptions.find(option => option.id === nodes.chart.value);
    nodes.custom.hidden = nodes.range.value !== 'custom';
    nodes.panel.hidden = !config;
    if (!config) {
      frame = null;
      return;
    }
    frame = makeFrame(config);
    nodes['chart-title'].textContent = config.label;
    nodes.description.textContent = description(frame);
    const basis = frame.series[0]?.source.date_basis;
    nodes.observation.querySelector('label').textContent = dateLabels[basis] || '관측일';
    nodes.observation.hidden = !frame.visibleDates.length;
    if (!frame.visibleDates.length) {
      emptyPlot(frame.dateError ? '시작일은 종료일보다 늦을 수 없습니다.' :
        frame.requiresBaseline ? '선택 기간에 함께 관측된 시작일이 없습니다.' : '선택 기간에 표시할 이력이 없습니다.');
      selectedDate = '';
      renderReadings();
      return;
    }
    setOptions(nodes.date, [...frame.visibleDates].reverse().map(date => [date, basis === 'reference_month' ? date.slice(0, 7) : date]), preferredDate);
    chooseDate(nodes.date.value);
  }

  function modelChart(row) {
    if (!row.chart_series_key) return null;
    const key = row.chart_series_key.replace(/^us_(sp500|nasdaq|sox|ewy|koru)$/, '$1');
    const transform = row.chart_transform;
    if (!history[key]?.points.length || (transform !== 'level' && !Object.hasOwn(transforms, transform))) return null;
    const instrument = history[key]?.label || labels[key] || key;
    const measure = transform === 'level' ? instrument : instrument + ' ' + transforms[transform];
    return {
      ...chart(key + ':' + transform, measure, [key], transform, '참고 시장 이력입니다. 이 실행의 입력 기록은 아래 표에서 확인할 수 있습니다.'),
      inputDate: row.date,
    };
  }

  async function checkCurrentRun(model) {
    const sources = {
      A: ['reliable_index_forecast', 'reliable_index_forecast_v1'],
      B: ['model_b_forecast', 'model_b_forecast_v1'],
      C: ['model_c_forecast', 'model_c_forecast_v1'],
    };
    try {
      const current = await load(...sources[model.key]);
      const row = current.markets?.find(entry => entry.market === model.market);
      let matches;
      if (model.key === 'A') {
        const currentId = row?.forecast_id || row?.forecast?.forecast_id;
        matches = Boolean(model.forecast_id && model.forecast_id === currentId);
      } else {
        const sameDates = ['asof_date', 'target_date', 'prediction_cutoff'].every(key =>
          model[key] === (model.carried_forward ? row?.[key] : row?.[key] ?? current[key]));
        matches = model.carried_forward ? row?.carried_forward === true && sameDates :
          !row?.carried_forward && Boolean(model.publication_id) && model.publication_id === current.publication_id && sameDates;
        if (model.key === 'C' && !row) {
          matches = matches && model.status === current.status && Number.isFinite(current.trigger_returns?.[model.market]);
        }
      }
      return matches ? 'matched' : 'stale';
    } catch {
      return 'unavailable';
    }
  }

  function renderModel(model, verification) {
    const container = nodes['model-inputs'];
    container.hidden = !model;
    container.replaceChildren();
    if (!model) return;
    const heading = element('h3', '예측 입력 · ' + (model.label || '모델 ' + model.key) + ' · ' + model.market);
    const cutoff = new Date(model.prediction_cutoff || '');
    const dates = [
      model.target_date && '대상 거래일 ' + model.target_date,
      model.asof_date && '기준일 ' + model.asof_date,
      model.issue_stage && (stageNames[model.issue_stage] || model.issue_stage),
      Number.isFinite(cutoff.getTime()) && '입력 마감 ' + kst.format(cutoff) + ' KST',
      model.carried_forward && '이전 예측 유지', modelStatuses[model.status],
    ].filter(Boolean);
    container.append(heading);
    if (dates.length) container.append(element('p', dates.join(' · '), 'market-explorer-meta'));
    if (model.reason) container.append(element('p', model.reason, 'market-explorer-meta'));
    if (verification !== 'matched') {
      const messages = {
        pending: '현재 게시된 예측과 대조 중입니다.',
        stale: '현재 게시된 예측과 다른 실행의 입력 기록입니다.',
        unavailable: '현재 게시된 예측과 일치하는지 확인할 수 없습니다.',
      };
      const freshness = element('p', messages[verification], 'market-explorer-run-status');
      freshness.setAttribute('role', 'status');
      container.append(freshness);
      return;
    }
    if (!model.rows?.length) {
      container.append(element('p', '이 예측에 연결된 입력 기록이 없습니다.', 'market-explorer-empty'));
      return;
    }
    const table = element('table');
    const head = table.createTHead().insertRow();
    for (const label of ['입력', '기준일', '값', '기록 상태']) {
      const cell = element('th', label);
      cell.scope = 'col';
      head.append(cell);
    }
    const body = table.createTBody();
    for (const row of model.rows) {
      const line = body.insertRow();
      const label = element('th');
      label.scope = 'row';
      const choice = modelChart(row);
      if (choice) {
        const button = element('button', row.label || row.key, 'market-explorer-model-link');
        button.type = 'button';
        button.addEventListener('click', () => {
          nodes.chart.value = choice.id;
          renderChart(row.date);
          nodes.chart.focus();
        });
        label.append(button);
      } else {
        label.textContent = row.label || row.key;
      }
      line.append(label);
      line.insertCell().textContent = row.date || '—';
      const amount = line.insertCell();
      if (!Number.isFinite(row.value)) {
        amount.textContent = '—';
      } else if (row.unit === 'ratio') {
        amount.append(element('span', format(row.value * 100, '%', true)),
          element('span', '원본 비율 ' + String(row.value), 'market-explorer-reading-note'));
      } else {
        const unit = row.unit === 'index' ? 'pt' : row.unit;
        amount.textContent = String(row.value) + (unit === '%' ? '%' : unit ? ' ' + unit : '');
      }
      const detail = line.insertCell();
      detail.append(element('span', availabilityNames[row.availability] || '확인 불가'));
      if (row.heads?.length) detail.append(element('span', row.heads.map(head => headNames[head] || head).join(' · '), 'market-explorer-reading-note'));
      if (row.derivation) detail.append(element('span', row.derivation, 'market-explorer-reading-note'));
      if (row.reason) detail.append(element('span', row.reason, 'market-explorer-reading-note'));
    }
    const wrapper = element('div', null, 'market-explorer-model-table');
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', '예측 입력값 표');
    wrapper.append(table);
    container.append(wrapper);
  }

  function updateCharts(preferredDate) {
    const filtering = Boolean(nodes.model.value);
    const options = filtering ?
      (verifiedModel?.rows || []).map(modelChart).filter(Boolean) :
      groups.find(entry => entry.id === nodes.category.value).charts;
    chartOptions = options.filter((option, index) => options.findIndex(entry => entry.id === option.id) === index);
    setOptions(nodes.chart, chartOptions.map(option => [option.id, option.label]), nodes.chart.value);
    nodes.category.closest('label').hidden = filtering;
    nodes.controls.dataset.filtered = String(filtering);
    nodes.chart.disabled = !chartOptions.length;
    nodes.range.disabled = !chartOptions.length;
    nodes.start.disabled = !chartOptions.length;
    nodes.end.disabled = !chartOptions.length;
    nodes.status.textContent = !Object.keys(history).length ? '시장 이력을 불러오지 못했습니다.' :
      verifiedModel && !chartOptions.length ? '이 예측에서 확인된 차트 연결 입력이 없습니다.' : '';
    nodes.status.hidden = !nodes.status.textContent;
    renderChart(chartOptions.find(option => option.id === nodes.chart.value)?.inputDate || preferredDate);
  }

  async function load(filename, schema) {
    const response = await fetch('data/' + filename + '.json', { cache: 'no-store' });
    if (!response.ok) throw Error('데이터 응답 없음');
    const payload = await response.json();
    if (payload.schema_version !== schema) throw Error('알 수 없는 데이터 형식');
    return payload;
  }

  function openLinkedSnapshot() {
    const destination = pick(window.location.hash.slice(1));
    if (destination?.matches('details.market-explorer-snapshot')) destination.open = true;
  }

  window.addEventListener('hashchange', openLinkedSnapshot);
  for (const link of document.querySelectorAll('a[href="#extra-numerics"], a[href="#market-indicators"], a[href="#economic-calendar"]')) {
    link.addEventListener('click', event => {
      if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) pick(link.hash.slice(1)).open = true;
    });
  }
  openLinkedSnapshot();

  nodes.model.addEventListener('change', async () => {
    const model = models.get(nodes.model.value);
    const request = ++modelRequest;
    verifiedModel = null;
    renderModel(model, 'pending');
    updateCharts('');
    if (!model) return;
    const verification = await checkCurrentRun(model);
    if (request !== modelRequest) return;
    if (verification === 'matched') verifiedModel = model;
    renderModel(model, verification);
    updateCharts(model.asof_date || '');
  });
  nodes.category.addEventListener('change', () => updateCharts(''));
  nodes.chart.addEventListener('change', () => {
    const choice = chartOptions.find(option => option.id === nodes.chart.value);
    renderChart(choice?.inputDate || verifiedModel?.asof_date || selectedDate);
  });
  nodes.range.addEventListener('change', () => {
    if (nodes.range.value === 'custom' && frame) {
      if (!nodes.start.value) nodes.start.value = frame.start || '';
      if (!nodes.end.value) nodes.end.value = frame.end || '';
    }
    renderChart();
  });
  nodes.start.addEventListener('change', () => renderChart());
  nodes.end.addEventListener('change', () => renderChart());
  nodes.date.addEventListener('change', () => chooseDate(nodes.date.value));
  nodes.prev.addEventListener('click', () => chooseDate(frame.visibleDates[frame.visibleDates.indexOf(selectedDate) - 1]));
  nodes.next.addEventListener('click', () => chooseDate(frame.visibleDates[frame.visibleDates.indexOf(selectedDate) + 1]));
  new ResizeObserver(() => {
    const width = Math.max(250, Math.floor(nodes.plot.getBoundingClientRect().width));
    if (plotWidth !== width) renderPlot();
  }).observe(nodes.plot);

  Promise.allSettled([
    load('market-chart-history', 'market_chart_history_v1'),
    load('market-chart-model-inputs', 'market_chart_model_inputs_v1'),
  ]).then(([historyResult, inputsResult]) => {
    if (historyResult.status === 'fulfilled') history = parseHistory(historyResult.value.series);
    if (inputsResult.status === 'fulfilled') {
      models = new Map((inputsResult.value.models || []).map(model => [model.key + ':' + model.market, model]));
    }
    setOptions(nodes.model, [
      ['', models.size ? '전체 시장 지표' : '예측 입력 기록 확인 불가'],
      ...Array.from(models, ([id, model]) => [id, (model.label || '모델 ' + model.key) + ' · ' + model.market]),
    ]);
    nodes.model.disabled = !models.size;
    setOptions(nodes.category, groups.map(entry => [entry.id, entry.label]));
    nodes.controls.hidden = false;
    updateCharts('');
  });
})();
