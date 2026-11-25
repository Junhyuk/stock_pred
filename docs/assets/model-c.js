(() => {
  'use strict';
  const status = document.getElementById('model-c-status');
  const cards = document.getElementById('model-c-cards');
  const section = document.getElementById('model-c');
  const nav = document.getElementById('model-c-nav');
  const names = { KOSPI: '코스피', KOSDAQ: '코스닥' };

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
  const day = value => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return '';
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value ? value : '';
  };
  const kstStamp = value => {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return '';
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return '';
    return new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(date);
  };
  const level = value => Number.isFinite(value)
    ? Number(value).toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : '확인 불가';
  const probability = value => Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '확인 불가';
  const signedReturn = value => {
    if (!Number.isFinite(value)) return '확인 불가';
    const text = `${(value * 100).toFixed(2)}%`;
    return value > 0 ? `+${text}` : text;
  };
  const thresholdText = value => {
    if (!Number.isFinite(value)) return '3%';
    const text = (value * 100).toFixed(1).replace(/\.0$/, '');
    return `${text}%`;
  };

  const poolLine = (title, pool) => {
    if (!pool || !pool.n) {
      return `<div class="detail-line"><span>${title} </span><strong>표본 없음</strong></div>`;
    }
    const hit = Number.isFinite(pool.direction_hit) ? `${(pool.direction_hit * 100).toFixed(1)}%` : '확인 불가';
    const brier = Number.isFinite(pool.brier) ? Number(pool.brier).toFixed(4) : '확인 불가';
    const mae = Number.isFinite(pool.close_mae) ? level(pool.close_mae) : '확인 불가';
    return `<div class="detail-line"><span>${title} </span><strong>${pool.n}일 · 적중 ${hit} · Brier ${brier} · 종가 오차 ${mae}</strong></div>`;
  };

  const renderCard = (row, threshold) => {
    const label = names[row.market] || row.market;
    return `<article class="forecast-card">
      <h3>${escapeHtml(label)}</h3>
      <div class="card-status warn">급등·급락 전용. 절대 수익률 ${escapeHtml(threshold)} 이상인 날만 성적에 넣습니다. 평범한 날은 성공도 실패도 아닙니다.</div>
      <div class="card-dates session-dates">
        <div><strong>기준 종가일</strong> ${escapeHtml(day(row.asof_date) || '확인 불가')}</div>
        <div><strong>다음 거래일</strong> ${escapeHtml(day(row.target_date) || '확인 불가')}</div>
      </div>
      <div class="hero-metrics">
        <div class="hero-metric"><span>기준 종가</span><strong>${escapeHtml(level(row.close))}</strong></div>
        <div class="hero-metric"><span>예상 지수</span><strong>${escapeHtml(level(row.expected_index))}</strong></div>
      </div>
      <div class="prob-row">
        <div class="prob-box up"><span>상승 확률</span><strong>${escapeHtml(probability(row.p_up))}</strong></div>
        <div class="prob-box down"><span>하락 확률</span><strong>${escapeHtml(probability(row.p_down))}</strong></div>
      </div>
      <div class="detail-line"><span>예상 수익률 </span><strong>${escapeHtml(signedReturn(row.expected_return))}</strong></div>
      <div class="detail-line"><span>급등·급락 학습 마지막 </span><strong>${escapeHtml(day(row.trained_through) || '확인 불가')}</strong></div>
      ${poolLine('2026년 이전', row.examined?.pre_2026)}
      ${poolLine('이미 확인한 2026년', row.examined?.year_2026)}
      <div class="detail-line"><span>내일 수익률 </span><strong>아직 없으므로 예측은 그대로 표시</strong></div>
    </article>`;
  };

  const validThreeWay = row => {
    const values = [row.p_up, row.p_normal, row.p_down];
    return values.every(value => Number.isFinite(value) && value >= 0 && value <= 1)
      && Math.abs(values.reduce((sum, value) => sum + value, 0) - 1) <= 0.001;
  };

  const renderThreeWay = (row, threshold) => {
    const evaluation = row.evaluation_2026;
    const review = evaluation?.n > 0 && Number.isFinite(evaluation.brier)
      ? `<div class="detail-line">2026년 장후 재구성 · ${escapeHtml(evaluation.n)}거래일 · Brier ${evaluation.brier.toFixed(4)}${Number.isFinite(evaluation.baseline_brier) ? ` · 기준 ${evaluation.baseline_brier.toFixed(4)}` : ''}</div>`
      : '';
    return `<article class="forecast-card research-card">
      <h3>${escapeHtml(names[row.market])}</h3>
      <div class="card-dates session-dates">
        <div><strong>기준 종가일</strong> ${escapeHtml(day(row.asof_date) || '확인 불가')}</div>
        <div><strong>대상 거래일</strong> ${escapeHtml(day(row.target_date) || '확인 불가')}</div>
      </div>
      <div class="hero-metric"><span>기준 종가</span><strong>${escapeHtml(level(row.close))}</strong></div>
      <div class="prob-row three-way">
        <div class="prob-box up"><span>${escapeHtml(threshold)} 이상 상승</span><strong>${escapeHtml(probability(row.p_up))}</strong></div>
        <div class="prob-box neutral"><span>중립</span><strong>${escapeHtml(probability(row.p_normal))}</strong></div>
        <div class="prob-box down"><span>${escapeHtml(threshold)} 이상 하락</span><strong>${escapeHtml(probability(row.p_down))}</strong></div>
      </div>
      ${row.variant ? `<div class="detail-line">${escapeHtml(row.variant)}</div>` : ''}
      ${review}
    </article>`;
  };

  const render = data => {
    const threshold = thresholdText(data.threshold);
    const generated = kstStamp(data.generated_at);
    const when = generated ? `생성 ${generated} KST` : '생성 시각 확인 불가';
    const threeWay = data.probability_mode === 'three_way';
    if (status) {
      const stage = data.issue_stage === 'postclose' ? '장후' : data.issue_stage === 'premarket' ? '장전' : '수동';
      const triggers = Object.keys(names).filter(market => Math.abs(data.trigger_returns[market]) >= 0.03)
        .map(market => `${names[market]} ${signedReturn(data.trigger_returns[market])}`).join(' · ');
      status.textContent = threeWay
        ? `${stage} 예측 · ${when} · ${data.asof_date} 종가 변동 ${triggers} · 종가 수익률의 급등·중립·급락 확률 · 연구용`
        : `수동 GPU 예측 · ${data.architecture || '8층 미세조정 복사'} · ${when} · ${data.gpu || 'GPU 확인 불가'} · 운영 반영 비중 0. 급등·급락 전용이며 절대 수익률 ${threshold} 이상인 날만 적중과 오차에 넣습니다. 매매 지시가 아닙니다.`;
    }
    if (!cards) return;
    const byMarket = new Map((data.markets || []).filter(row => row && names[row.market]).map(row => [row.market, row]));
    cards.replaceChildren();
    for (const market of Object.keys(names)) {
      const row = byMarket.get(market);
      const holder = document.createElement('div');
      holder.className = 'compare-side';
      const canDisplay = row && Number.isFinite(row.close) && (threeWay
        ? validThreeWay(row)
        : Number.isFinite(row.p_up) && Number.isFinite(row.expected_index));
      holder.innerHTML = canDisplay
        ? (threeWay ? renderThreeWay(row, threshold) : renderCard(row, threshold))
        : '<p>모델 C 결과가 없습니다.</p>';
      cards.appendChild(holder);
    }
  };

  const hide = () => {
    if (section) section.hidden = true;
    if (nav) nav.hidden = true;
    if (cards) cards.replaceChildren();
    if (status) status.textContent = '';
  };
  const valid = data => {
    if (data?.schema_version !== 'model_c_forecast_v1' || data.status !== 'ready'
      || data.production_weight !== 0 || data.probability_mode !== 'three_way' || data.threshold !== 0.03
      || !day(data.asof_date) || !day(data.target_date) || data.asof_date >= data.target_date
      || typeof data.target_close_at !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(data.target_close_at)
      || !data.trigger_returns || typeof data.trigger_returns !== 'object' || Array.isArray(data.trigger_returns)
      || !Array.isArray(data.markets)) return false;
    const close = new Date(data.target_close_at);
    if (!Number.isFinite(close.valueOf()) || close.valueOf() <= Date.now()
      || kstStamp(data.target_close_at).slice(0, 10) !== data.target_date) return false;
    const returns = Object.keys(names).filter(market => Object.hasOwn(data.trigger_returns, market))
      .map(market => data.trigger_returns[market]);
    if (!returns.length || !returns.every(Number.isFinite) || !returns.some(value => Math.abs(value) >= 0.03)) return false;
    const rows = data.markets.filter(row => row && names[row.market]);
    return rows.length > 0 && rows.every(row => row.asof_date === data.asof_date && row.target_date === data.target_date
      && Number.isFinite(row.close) && row.close > 0 && validThreeWay(row));
  };
  let deadline = 0;
  const checkExpiry = () => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) { hide(); return; }
    setTimeout(checkExpiry, Math.min(remaining, 2147483647));
  };
  document.addEventListener('visibilitychange', () => {
    if (deadline && Date.now() >= deadline) hide();
  });
  hide();
  fetch('data/model_c_forecast.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      if (!valid(data)) return;
      deadline = new Date(data.target_close_at).valueOf();
      render(data);
      if (section) section.hidden = false;
      if (nav) nav.hidden = false;
      checkExpiry();
    })
    .catch(hide);
})();
