(() => {
  'use strict';
  const status = document.getElementById('cross-assets-status');
  const body = document.getElementById('cross-assets-rows');
  const series = [
    ['usdkrw', '원/달러'],
    ['dxy', '달러 인덱스'],
    ['btc', '비트코인'],
    ['eth', '이더리움'],
    ['wti', 'WTI (서부 텍사스 원유)'],
    ['brent', '브렌트유'],
    ['us_1y', '미국 국채 1년'],
    ['us_3y', '미국 국채 3년'],
    ['us_5y', '미국 국채 5년'],
    ['us_10y', '미국 국채 10년'],
    ['kr_1y', '한국 국고채 1년'],
    ['kr_3y', '한국 국고채 3년'],
    ['kr_5y', '한국 국고채 5년'],
    ['kr_10y', '한국 국고채 10년'],
    ['jp_1y', '일본 국채 1년'],
    ['jp_3y', '일본 국채 3년'],
    ['jp_5y', '일본 국채 5년'],
    ['jp_10y', '일본 국채 10년'],
  ];
  const kst = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });

  const changeText = row => {
    if (!row) return '—';
    if (typeof row.change_display === 'string' && row.change_display) return row.change_display;
    if (!Number.isFinite(row.change_pct)) return '—';
    return `${row.change_pct > 0 ? '+' : ''}${Number(row.change_pct).toFixed(2)}%`;
  };

  fetch('data/unofficial_numerics.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      if (data?.schema_version !== 'unofficial_numerics_v1' || !Array.isArray(data.series)) throw new Error();
      const byId = new Map(data.series.filter(row => row && row.id).map(row => [row.id, row]));
      body.replaceChildren();
      for (const [id, label] of series) {
        const row = byId.get(id);
        const ready = row && typeof row.display === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(row.date || '');
        const tr = body.insertRow();
        for (const text of [ready ? row.label || label : label, ready ? row.display : '—', changeText(ready ? row : null), ready ? row.date : '—']) {
          tr.insertCell().textContent = text;
        }
      }
      const stamp = new Date(data.received_at);
      status.textContent = Number.isNaN(stamp.valueOf()) ? '' : `수신 ${kst.format(stamp)} KST`;
    })
    .catch(() => { status.textContent = '환율·금리·원유를 확인하지 못했습니다.'; });
})();
