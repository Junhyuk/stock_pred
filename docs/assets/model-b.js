(() => {
  'use strict';
  const status = document.getElementById('model-b-status');
  const cards = document.getElementById('manual-model-b-cards');
  const target = document.getElementById('model-b-target');
  const marketNames = { KOSPI: '코스피', KOSDAQ: '코스닥' };
  const names = Object.keys(marketNames);

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
  const baseline = '장후 검증 기준선 · 종가 유지';

  const modelBItem = (row, data) => ({
    market: row.market,
    expected_index_label: row.variant === baseline ? '기준선 종가' : '예상 종가',
    carried_forward: row.carried_forward === true && row.target_date !== data.target_date,
    context: {
      asof_date: day(row.asof_date),
      target_date: day(row.target_date),
      target_close_at: row.target_date === data.target_date ? data.target_close_at : undefined,
    },
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
    if (status) {
      const baselineOnly = data.markets.length > 0 && data.markets.every(row => row?.variant === baseline);
      const when = generated ? ` · 생성 ${generated} KST` : '';
      status.textContent = !data.markets.length ? '장전 예상 종가 산출 대기'
        : baselineOnly ? `${baseline}${stage === '장후' ? when : ''}`
        : `${stage} 예측${when}`;
    }
    const byMarket = new Map(data.markets.filter(row => row && names.includes(row.market)).map(row => [row.market, row]));
    const sharedTarget = byMarket.size === names.length && Boolean(day(data.target_date)) &&
      [...byMarket.values()].every(row => row.target_date === data.target_date
        && Number.isFinite(row.p_up) && Number.isFinite(row.close));
    if (target) {
      target.hidden = !sharedTarget;
      target.innerHTML = sharedTarget ? renderTargetDate({
        target_date: data.target_date, target_close_at: data.target_close_at,
      }) : '';
    }
    cards.replaceChildren();
    for (const market of names) {
      const row = byMarket.get(market);
      const holder = document.createElement('div');
      if (!row || !Number.isFinite(row.p_up) || !Number.isFinite(row.close)) {
        holder.innerHTML = `<article class="forecast-card"><h3>${marketNames[market]}</h3><p class="forecast-note">예상 종가 산출 대기</p></article>`;
      } else {
        const item = modelBItem(row, data);
        const trained = day(row.trained_through);
        const evaluation = row.evaluation_2026;
        const review = evaluation?.n > 0 && Number.isFinite(evaluation.close_mae)
          ? `<p>2026년 장후 재구성 · ${escapeHtml(evaluation.n)}거래일 · 평균 종가 오차 ${evaluation.close_mae.toFixed(2)}점</p>`
          : '';
        const details = row.variant || trained || review
          ? `<details class="forecast-support model-card-details">
              <summary>모델 정보</summary>
              ${row.variant ? `<p>${escapeHtml(row.variant)}</p>` : ''}
              ${trained ? `<p>학습 기준일 ${trained}</p>` : ''}
              ${review}
            </details>`
          : '';
        const card = renderCard(item, 'research_only', undefined, sharedTarget);
        holder.innerHTML = details ? card.replace('</article>', `${details}</article>`) : card;
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
