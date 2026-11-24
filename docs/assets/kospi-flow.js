(() => {
  'use strict';
  const status = document.getElementById('kospi-flow-status');
  const content = document.getElementById('kospi-flow-content');
  const MIN_ROWS = 20;
  const MAX_ROWS = 640;
  const AVERAGE_SESSIONS = 20;
  const series = [
    ['institution_eok', '기관', 'flow-institution', 'institution'],
    ['personal_eok', '개인', 'flow-personal', 'personal'],
    ['foreign_eok', '외국인', 'flow-foreign', 'foreign'],
  ];
  const markets = ['KOSPI', 'KOSDAQ'];
  const LEFT = 96;
  const RIGHT = 468;
  const TOP = 14;
  const BOTTOM = 126;
  const LABEL_Y = 148;

  function validDate(text) {
    if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
    const parsed = new Date(`${text}T00:00:00Z`);
    return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === text;
  }

  function scopedRows(rows, windowStart) {
    if (!Array.isArray(rows) || !validDate(windowStart)) return rows;
    return rows.filter(row => row && row.date >= windowStart);
  }

  function validRows(rows) {
    if (!Array.isArray(rows) || rows.length < MIN_ROWS || rows.length > MAX_ROWS) return false;
    return rows.every((row, i) => row && validDate(row.date) &&
      series.every(([key]) => typeof row[key] === 'number' && Number.isFinite(row[key])) &&
      (i === 0 || rows[i - 1].date < row.date));
  }

  function amount(value) {
    return `${value > 0 ? '+' : ''}${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}억`;
  }

  function round1(value) {
    const sign = value < 0 ? -1 : 1;
    return sign * Math.round(Math.abs(value) * 10) / 10;
  }

  // Stored eok is won / 1e8, so eok / 1e4 is won / 1e12.
  function joText(eok) {
    const jo = eok / 10000;
    const rounded = round1(jo);
    const abs = Math.abs(rounded);
    if (abs === 0) {
      if (jo === 0) return '0조원';
      const finer = Math.round(Math.abs(jo) * 100) / 100;
      if (finer === 0) return '0조원';
      return `${jo < 0 ? '-' : '+'}${finer.toFixed(2)}조원`;
    }
    const body = Number.isInteger(abs) ? String(abs) : abs.toFixed(1);
    return `${rounded < 0 ? '-' : '+'}${body}조원`;
  }

  // Cumulative value on a date is the sum of daily eok from the first session
  // through that date. The sum starts at 0 before the first session.
  function cumulative(rows) {
    const running = { institution_eok: 0, personal_eok: 0, foreign_eok: 0 };
    return rows.map(row => {
      const point = { date: row.date };
      series.forEach(([key]) => {
        running[key] = Math.round((running[key] + row[key]) * 10) / 10;
        point[key] = running[key];
      });
      return point;
    });
  }

  // 4-week average of the cumulative line: the mean of the last 20 stored
  // trading sessions. Not a calendar window, and not an average of daily net buys.
  function trailingAverage(values, sessions) {
    return values.map((_, index) => {
      if (index + 1 < sessions) return null;
      let sum = 0;
      for (let cursor = index + 1 - sessions; cursor <= index; cursor += 1) sum += values[cursor];
      return sum / sessions;
    });
  }

  function monthTicks(points) {
    const months = [];
    let seen = '';
    points.forEach((row, index) => {
      const month = row.date.slice(0, 7);
      if (month === seen) return;
      seen = month;
      months.push({
        index,
        label: row.date.slice(2, 7),
        month: Number(row.date.slice(5, 7)),
      });
    });
    const quarter = months.filter(tick => [1, 4, 7, 10].includes(tick.month));
    return quarter.length >= 2 ? quarter : months;
  }

  function niceStep(span) {
    const raw = span / 4;
    const power = 10 ** Math.floor(Math.log10(raw));
    const error = raw / power;
    const factor = error >= 7.5 ? 10 : error >= 3.5 ? 5 : error >= 1.5 ? 2 : 1;
    return factor * power;
  }

  function axisTicks(low, high) {
    const step = niceStep(high - low);
    const start = Math.ceil(low / step - 1e-9) * step;
    const ticks = [];
    for (let value = start; value <= high + step * 1e-6; value += step) {
      if (value < low - step * 0.01 || value > high + step * 0.01) continue;
      ticks.push(Math.round(value * 10) / 10);
      if (ticks.length > 8) break;
    }
    return ticks.length ? ticks : [Math.round(((low + high) / 2) * 10) / 10];
  }

  function markerIndexes(count) {
    const stride = Math.max(1, Math.round(count / 32));
    const indexes = [];
    for (let index = 0; index < count; index += stride) indexes.push(index);
    if (indexes[indexes.length - 1] !== count - 1) indexes.push(count - 1);
    return indexes;
  }

  function investorChart(market, rows, points, investor, averageSessions) {
    const [key, label, klass, investorId] = investor;
    const values = points.map(point => point[key]);
    const average = trailingAverage(values, averageSessions);
    const scaled = values.concat(average.filter(value => value != null));
    const min = Math.min(0, ...scaled);
    const max = Math.max(0, ...scaled);
    const span = max - min || 1;
    const low = min - span * 0.08;
    const high = max + span * 0.08;
    const x = index => LEFT + (points.length === 1 ? (RIGHT - LEFT) / 2 : (RIGHT - LEFT) * index / (points.length - 1));
    const y = value => TOP + (BOTTOM - TOP) * (high - value) / (high - low);
    const grid = axisTicks(low, high).map(value => {
      const pos = y(value);
      return `<line class="credit-gridline" x1="${LEFT}" y1="${pos.toFixed(1)}" x2="${RIGHT}" y2="${pos.toFixed(1)}"/><text class="flow-ylabel" x="${LEFT - 8}" y="${(pos + 4).toFixed(1)}" text-anchor="end">${Math.round(value).toLocaleString('ko-KR')}</text>`;
    }).join('');
    const cumulativePath = points.map((point, index) => `${index ? 'L' : 'M'}${x(index).toFixed(1)},${y(point[key]).toFixed(1)}`).join(' ');
    const averageCommands = [];
    average.forEach((value, index) => {
      if (value == null) return;
      averageCommands.push(`${averageCommands.length ? 'L' : 'M'}${x(index).toFixed(1)},${y(value).toFixed(1)}`);
    });
    const size = 5.2;
    const markers = markerIndexes(points.length).map(index => {
      const cx = x(index);
      const cy = y(points[index][key]);
      return `<rect class="flow-marker ${klass}" x="${(cx - size / 2).toFixed(1)}" y="${(cy - size / 2).toFixed(1)}" width="${size}" height="${size}"/>`;
    }).join('');
    const ticks = monthTicks(points).map(tick => {
      const pos = x(tick.index);
      const anchor = pos < LEFT + 28 ? 'start' : pos > RIGHT - 28 ? 'end' : 'middle';
      return `<text class="flow-tick" x="${pos.toFixed(1)}" y="${LABEL_Y}" text-anchor="${anchor}">${tick.label}</text>`;
    }).join('');
    const latest = points[points.length - 1];
    const today = rows[rows.length - 1][key];
    const prior = rows[rows.length - 2][key];
    const maLast = average[average.length - 1];
    const maAttr = round1(maLast);
    const maText = Number.isInteger(maAttr) ? String(maAttr) : maAttr.toFixed(1);
    const description = `${market} ${label} 누적 순매수 ${joText(latest[key])}, 오늘 ${joText(today)}, 전일 ${joText(prior)}. 가는 선은 누적선의 ${averageSessions}거래일 평균. ${rows[0].date}부터 ${latest.date}까지`;
    return `<article class="flow-chart" data-investor="${investorId}" data-average-sessions="${averageSessions}" data-ma-eok="${maText}">
      <div class="flow-figure">
        <div class="flow-plot">
          <h4>${label}순매수</h4>
          <p class="flow-legend"><span class="${klass}">${label}순매수누적</span><span class="flow-ma">4주 이평</span></p>
          <p class="credit-axis-label">누적 순매수 (억원)</p>
          <svg class="credit-chart" viewBox="0 0 484 164" role="img" aria-label="${description}">
            <rect class="flow-frame" x="${LEFT}" y="${TOP}" width="${RIGHT - LEFT}" height="${BOTTOM - TOP}"/>
            ${grid}
            <line class="flow-zero" x1="${LEFT}" y1="${y(0).toFixed(1)}" x2="${RIGHT}" y2="${y(0).toFixed(1)}"/>
            <path class="credit-line flow-ma" d="${averageCommands.join(' ')}"/>
            <path class="credit-line flow-cum ${klass}" d="${cumulativePath}"/>
            ${markers}${ticks}
          </svg>
        </div>
        <aside class="flow-box">
          <p>누적 / ${joText(latest[key])}</p>
          <p>오늘 / ${joText(today)}</p>
          <p>전일 / ${joText(prior)}</p>
        </aside>
      </div>
    </article>`;
  }

  function chart(market, rows, averageSessions) {
    const points = cumulative(rows);
    const latest = points[points.length - 1];
    const table = points.slice().reverse().map(row =>
      `<tr><td>${row.date}</td>${series.map(([key]) => `<td>${amount(row[key])}</td>`).join('')}</tr>`).join('');
    const heads = series.map(([, label]) => `<th scope="col">${label}</th>`).join('');
    const charts = series.map(investor => investorChart(market, rows, points, investor, averageSessions)).join('');
    return `<section class="flow-market" data-market="${market}"><h3>${market}</h3>
      <p class="credit-date">기준일 ${latest.date} · ${rows[0].date}부터 ${rows.length}거래일</p>
      ${charts}
      <details><summary>날짜별 누적 순매수 보기</summary><div class="credit-values"><table><thead><tr><th scope="col">날짜</th>${heads}</tr></thead><tbody>${table}</tbody></table></div></details>
    </section>`;
  }

  function receivedAt(text) {
    if (typeof text !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(text)) return null;
    const date = new Date(text);
    if (Number.isNaN(date.valueOf())) return null;
    return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date);
  }

  fetch('data/kospi_investor_flow_chart.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      const receipt = receivedAt(data?.snapshot_received_at);
      const sessions = Object.fromEntries(markets.map(market => [market, scopedRows(data?.markets?.[market]?.sessions, data?.window_start)]));
      if (data?.schema_version !== 'kospi_investor_flow_chart_v1' || data.unit !== '억원' ||
          data.production_weight !== 0 || data.average_sessions !== AVERAGE_SESSIONS || !receipt ||
          markets.some(market => !validRows(sessions[market]))) throw new Error();
      content.innerHTML = `<div class="credit-grid">${markets.map(market => chart(market, sessions[market], data.average_sessions)).join('')}</div>`;
      const provisional = Array.isArray(data.provisional_dates) ? data.provisional_dates.filter(validDate) : [];
      status.textContent = `자료 수신 ${receipt} KST` +
        (provisional.length ? ` · ${provisional.join(', ')} 잠정치(다음 수집에서 확정값으로 교체)` : '');
    })
    .catch(() => {
      content.innerHTML = '';
      status.textContent = '투자자 누적 순매수 그래프를 불러오지 못했습니다.';
    });
})();
