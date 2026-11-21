(() => {
  'use strict';
  const status = document.getElementById('market-indicators-status');
  const body = document.getElementById('market-indicators-rows');
  const items = [
    ...['1Y', '3Y', '10Y'].map(tenor => [`KR_YIELD_${tenor}`, `한국 국고채 ${tenor.slice(0, -1)}년`, 'percent']),
    ...['1Y', '3Y', '10Y'].map(tenor => [`US_YIELD_${tenor}`, `미국 국채 ${tenor.slice(0, -1)}년`, 'percent']),
    ['MARGIN_CREDIT_BALANCE', '전체 신용융자 잔고', 'KRW'],
  ];
  const kst = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

  fetch('data/market_indicators_status.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      const checked = new Date(data?.checked_at);
      if (data?.schema_version !== 'market_indicators_status_v1' || !Array.isArray(data.observations) ||
          typeof data.checked_at !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(data.checked_at) ||
          Number.isNaN(checked.valueOf())) throw new Error();
      for (const [series, name, unit] of items) {
        const row = data.observations.find(item => item.market === 'GLOBAL' && item.series === series &&
          item.unit === unit && Number.isFinite(item.value) && /^\d{4}-\d{2}-\d{2}$/.test(item.observation_date));
        const tr = body.insertRow();
        const value = row ? (unit === 'percent' ? `${row.value.toFixed(3)}%` : `${(row.value / 1e12).toFixed(3)}조 원`) : '—';
        const state = row ? (row.tracking_status === 'seen' ? '이번 수집 확인' : '이전 관측') : '자료 없음';
        for (const text of [name, value, row?.observation_date ?? '—', state]) tr.insertCell().textContent = text;
      }
      status.textContent = `수집 ${kst.format(checked)} KST`;
    })
    .catch(() => { status.textContent = '시장 지표 수집 상태를 불러오지 못했습니다.'; });
})();
