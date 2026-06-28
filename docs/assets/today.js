function bootTodayMarket() {
  try {
    renderTodayMarket(readInlineJson("today-snapshot"));
  } catch (_) {
    document.getElementById("todayStatus").innerHTML = metric("업데이트", "오류", "스냅샷 JSON을 확인하세요");
  }
}
function renderTodayMarket(data) {
  const quality = data.data_quality || {};
  const statusCards = todayStatusCards(data);
  document.getElementById("todayStatus").innerHTML = [
    metric("스냅샷 상태", statusCards.snapshot.value, statusCards.snapshot.sub),
    metric("국내 가격", statusCards.domestic.value, statusCards.domestic.sub),
    metric("해외 레짐", statusCards.regime.value, statusCards.regime.sub),
    metric("뉴스", statusCards.news.value, statusCards.news.sub)
  ].join("");
  renderTodayMarketOutlook(data.market_outlook || {});
  renderTodayMoveExplanations(data.move_explanations || {});
  renderTodayKoru(data.koru_linkage || {});
  renderTodaySectorLinkage(data.sector_linkage || {});
  document.getElementById("todayFocusPrices").innerHTML = table(data.focus_prices || [], [
    { key: "name", label: "종목" },
    { key: "symbol", label: "코드", format: symbolText },
    { key: "date", label: "일자", format: shortDate },
    { key: "close", label: "종가", format: money },
    { key: "volume", label: "거래량", format: money },
    { key: "source", label: "소스" },
    { key: "status", label: "상태" }
  ]);
  const yahooItems = (data.yahoo_prices || []).slice(0, 14);
  document.getElementById("todayYahooPrices").innerHTML = yahooItems.length
    ? table(yahooItems, [
        { key: "yahoo_symbol", label: "Yahoo" },
        { key: "symbol", label: "내부코드" },
        { key: "asset_type", label: "구분" },
        { key: "date", label: "일자", format: shortDate },
        { key: "close", label: "종가", format: money },
        { key: "currency", label: "통화" },
        { key: "source", label: "소스" }
      ])
    : '<div class="empty">Yahoo/yfinance 데이터가 없습니다.</div>';
  document.getElementById("todayRegime").innerHTML = renderRegimeDetail(data.global_regime || {});
  renderTodayGlobalMarkets(data.global_markets || {});
  renderTodayNews(data.news || [], data.macro_news || [], data.market_context || [], quality);
  document.getElementById("todayDisclaimer").textContent = data.disclaimer || "";
}
function todayStatusCards(data) {
  const quality = data.data_quality || {};
  const components = quality.components || {};
  const freshness = quality.freshness || {};
  const focusPrices = data.focus_prices || [];
  const readyFocus = focusPrices.filter(item => item.status === "ready");
  const latestPriceDate = shortDate(freshness.latest_date) !== "-" ? shortDate(freshness.latest_date) : latestShortDate(readyFocus);
  const snapshotStatus = `${statusLabel(data.status)}${freshness.stale ? " · 지연" : ""}`;
  const domesticValue = components.domestic_prices === "ready"
    ? `정상 · ${count(readyFocus.length)}/${count(focusPrices.length)} 종목`
    : componentValue(components.domestic_prices);
  const regime = data.global_regime || {};
  const regimeValue = components.market_regime === "ready"
    ? `${escapeHtml(regime.regime || "레짐")} · 위험 ${num1(regime.global_risk_score)}`
    : componentValue(components.market_regime);
  const stockNewsCount = (data.news || []).length;
  const macroNewsCount = (data.macro_news || []).length;
  const newsValue = components.news === "ready"
    ? (stockNewsCount ? `종목뉴스 ${count(stockNewsCount)}건` : `거시뉴스 ${count(macroNewsCount)}건`)
    : componentValue(components.news);
  return {
    snapshot: { value: escapeHtml(snapshotStatus), sub: escapeHtml(`스냅샷 ${shortDate(data.snapshot_date)}`) },
    domestic: { value: escapeHtml(domesticValue), sub: escapeHtml(compactText([latestPriceDate !== "-" ? `가격일 ${latestPriceDate}` : null])) },
    regime: { value: regimeValue, sub: escapeHtml(compactText([shortDate(regime.prediction_date) !== "-" ? `기준 ${shortDate(regime.prediction_date)}` : null])) },
    news: { value: escapeHtml(newsValue), sub: escapeHtml(compactText([macroNewsCount ? `거시 ${count(macroNewsCount)}건` : null, stockNewsCount ? `Naver ${count(stockNewsCount)}건` : "종목뉴스 없음"])) }
  };
}
function renderTodayMarketOutlook(payload) {
  const items = payload.items || [];
  const quality = payload.data_quality || {};
  const messages = (quality.messages || []).slice(0, 3);
  const status = payload.status || "not_collected";
  const ordered = ["TODAY:KOSPI", "TODAY:KOSDAQ", "WEEK:KOSPI", "WEEK:KOSDAQ"];
  const byKey = Object.fromEntries(items.map(item => [`${item.horizon}:${item.market}`, item]));
  const cards = ordered.filter(key => byKey[key]).map(key => outlookCard(byKey[key])).join("");
  document.getElementById("todayMarketOutlook").innerHTML = `
    <div class="inline"><span class="chip">${escapeHtml(statusLabel(status))}</span><span>기준 ${shortDate(payload.asof_date)}</span><span>정보제공용 전망</span></div>
    ${cards ? `<div class="card-grid">${cards}</div>` : '<div class="empty">아직 생성된 시장 전망이 없습니다.</div>'}
    ${messages.length ? `<p class="muted">${messages.map(escapeHtml).join(" · ")}</p>` : ""}
  `;
}
function outlookCard(item) {
  const directionClass = item.direction === "BEARISH" ? "risk" : "";
  const drivers = Array.isArray(item.drivers) ? item.drivers.slice(0, 5) : [];
  const driverHtml = drivers.length
    ? `<ul class="reason-list">${drivers.map(driver => `<li><b>${escapeHtml(driver.label || driver.kind || "-")}</b>: ${escapeHtml(String(driver.summary || "-"))}</li>`).join("")}</ul>`
    : `<p class="muted">driver 데이터 부족</p>`;
  return `<article class="mini-card">
    <h3>${escapeHtml(horizonLabel(item.horizon))} · ${escapeHtml(item.market || "-")}</h3>
    <div class="row"><span>예상 등락률</span><b class="${directionClass}">${pct(item.expected_return)}</b></div>
    <div class="row"><span>예상 범위</span><b>${pct(item.range_low)} ~ ${pct(item.range_high)}</b></div>
    <div class="row"><span>상승확률</span><b>${pct(item.up_probability)}</b></div>
    <div class="row"><span>하락확률</span><b>${pct(item.down_probability)}</b></div>
    <div class="row"><span>방향</span><b>${escapeHtml(directionLabel(item.direction))}</b></div>
    <div class="row"><span>신뢰도</span><b>${pct(item.confidence)}</b></div>
    <div><b>주요 driver</b>${driverHtml}</div>
  </article>`;
}
function horizonLabel(value) {
  const labels = { TODAY: "오늘", WEEK: "이번주", NEXT_TRADING_DAY: "다음 거래일" };
  return labels[String(value || "").toUpperCase()] || String(value || "-");
}
function renderTodayMoveExplanations(payload) {
  const market = payload.market || [];
  const top50 = payload.top50 || [];
  const status = payload.status || "not_collected";
  const marketHtml = market.length
    ? `<div class="card-grid">${market.map(item => moveCard(item)).join("")}</div>`
    : `<div class="empty">시장 요약이 아직 없습니다.</div>`;
  const topHtml = top50.length
    ? `<div class="card-grid">${top50.slice(0, 12).map(item => moveCard(item)).join("")}</div>`
    : `<div class="empty">Top50 내 2% 이상 변동 종목이 없습니다.</div>`;
  document.getElementById("todayMoveExplanations").innerHTML = `
    <div class="inline"><span class="chip">${escapeHtml(status)}</span><span>${shortDate(payload.asof_date)}</span></div>
    ${marketHtml}
    ${topHtml}
  `;
}
function moveCard(item) {
  const directionClass = item.direction === "DOWN" ? "risk" : "";
  return `<article class="mini-card">
    <h3>${escapeHtml(item.name || item.symbol)} <span class="muted">${escapeHtml(item.symbol || "")}</span></h3>
    <div class="row"><span>변동률</span><b class="${directionClass}">${pct(item.move_pct)}</b></div>
    <div class="row"><span>방향</span><b>${escapeHtml(item.direction || "-")}</b></div>
    <p><b>실제 변동 원인</b><br>${escapeHtml(item.primary_reason || "-")}</p>
  </article>`;
}
function renderTodayKoru(payload) {
  const item = payload.item || {};
  const trigger = item.market_index_trigger || {};
  document.getElementById("todayKoru").innerHTML = `
    <div class="inline"><span class="chip">${escapeHtml(payload.status || "not_collected")}</span><span>${shortDate(payload.asof_date)}</span></div>
    <div class="row"><span>KORU 1D</span><b>${pct(item.koru_return_1d)}</b></div>
    <div class="row"><span>EWY 1D</span><b>${pct(item.ewy_return_1d)}</b></div>
    <div class="row"><span>시장충격</span><b>${trigger.triggered ? "발생" : "미발생"}</b></div>
    <p class="notice">${escapeHtml(payload.leverage_warning || "KORU는 일간 3배 레버리지 ETF입니다.")}</p>
  `;
}
function renderTodaySectorLinkage(payload) {
  const items = payload.items || [];
  const cards = items.slice(0, 8).map(item => `<article class="mini-card">
    <h3>${escapeHtml(sectorLabel(item.domestic_sector))}</h3>
    <div class="row"><span>미국 섹터 1D</span><b>${pct(item.us_sector_return_1d)}</b></div>
    <div class="row"><span>impact</span><b>${pct(item.us_sector_impact_score)}</b></div>
  </article>`).join("");
  document.getElementById("todaySectorLinkage").innerHTML = `
    <div class="inline"><span class="chip">${escapeHtml(statusLabel(payload.status || "not_collected"))}</span><span>기준 ${shortDate(payload.asof_date)}</span></div>
    ${cards ? `<div class="card-grid">${cards}</div>` : '<div class="empty">미국 유사섹터 linkage가 아직 없습니다.</div>'}
  `;
}
function sectorLabel(value) {
  const labels = { semiconductor: "반도체", auto: "자동차/부품", industrial: "산업재", financial: "금융", healthcare: "헬스케어/바이오", energy_materials: "에너지/소재", broad: "시장 전체" };
  return labels[String(value || "")] || String(value || "-");
}
function renderTodayNews(items, macroNews, marketContext, quality) {
  const macroHtml = (macroNews || []).length
    ? `<section class="mini-card"><h3>거시·수급 뉴스</h3><div class="stack">${(macroNews || []).slice(0, 8).map(item => `<div class="row"><span><a href="${escapeHtml(item.link || "#")}" target="_blank" rel="noreferrer">${escapeHtml(item.title || "제목 없음")}</a><br><small class="muted">${shortDate(item.pub_date)} · ${escapeHtml(item.source || item.category || "-")}</small></span></div>`).join("")}</div></section>`
    : "";
  if (items.length) {
    const grouped = items.reduce((acc, item) => {
      const key = item.symbol || "unknown";
      if (!acc[key]) acc[key] = [];
      acc[key].push(item);
      return acc;
    }, {});
    document.getElementById("todayNews").innerHTML = macroHtml + Object.entries(grouped).map(([symbol, rows]) => `
      <section class="mini-card">
        <h3>${escapeHtml(rows[0].name || symbol)} <span class="muted">${escapeHtml(symbol)}</span></h3>
        <div class="stack">
          ${rows.slice(0, 5).map(item => `<div class="row"><span><a href="${escapeHtml(item.originallink || item.link || "#")}" target="_blank" rel="noreferrer">${escapeHtml(item.title || "제목 없음")}</a><br><small class="muted">${shortDate(item.pub_date)} · ${escapeHtml(item.source_name || "-")}</small></span></div>`).join("")}
        </div>
      </section>
    `).join("");
    return;
  }
  const notice = (quality.messages || []).length ? escapeHtml((quality.messages || []).join(" · ")) : "수집된 뉴스가 없습니다.";
  document.getElementById("todayNews").innerHTML = `${macroHtml}<div class="empty">${notice}</div>`;
}
function renderRegimeDetail(regime) {
  if (!regime || regime.status !== "ready") {
    return `<div class="row"><span>상태</span><b>수집 대기</b></div><p class="muted">${escapeHtml(regime?.message || "글로벌 레짐이 아직 없습니다.")}</p>`;
  }
  const reasons = regime.reasons || [];
  return `
    <div class="row"><span>Regime</span><b>${escapeHtml(regime.regime || "-")}</b></div>
    <div class="row"><span>글로벌 위험도</span><b>${num(regime.global_risk_score)}</b></div>
    <div class="row"><span>권장 현금비중</span><b>${pct(regime.recommended_cash_ratio)}</b></div>
    <div class="row"><span>기준일</span><b>${shortDate(regime.prediction_date)}</b></div>
    ${reasons.length ? `<ul class="reason-list">${reasons.slice(0, 6).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
  `;
}
function renderTodayGlobalMarkets(globalMarkets) {
  const items = globalMarkets.items || [];
  document.getElementById("todayGlobalMarkets").innerHTML = table(items.slice(0, 16), [
    { key: "display_name", label: "지표" },
    { key: "symbol", label: "심볼" },
    { key: "trade_date", label: "일자", format: shortDate },
    { key: "close", label: "종가", format: money },
    { key: "return_1d", label: "1D", format: pct },
    { key: "return_5d", label: "5D", format: pct },
    { key: "source_name", label: "소스" }
  ]);
}
document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("today-snapshot")) bootTodayMarket();
});
