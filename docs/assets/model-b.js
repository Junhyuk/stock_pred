(() => {
  'use strict';
  const status = document.getElementById('model-b-status');
  const cards = document.getElementById('model-b-cards');
  const names = { KOSPI: 'KOSPI', KOSDAQ: 'KOSDAQ' };

  fetch('data/model_b_forecast.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      if (data?.schema_version !== 'model_b_forecast_v1' || !Array.isArray(data.markets)) throw new Error();
      cards.replaceChildren();
      for (const row of data.markets) {
        if (!row || !names[row.market] || !Number.isFinite(row.p_up) || !Number.isFinite(row.close)) continue;
        const article = document.createElement('article');
        article.className = 'forecast-card';
        const up = `${(row.p_up * 100).toFixed(2)}%`;
        const down = `${((1 - row.p_up) * 100).toFixed(2)}%`;
        article.innerHTML = `
          <h3>${names[row.market]}</h3>
          <div class="card-dates">
            <div><strong>기준 종가일</strong> ${row.asof_date}</div>
            <div><strong>다음 거래일</strong> ${row.target_date}</div>
          </div>
          <div class="hero-metrics">
            <div class="hero-metric"><span>기준 종가</span><strong>${row.close.toLocaleString('ko-KR')}</strong></div>
          </div>
          <div class="prob-row">
            <div class="prob-box up"><span>상승 확률</span><strong>${up}</strong></div>
            <div class="prob-box down"><span>하락 확률</span><strong>${down}</strong></div>
          </div>`;
        cards.appendChild(article);
      }
      const asof = data.markets.find(row => row.asof_date)?.asof_date || '';
      status.textContent = `${data.architecture || '8층 트랜스포머'} · 기준 ${asof}`;
      if (!cards.children.length) status.textContent = '모델 B 결과를 확인하지 못했습니다.';
    })
    .catch(() => { status.textContent = '모델 B 결과를 확인하지 못했습니다.'; });
})();
