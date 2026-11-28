(() => {
  'use strict';
  const status = document.getElementById('model-c-status');
  const cards = document.getElementById('model-c-cards');
  const target = document.getElementById('model-c-target');
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

  const validThreeWay = row => {
    const values = [row.p_up, row.p_normal, row.p_down];
    return values.every(value => Number.isFinite(value) && value >= 0 && value <= 1)
      && Math.abs(values.reduce((sum, value) => sum + value, 0) - 1) <= 0.001;
  };

  const renderThreeWay = (row, threshold, targetClose, sharedTarget) => {
    const evaluation = row.evaluation_2026;
    const review = evaluation?.n > 0 && Number.isFinite(evaluation.brier)
      ? `<p>2026년 장후 재구성 · ${escapeHtml(evaluation.n)}거래일 · Brier ${evaluation.brier.toFixed(4)}${Number.isFinite(evaluation.baseline_brier) ? ` · 기준 ${evaluation.baseline_brier.toFixed(4)}` : ''}</p>`
      : '';
    const trained = day(row.trained_through);
    const details = row.variant || trained || review
      ? `<details class="forecast-support model-card-details">
          <summary>모델 정보</summary>
          ${row.variant ? `<p>${escapeHtml(row.variant)}</p>` : ''}
          ${trained ? `<p>학습 기준일 ${trained}</p>` : ''}
          ${review}
        </details>`
      : '';
    const interval = row.interval;
    const hasEstimate = Number.isFinite(row.expected_index) && row.expected_index > 0
      && Number.isFinite(row.expected_return) && interval?.level === 0.8
      && Number.isFinite(interval.low) && Number.isFinite(interval.high)
      && 0 < interval.low && interval.low <= interval.high;
    const ended = targetEnded({ target_date: row.target_date, target_close_at: targetClose });
    const returnClass = row.expected_return > 0 ? 'return-up' : row.expected_return < 0 ? 'return-down' : '';
    return `<article class="forecast-card research-card">
      <h3>${escapeHtml(names[row.market])}</h3>
      <div class="card-dates session-dates">
        <div><strong>기준 종가일</strong> ${escapeHtml(day(row.asof_date) || '확인 불가')}</div>
        ${sharedTarget ? '' : `<div><strong>대상 거래일</strong> ${escapeHtml(day(row.target_date) || '확인 불가')} <span class="session-ended" data-target-date="${escapeHtml(row.target_date)}" data-target-close="${escapeHtml(targetClose)}" data-kr-session${ended ? '' : ' hidden'}>이미 종료</span></div>`}
      </div>
      <div class="hero-metrics">
        <div class="hero-metric"><span>기준 종가</span><strong>${escapeHtml(level(row.close))}</strong></div>
        ${hasEstimate ? `<div class="hero-metric"><span>예상 종가</span><strong>${escapeHtml(level(row.expected_index))}</strong></div>` : ''}
      </div>
      <div class="prob-row three-way">
        <div class="prob-box up"><span>${escapeHtml(threshold)} 이상 상승</span><strong>${escapeHtml(probability(row.p_up))}</strong></div>
        <div class="prob-box neutral"><span>중립</span><strong>${escapeHtml(probability(row.p_normal))}</strong></div>
        <div class="prob-box down"><span>${escapeHtml(threshold)} 이상 하락</span><strong>${escapeHtml(probability(row.p_down))}</strong></div>
      </div>
      ${hasEstimate ? `
        <div class="detail-line"><span>예상 수익률 </span><strong class="${returnClass}">${escapeHtml(signedReturn(row.expected_return))}</strong></div>
        <div class="detail-line range-line"><span>80% 예측 구간 </span><strong>${escapeHtml(level(interval.low))} ~ ${escapeHtml(level(interval.high))}</strong></div>
        ${row.close_estimate_reconstructed === true ? '<div class="card-dates">예상 종가·구간은 기준일 자료로 사후 재구성</div>' : ''}
      ` : '<div class="detail-line">종가 추정 자료 없음</div>'}
      ${details}
    </article>`;
  };

  const render = data => {
    const threshold = thresholdText(data.threshold);
    const generated = kstStamp(data.generated_at);
    const when = generated ? ` · 생성 ${generated} KST` : '';
    if (status) {
      const stage = data.issue_stage === 'postclose' ? '장후' : data.issue_stage === 'premarket' ? '장전' : '수동';
      const triggers = Object.keys(names).filter(market => Math.abs(data.trigger_returns[market]) >= data.threshold)
        .map(market => `${names[market]} ${signedReturn(data.trigger_returns[market])}`).join(' · ');
      status.textContent = `${stage} 예측${when} · 기준일 ${data.asof_date} 변동 ${triggers}`;
    }
    if (!cards) return;
    const byMarket = new Map(data.markets.filter(row => row && names[row.market]).map(row => [row.market, row]));
    const sharedTarget = byMarket.size === Object.keys(names).length;
    if (target) {
      target.hidden = !sharedTarget;
      target.innerHTML = sharedTarget ? renderTargetDate({
        target_date: data.target_date, target_close_at: data.target_close_at,
      }) : '';
    }
    cards.replaceChildren();
    for (const market of Object.keys(names)) {
      const row = byMarket.get(market);
      const holder = document.createElement('div');
      holder.innerHTML = row
        ? renderThreeWay(row, threshold, data.target_close_at, sharedTarget)
        : `<article class="forecast-card"><h3>${names[market]}</h3><p class="forecast-note">모델 C 결과 없음</p></article>`;
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
    if (!Number.isFinite(close.valueOf()) || kstStamp(data.target_close_at).slice(0, 10) !== data.target_date) return false;
    const returns = Object.keys(names).filter(market => Object.hasOwn(data.trigger_returns, market))
      .map(market => data.trigger_returns[market]);
    if (!returns.length || !returns.every(Number.isFinite) || !returns.some(value => Math.abs(value) >= 0.03)) return false;
    const rows = data.markets.filter(row => row && names[row.market]);
    return rows.length > 0 && rows.every(row => row.asof_date === data.asof_date && row.target_date === data.target_date
      && Number.isFinite(row.close) && row.close > 0 && validThreeWay(row));
  };
  hide();
  fetch('data/model_c_forecast.json', { cache: 'no-store' })
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(data => {
      if (!valid(data)) return;
      render(data);
      if (section) section.hidden = false;
      if (nav) nav.hidden = false;
      const hash = document.defaultView?.location.hash;
      const destination = hash ? document.getElementById(hash.slice(1)) : null;
      if (destination && section?.contains(destination)) destination.scrollIntoView();
    })
    .catch(hide);
})();
