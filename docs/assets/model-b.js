(() => {
  'use strict';
  const status = document.getElementById('model-b-status');
  const cards = document.getElementById('manual-model-b-cards');
  const names = ['KOSPI', 'KOSDAQ'];

  const kstStamp = value => {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return '';
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return '';
    return new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(date);
  };
  const day = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value : '';

  const modelBItem = row => ({
    market: row.market,
    context: { asof_date: day(row.asof_date), target_date: day(row.target_date) },
    forecast: {
      status: 'ready',
      latest_close: row.close,
      p_up: row.p_up,
      p_down: row.p_down,
      expected_index: row.expected_index,
      expected_return: row.expected_return,
      index_range_low: row.interval?.level === 0.8 ? row.interval.low : undefined,
      index_range_high: row.interval?.level === 0.8 ? row.interval.high : undefined,
    },
  });

  const render = data => {
    if (!cards) return;
    const generated = kstStamp(data.generated_at);
    const stage = data.issue_stage === 'postclose' ? '장후' : data.issue_stage === 'premarket' ? '장전' : '수동';
    if (status) status.textContent = generated ? `${stage} 예측 · 생성 ${generated} KST · 연구용` : '수동 예측 생성 시각을 확인하지 못했습니다.';
    const byMarket = new Map(data.markets.filter(row => row && names.includes(row.market)).map(row => [row.market, row]));
    cards.replaceChildren();
    for (const market of names) {
      const row = byMarket.get(market);
      const holder = document.createElement('div');
      if (!row || !Number.isFinite(row.p_up) || !Number.isFinite(row.close)) {
        holder.textContent = `${market} 수동 예측 결과가 없습니다.`;
      } else {
        const trained = day(row.trained_through) || '확인 불가';
        const variant = row.variant ? ` · ${escapeHtml(row.variant)}` : '';
        const evaluation = row.evaluation_2026;
        const review = evaluation?.n > 0 && Number.isFinite(evaluation.close_mae)
          ? `<p class="model-run-note">2026년 장후 재구성 · ${escapeHtml(evaluation.n)}거래일 · 종가 MAE ${evaluation.close_mae.toFixed(2)}점</p>`
          : '';
        holder.innerHTML = `<p class="model-run-note">학습 마지막 ${trained}${variant}</p>${renderCard(modelBItem(row), 'research_only')}${review}`;
      }
      cards.appendChild(holder);
    }
  };

  fetch('data/model_b_forecast.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      if (data?.schema_version !== 'model_b_forecast_v1' || !Array.isArray(data.markets)) throw new Error();
      render(data);
    })
    .catch(() => { if (status) status.textContent = '모델 B 수동 예측을 확인하지 못했습니다.'; });
})();
