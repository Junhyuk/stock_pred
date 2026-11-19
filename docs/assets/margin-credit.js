(() => {
  'use strict';

  const status = document.getElementById('margin-credit-status');
  const content = document.getElementById('margin-credit-content');
  const labels = { KOSPI: '코스피 (KOSPI)', KOSDAQ: '코스닥 (KOSDAQ)' };

  function validDate(text) {
    if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
    const parsed = new Date(`${text}T00:00:00Z`);
    return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === text;
  }

  function validRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0 || rows.length > 90) return false;
    return rows.every((row, i) => row && validDate(row.date) &&
      typeof row.balance_krw === 'number' && Number.isFinite(row.balance_krw) && row.balance_krw >= 0 &&
      (i === 0 || rows[i - 1].date < row.date));
  }

  function inTrillion(value) { return (value / 1e12).toFixed(3); }

  function card(market, rows) {
    const label = labels[market];
    if (!validRows(rows)) return `<article class="credit-card"><h3>${label}</h3><p>표시할 자료가 없습니다.</p></article>`;
    const latest = rows[rows.length - 1];
    const values = rows.map(row => row.balance_krw / 1e12);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = max === min ? Math.max(max * 0.02, 0.01) : (max - min) * 0.12;
    const low = Math.max(0, min - padding);
    const high = max + padding;
    const x = i => 55 + (rows.length === 1 ? 192 : 384 * i / (rows.length - 1));
    const y = value => 18 + 125 * (high - value) / (high - low);
    const path = values.map((value, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(value).toFixed(1)}`).join(' ');
    const grid = [0, 0.5, 1].map(part => {
      const value = high - part * (high - low);
      const pos = 18 + 125 * part;
      return `<line class="credit-gridline" x1="55" y1="${pos}" x2="439" y2="${pos}"/><text x="48" y="${pos + 4}" text-anchor="end">${value.toFixed(2)}</text>`;
    }).join('');
    const table = rows.slice().reverse().map(row => `<tr><td>${row.date}</td><td>${inTrillion(row.balance_krw)}</td></tr>`).join('');
    const description = `${label} 신용융자 잔고, ${rows[0].date}부터 ${latest.date}까지, 최근 ${inTrillion(latest.balance_krw)}조 원`;
    return `<article class="credit-card" data-market="${market}"><h3>${label}</h3>
      <p class="credit-latest">${inTrillion(latest.balance_krw)}조 원</p>
      <p class="credit-date">기준일 ${latest.date} · 최근 관측 ${rows.length}건</p>
      <p class="credit-axis-label">잔고 (조 원)</p>
      <svg class="credit-chart" viewBox="0 0 480 175" role="img" aria-label="${description}">
        ${grid}<path class="credit-line" d="${path}"/><circle class="credit-dot" cx="${x(rows.length - 1).toFixed(1)}" cy="${y(values[values.length - 1]).toFixed(1)}" r="3"/>
        <text x="55" y="166">${rows[0].date}</text><text x="439" y="166" text-anchor="end">${latest.date}</text>
      </svg>
      <details><summary>날짜별 잔고 보기</summary><div class="credit-values"><table><thead><tr><th scope="col">기준일</th><th scope="col">잔고 (조 원)</th></tr></thead><tbody>${table}</tbody></table></div></details>
    </article>`;
  }

  function receivedAt(text) {
    if (typeof text !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(text)) return null;
    const date = new Date(text);
    if (Number.isNaN(date.valueOf())) return null;
    return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date);
  }

  fetch('data/margin_credit_chart.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error('missing'); return response.json(); })
    .then(data => {
      const receipt = receivedAt(data?.snapshot_received_at);
      if (data?.schema_version !== 'margin_credit_chart_v1' || data.unit !== 'KRW' ||
          data.source !== '금융투자협회 FreeSIS' ||
          data.source_url !== 'https://freesis.kofia.or.kr/stat/FreeSIS.do?parentDivId=MSIS10000000000000&serviceId=STATSCU0100000140' ||
          !data.markets || !receipt) throw new Error('invalid');
      content.innerHTML = `<div class="credit-grid">${Object.keys(labels).map(market => card(market, data.markets[market])).join('')}</div>`;
      status.textContent = `자료 수신 ${receipt} KST`;
    })
    .catch(() => {
      content.innerHTML = '';
      status.textContent = '신용융자 잔고를 불러오지 못했습니다.';
    });
})();
