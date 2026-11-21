(() => {
  'use strict';
  const status = document.getElementById('extra-numerics-status');
  const body = document.getElementById('extra-numerics-rows');
  const kst = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });

  fetch('data/unofficial_numerics.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      if (data?.schema_version !== 'unofficial_numerics_v1' || !Array.isArray(data.series)) throw new Error();
      body.replaceChildren();
      for (const row of data.series) {
        if (!row || typeof row.label !== 'string' || typeof row.display !== 'string'
            || !/^\d{4}-\d{2}-\d{2}$/.test(row.date || '')) continue;
        const tr = body.insertRow();
        const change = Number.isFinite(row.change_pct)
          ? `${row.change_pct > 0 ? '+' : ''}${Number(row.change_pct).toFixed(2)}%`
          : '—';
        for (const text of [row.label, row.display, change, row.date]) tr.insertCell().textContent = text;
      }
      const stamp = new Date(data.received_at);
      status.textContent = Number.isNaN(stamp.valueOf()) ? '' : `수신 ${kst.format(stamp)} KST`;
      if (!body.rows.length) status.textContent = '추가 수치를 확인하지 못했습니다.';
    })
    .catch(() => { status.textContent = '추가 수치를 확인하지 못했습니다.'; });
})();
