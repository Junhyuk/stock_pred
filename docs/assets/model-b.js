(() => {
  'use strict';
  const status = document.getElementById('model-b-status');
  const paired = document.getElementById('paired-forecast');
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
    },
  });

  const renderPairs = modelB => {
    if (typeof renderCard !== 'function' || !paired) return;
    const generated = kstStamp(modelB?.generated_at);
    const noteFor = row => {
      const trained = day(row?.trained_through) || '확인 불가';
      const variant = row?.variant ? ` · ${row.variant}` : '';
      return generated
        ? `수동 예측 · 생성 ${generated} KST · 학습 마지막 ${trained}${variant}`
        : '수동 예측 생성 시각을 확인하지 못했습니다.';
    };
    const byMarket = new Map((modelB?.markets || []).filter(row => row && names.includes(row.market)).map(row => [row.market, row]));
    const automatic = new Map((window.__forecastCards || []).filter(card => names.includes(card.item?.market)).map(card => [card.item.market, card]));
    paired.replaceChildren();
    for (const market of names) {
      const row = document.createElement('div');
      row.className = 'compare-row';
      const auto = automatic.get(market);
      const manualRow = byMarket.get(market);
      const autoCard = auto ? renderCard(auto.item, auto.runStatus) : '<p>모델 A 결과가 없습니다.</p>';
      const manualCard = manualRow && Number.isFinite(manualRow.p_up) && Number.isFinite(manualRow.close)
        ? renderCard(modelBItem(manualRow), 'research_only')
        : '<p>모델 B 결과가 없습니다.</p>';
      const manual = noteFor(manualRow);
      row.innerHTML = `<div class="compare-side"><h2>모델 A · ${market}</h2><p class="model-run-note">평일 자동 예측</p>${autoCard}</div>`
        + `<div class="compare-side"><h2>모델 B · ${market}</h2><p class="model-run-note">${manual}</p>${manualCard}</div>`;
      paired.appendChild(row);
    }
    if (status) status.textContent = noteFor(byMarket.get('KOSPI') || (modelB?.markets || [])[0]);
  };

  const start = () => {
    fetch('data/model_b_forecast.json', { cache: 'no-store' })
      .then(response => { if (!response.ok) throw new Error(); return response.json(); })
      .then(data => {
        if (data?.schema_version !== 'model_b_forecast_v1' || !Array.isArray(data.markets)) throw new Error();
        const draw = () => renderPairs(data);
        if (window.__forecastCards) draw();
        else document.addEventListener('forecast-cards-ready', draw, { once: true });
      })
      .catch(() => { if (status) status.textContent = '모델 B 결과를 확인하지 못했습니다.'; });
  };
  start();
})();
