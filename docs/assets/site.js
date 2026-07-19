const DATA = "data/";

async function load(name) {
  const response = await fetch(`${DATA}${name}.json`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${name} ${response.status}`);
  return response.json();
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}
function num(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(3);
}
function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Math.round(Number(value)).toLocaleString();
}
function shortDate(value) { return value ? String(value).slice(0, 10) : "-"; }
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function metric(label, value, sub = "") {
  return `<article class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><p class="muted">${esc(sub)}</p></article>`;
}
function table(items, columns) {
  if (!items || items.length === 0) return '<div class="empty">데이터 미수집: 최신 파이프라인 실행 또는 API 키 설정이 필요합니다.</div>';
  const head = columns.map((column) => `<th>${esc(column.label)}</th>`).join("");
  const body = items.map((item) => `<tr>${columns.map((column) => {
    const raw = item[column.key];
    const value = column.format ? column.format(raw, item) : esc(raw ?? "-");
    return `<td>${value}</td>`;
  }).join("")}</tr>`).join("");
  return `<div class="table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function statusBadge(status) {
  const value = String(status || "-");
  const cls = value === "ready" || value === "exported" ? "badge" : value.includes("error") || value === "blocked_stale_input" ? "badge risk" : "badge warn";
  return `<span class="${cls}">${esc(value)}</span>`;
}

async function boot() {
  const [manifest, dashboard, today, tomorrow, premarketShadow, marketAnchorShadow, marketUsLinkageShadow, marketNewsFeedLlmShadow, localLlmNewsShadow, top20, top50, longShort, moves, news, newsSignals, officialStatus, newsUrlCrawl, xTop20Impact, xMarketImpact, validation] = await Promise.all([
    load("manifest"),
    load("dashboard"),
    load("today"),
    load("tomorrow"),
    load("premarket_shadow"),
    load("market_anchor_shadow"),
    load("market_us_linkage_shadow"),
    load("marketnewsfeed_llm_status"),
    load("local_llm_news_status"),
    load("top20_upside_3M"),
    load("top50_3M"),
    load("long_short_2M"),
    load("market_move_explanations"),
    load("news_latest"),
    load("news_signal_latest"),
    load("official_news_status"),
    load("news_url_crawl"),
    load("x_news_top20_impact"),
    load("x_market_outlook_impact"),
    load("validation"),
  ]);
  document.getElementById("runStatus").innerHTML = `${statusBadge(validation.status || manifest.status)}<br><span>${shortDate(manifest.generated_at)}</span>`;
  document.getElementById("disclaimer").textContent = manifest.disclaimer || dashboard.disclaimer || "";
  const todayQuality = today.data_quality || {};
  const tomorrowItems = representativesByMarket(tomorrow.market_outlook?.items || []);
  const longShortCount = (longShort.long_leg || []).length + (longShort.short_leg || []).length;
  document.getElementById("summary").innerHTML = [
    metric("Snapshot", shortDate(dashboard.snapshot_date || today.snapshot_date), today.status || "-"),
    metric("Top50", validation.counts?.top50 ?? top50.items?.length ?? 0, shortDate(top50.snapshot_date)),
    metric("Long/Short", longShortCount, `${longShort.horizon || "2M"} · ${shortDate(longShort.asof_date)}`),
    metric("Tomorrow", tomorrowItems.length, tomorrow.next_trading_day || "-"),
  ].join("");
  document.getElementById("top20").innerHTML = table(top20.items || [], [
    { key: "rank", label: "#" },
    { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
    { key: "market", label: "Market" },
    { key: "up_probability", label: "Up Prob", format: pct },
    { key: "upside_return", label: "Upside", format: pct },
    { key: "risk_score", label: "Risk", format: pct },
  ]);
  document.getElementById("top50").innerHTML = renderTop50(top50);
  document.getElementById("longShort").innerHTML = renderLongShort(longShort);
  document.getElementById("tomorrow").innerHTML = renderTomorrow(tomorrow);
  document.getElementById("premarketShadow").innerHTML = renderPremarketShadow(premarketShadow);
  document.getElementById("marketAnchorShadow").innerHTML = renderMarketAnchorShadow(marketAnchorShadow);
  document.getElementById("marketUsLinkageShadow").innerHTML = renderMarketUsLinkageShadow(marketUsLinkageShadow);
  document.getElementById("marketNewsFeedLlmShadow").innerHTML = renderMarketNewsFeedLlmShadow(marketNewsFeedLlmShadow);
  document.getElementById("localLlmNewsShadow").innerHTML = renderLocalLlmNewsShadow(localLlmNewsShadow);
  document.getElementById("xImpact").innerHTML = renderXImpact(xTop20Impact, xMarketImpact);
  document.getElementById("officialData").innerHTML = renderOfficialData(officialStatus, newsUrlCrawl);
  document.getElementById("quality").innerHTML = renderQuality(todayQuality, validation);
  document.getElementById("newsSignals").innerHTML = renderNewsSignals(newsSignals);
  document.getElementById("moves").innerHTML = table([...(moves.market || []), ...(moves.top50 || [])].slice(0, 20), [
    { key: "name", label: "Name", format: (value, row) => esc(value || row.market || row.symbol || "-") },
    { key: "move_pct", label: "Move", format: pct },
    { key: "primary_reason", label: "Reason" },
    { key: "confidence", label: "Confidence", format: pct },
  ]);
  document.getElementById("news").innerHTML = renderNews(news.items || []);
}

function renderTop50(data) {
  return table(data.items || [], [
    { key: "prediction_rank", label: "#" },
    { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
    { key: "market", label: "Market" },
    { key: "market_cap", label: "Market Cap", format: money },
    { key: "close", label: "Close", format: money },
    { key: "volume", label: "Volume", format: money },
    { key: "pred_prob_top20", label: "Up Prob", format: pct },
    { key: "pred_return", label: "Return", format: pct },
    { key: "recommendation_rank", label: "Top20 Rank" },
  ]);
}

function renderLongShort(data) {
  const markets = data.markets || {};
  const kospi = markets.KOSPI || { long_leg: [], short_leg: [] };
  const kosdaq = markets.KOSDAQ || { long_leg: [], short_leg: [] };
  const totalLong = (data.long_leg || []).length;
  const totalShort = (data.short_leg || []).length;
  const kospiCount = (kospi.long_leg || []).length + (kospi.short_leg || []).length;
  const kosdaqCount = (kosdaq.long_leg || []).length + (kosdaq.short_leg || []).length;
  return [
    `<div class="inline-metrics">
      ${inlineMetric("기준일", shortDate(data.asof_date), data.horizon || "2M")}
      ${inlineMetric("LONG", totalLong, "모의 long leg")}
      ${inlineMetric("SHORT", totalShort, "모의 short leg")}
      ${inlineMetric("시장분할", kospiCount + "/" + kosdaqCount, "KOSPI/KOSDAQ")}
    </div>`,
    `<div class="subgrid">
      ${legSection("KOSPI LONG", kospi.long_leg || [], "long_score", "pred_prob_top20", "상승확률")}
      ${legSection("KOSPI SHORT", kospi.short_leg || [], "short_score", "pred_prob_bottom20", "하락확률")}
      ${legSection("KOSDAQ LONG", kosdaq.long_leg || [], "long_score", "pred_prob_top20", "상승확률")}
      ${legSection("KOSDAQ SHORT", kosdaq.short_leg || [], "short_score", "pred_prob_bottom20", "하락확률")}
    </div>`,
    `<p class="muted">${esc(data.disclaimer || "롱·숏 추천은 정보제공용 모의 시뮬레이션입니다.")}</p>`,
  ].join("");
}

function inlineMetric(label, value, sub = "") {
  return `<div class="inline-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><p class="muted">${esc(sub)}</p></div>`;
}

function legSection(title, items, scoreKey, probabilityKey, probabilityLabel) {
  return `<section class="subsection">
    <h3>${esc(title)}</h3>
    ${table(items || [], [
      { key: "rank", label: "#" },
      { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
      { key: scoreKey, label: "Score", format: num },
      { key: "pred_return", label: "Return", format: pct },
      { key: probabilityKey, label: probabilityLabel, format: pct },
      { key: "confidence", label: "Confidence", format: pct },
    ])}
  </section>`;
}

function renderTomorrow(data) {
  const outlook = data.market_outlook || {};
  const range = data.long_short_range || {};
  const outlookItems = representativesByMarket(outlook.items || []);
  const rangeByMarket = Object.fromEntries(representativesByMarket(range.items || []).map((item) => [item.market, item]));
  const duplicateNotice = (outlook.items || []).length > outlookItems.length
    ? '<p class="muted">동일 시장 복수 전망 중 confidence가 높은 대표값을 표시합니다.</p>'
    : "";
  const cards = outlookItems.map((item) => {
    const exposure = rangeByMarket[item.market] || {};
    return `<section class="market-card">
      <h3>${esc(item.market)}</h3>
      <div class="row"><span>예상 수익률</span><b>${pct(item.expected_return)}</b></div>
      <div class="row"><span>예상 범위</span><b>${pct(item.range_low)} ~ ${pct(item.range_high)}</b></div>
      <div class="row"><span>상승 / 하락</span><b>${pct(item.up_probability)} / ${pct(item.down_probability)}</b></div>
      <div class="row"><span>쇼크 확률</span><b>${pct(item.shock_probability)}</b></div>
      <div class="row"><span>Confidence</span><b>${pct(item.confidence)}</b></div>
      <div class="row"><span>LONG 범위</span><b>${pct(exposure.long_low)} ~ ${pct(exposure.long_high)}</b></div>
      <div class="row"><span>SHORT 범위</span><b>${pct(exposure.short_low)} ~ ${pct(exposure.short_high)}</b></div>
    </section>`;
  }).join("");
  return [
    `<div class="row"><span>Status</span><b>${statusBadge(data.status)}</b></div>`,
    `<div class="row"><span>Target</span><b>${esc(data.next_trading_day || outlook.target_date || "-")}</b></div>`,
    duplicateNotice,
    cards || '<div class="empty">내일 예측 미수집: market_outlook_forecasts 생성이 필요합니다.</div>',
  ].join("");
}

function representativesByMarket(items) {
  const selected = {};
  for (const item of items || []) {
    const market = String(item.market || "-");
    const current = selected[market];
    if (!current || Number(item.confidence || 0) > Number(current.confidence || 0)) {
      selected[market] = item;
    }
  }
  const ordered = [];
  for (const market of ["KOSPI", "KOSDAQ"]) {
    if (selected[market]) ordered.push(selected[market]);
  }
  for (const market of Object.keys(selected).sort()) {
    if (!["KOSPI", "KOSDAQ"].includes(market)) ordered.push(selected[market]);
  }
  return ordered;
}

function renderPremarketShadow(data) {
  const stages = data.stages || [];
  const sections = stages.map((stage) => {
    const items = representativesByMarket(stage.items || []);
    const cards = items.map((item) => `<section class="market-card">
      <h3>${esc(item.market)}</h3>
      <div class="row"><span>Ridge 총수익</span><b>${pct(item.ridge_expected_return)}</b></div>
      <div class="row"><span>Overnight gap</span><b>${pct(item.ridge_gap_return)}</b></div>
      <div class="row"><span>장중 수익률</span><b>${pct(item.ridge_intraday_return)}</b></div>
      <div class="row"><span>Gap + 장중</span><b>${pct(item.component_expected_return)}</b></div>
      <div class="row"><span>상승 확률 / 분류</span><b>${pct(item.up_probability)} / ${esc(item.classifier_direction)}</b></div>
      <div class="row"><span>Ridge 방향 / 일치</span><b>${esc(item.ridge_direction)} / ${item.direction_agreement ? "일치" : "불일치"}</b></div>
      <div class="row"><span>Risk gate / 현금</span><b>${esc(item.risk_gate_status || "-")} / ${pct(item.recommended_cash_ratio)}</b></div>
    </section>`).join("");
    const waiting = stage.stage === "final_premarket" && !stage.is_final_close
      ? "미국장 최종 종가가 확인되면 08:00 KST 최종 shadow 예측을 생성합니다."
      : "해당 stage 예측이 아직 생성되지 않았습니다.";
    return `<section class="subsection">
      <h3>${esc(stage.label || stage.stage)}</h3>
      <div class="row"><span>Status</span><b>${statusBadge(stage.status)}</b></div>
      <div class="row"><span>Target / cutoff</span><b>${shortDate(stage.prediction_date)} / ${esc(stage.prediction_cutoff || "-")}</b></div>
      <div class="subgrid">${cards}</div>
      ${cards ? "" : `<div class="empty">${esc(waiting)}</div>`}
    </section>`;
  }).join("");
  return [
    `<div class="inline-metrics">
      ${inlineMetric("운영 반영 비중", pct(data.production_weight), "shadow only")}
      ${inlineMetric("상태", data.status || "not_ready", "별도 검증 경로")}
    </div>`,
    sections,
    `<p class="muted">${esc(data.message || "검증 중인 shadow 예측입니다.")}</p>`,
  ].join("");
}

function renderMarketAnchorShadow(data) {
  const items = representativesByMarket(data.items || []);
  const constituents = data.constituents || {};
  const backtests = Object.fromEntries((data.backtests || []).map((item) => [item.market, item]));
  const cards = items.map((item) => {
    const members = constituents[item.market] || [];
    const gate = backtests[item.market] || {};
    const memberText = members.map((member) => `${member.name || member.symbol} (${pct(member.weight)})`).join(", ");
    return `<section class="market-card">
      <h3>${esc(item.market)}</h3>
      <div class="row"><span>q10 / q50 / q90</span><b>${pct(item.q10_return)} / ${pct(item.q50_return)} / ${pct(item.q90_return)}</b></div>
      <div class="row"><span>Conformal 80% 범위</span><b>${pct(item.calibrated_range_low)} ~ ${pct(item.calibrated_range_high)}</b></div>
      <div class="row"><span>Residual control</span><b>${pct(item.control_range_low)} ~ ${pct(item.control_range_high)}</b></div>
      <div class="row"><span>보정 표본 / 상태</span><b>${esc(item.calibration_count ?? 0)} / ${statusBadge(item.status)}</b></div>
      <div class="row"><span>Backtest gate</span><b>${statusBadge(gate.gate_status || "not_ready")}</b></div>
      <p class="muted">Anchors: ${esc(memberText || "동일 기준일 구성 미수집")}</p>
    </section>`;
  }).join("");
  const waiting = data.message || "앵커 구성과 756거래일 이상의 point-in-time 이력이 쌓이면 shadow 범위를 생성합니다.";
  return [
    `<div class="inline-metrics">
      ${inlineMetric("운영 반영 비중", pct(data.production_weight), "shadow only")}
      ${inlineMetric("상태", data.status || "not_ready", "production 전망과 분리")}
      ${inlineMetric("시장 수", items.length, "KOSPI/KOSDAQ")}
    </div>`,
    `<div class="subgrid">${cards}</div>`,
    cards ? "" : `<div class="empty">${esc(waiting)}</div>`,
    `<p class="muted">${esc(data.message || "앵커 기반 범위는 연구·정보제공용 shadow 결과입니다.")}</p>`,
  ].join("");
}

function renderMarketUsLinkageShadow(data) {
  const items = representativesByMarket(data.items || []);
  const runs = Object.fromEntries((data.runs || []).map((item) => [item.market, item]));
  const feedback = data.feedback || {};
  const cards = items.map((item) => {
    const run = runs[item.market] || {};
    const metrics = run.metrics || {};
    return `<section class="market-card">
      <h3>${esc(item.market)}</h3>
      <div class="row"><span>Baseline / Global</span><b>${pct(item.baseline_expected_return)} / ${pct(item.global_expected_return)}</b></div>
      <div class="row"><span>Global delta / weight</span><b>${pct(item.global_delta)} / ${pct(item.dynamic_overlay_weight)}</b></div>
      <div class="row"><span>Shadow 후보</span><b>${pct(item.candidate_expected_return)}</b></div>
      <div class="row"><span>Candidate 80% 범위</span><b>${pct(item.candidate_range_low)} ~ ${pct(item.candidate_range_high)}</b></div>
      <div class="row"><span>Gate / OOS</span><b>${statusBadge(run.gate_status || "not_ready")} / ${esc(metrics.oos_count ?? run.oos_count ?? 0)}</b></div>
    </section>`;
  }).join("");
  const correlations = data.correlations || [];
  return [
    `<div class="inline-metrics">
      ${inlineMetric("운영 반영 비중", pct(data.production_weight), "shadow only")}
      ${inlineMetric("Feedback", feedback.count ?? 0, `live ${feedback.live_count ?? 0}`)}
      ${inlineMetric("평균 weight", pct(feedback.mean_dynamic_overlay_weight), "최대 20%")}
    </div>`,
    `<div class="subgrid">${cards}</div>`,
    cards ? "" : `<div class="empty">${esc(data.message || "확정 국내 입력과 OOS 검증 이력을 기다리고 있습니다.")}</div>`,
    `<section class="subsection"><h3>다음 KRX 세션 상관성</h3>${table(correlations, [
      { key: "market", label: "Market" },
      { key: "signal", label: "Signal" },
      { key: "sample_count", label: "N" },
      { key: "pearson", label: "Pearson", format: num },
      { key: "recent_60d_pearson", label: "Recent 60D", format: num },
    ])}</section>`,
    `<p class="muted">${esc(data.disclaimer || data.message || "Nasdaq/SOX 후보는 production 시장 전망을 변경하지 않는 별도 검증 경로입니다.")}</p>`,
  ].join("");
}

function renderMarketNewsFeedLlmShadow(data) {
  const run = data.latest_run || {};
  const counts = data.counts || {};
  const gates = data.shadow || [];
  const ranges = data.market_range_shadow || [];
  return [
    `<div class="inline-metrics">
      ${inlineMetric("상태", data.status || "not_ready", run.model_name || "local only")}
      ${inlineMetric("평가 headline", counts.assessments || 0, `수집 ${counts.headlines || 0}건`)}
      ${inlineMetric("신호 일수", counts.signal_days || 0, "gate 최소 60일")}
      ${inlineMetric("운영 반영", String(data.production_weight ?? 0), "shadow only")}
    </div>`,
    `<section class="subgrid">
      <div class="subsection"><h3>2M/3M A/B Gate</h3>${table(gates, [
        { key: "horizon", label: "Horizon" },
        { key: "gate_status", label: "Gate", format: statusBadge },
        { key: "signal_days", label: "Signal Days" },
        { key: "mature_prediction_dates", label: "Mature Dates" },
        { key: "fail_reason", label: "Reason" },
      ])}</div>
      <div class="subsection"><h3>KOSPI/KOSDAQ Range</h3>${table(ranges, [
        { key: "market", label: "Market" },
        { key: "coverage_status", label: "Status", format: statusBadge },
        { key: "range_low", label: "Low", format: pct },
        { key: "range_high", label: "High", format: pct },
      ])}</div>
    </section>`,
    `<p class="muted">${esc(data.disclaimer || "Local LLM shadow status only. Headline text and summaries are intentionally not exported to GitHub Pages.")}</p>`,
  ].join("");
}

function renderLocalLlmNewsShadow(data) {
  const run = data.latest_run || {};
  const counts = data.counts || {};
  const shadow = data.shadow || {};
  const sources = data.sources || [];
  const items = shadow.items || [];
  const runs = shadow.runs || [];
  return [
    `<div class="inline-metrics">
      ${inlineMetric("상태", data.status || "not_ready", run.model_name || "local only")}
      ${inlineMetric("문서 / 평가", `${counts.documents || 0} / ${counts.assessments || 0}`, "캐시 재사용")}
      ${inlineMetric("신호 일수", counts.signal_days || 0, "gate 최소 60일")}
      ${inlineMetric("Token", `${run.input_tokens || 0} / ${run.output_tokens || 0}`, "입력 / 출력")}
    </div>`,
    `<section class="subgrid">
      <div class="subsection"><h3>출처 집계</h3>${table(sources, [
        { key: "source", label: "Source" },
        { key: "document_count", label: "Documents" },
        { key: "assessment_count", label: "Assessed" },
      ])}</div>
      <div class="subsection"><h3>KOSPI/KOSDAQ Gate</h3>${table(runs, [
        { key: "market", label: "Market" },
        { key: "signal_days", label: "Signal Days" },
        { key: "mature_prediction_dates", label: "Mature Dates" },
        { key: "gate_status", label: "Gate", format: statusBadge },
        { key: "fail_reason", label: "Reason" },
      ])}</div>
    </section>`,
    `<section class="subsection"><h3>최신 범위 후보</h3>${table(items, [
      { key: "market", label: "Market" },
      { key: "gate_status", label: "Gate", format: statusBadge },
      { key: "calibration_count", label: "Calibration" },
      { key: "feature_coverage", label: "LLM Coverage", format: pct },
      { key: "production_weight", label: "Production", format: pct },
    ])}</section>`,
    `<p class="muted">${esc(data.disclaimer || "집계 상태만 공개합니다. 원문, 요약, URL, 로컬 경로와 reasoning은 내보내지 않습니다.")}</p>`,
  ].join("");
}

function renderQuality(quality, validation) {
  const components = quality.components || {};
  const rows = Object.entries(components).map(([key, value]) => `<div class="row"><span>${esc(key)}</span><b>${statusBadge(value)}</b></div>`);
  const counts = validation.counts || {};
  const countRows = Object.entries(counts).map(([key, value]) => `<div class="row"><span>${esc(key)}</span><b>${esc(value)}</b></div>`);
  const messages = [...(validation.messages || []), ...(quality.messages || [])].slice(0, 6).map((message) => `<p class="muted">${esc(message)}</p>`);
  return [...rows, ...countRows, ...messages].join("") || '<div class="empty">품질 메시지 없음: 생성된 데이터가 정상 범위입니다.</div>';
}

function renderXImpact(top20, market) {
  const topItems = top20.items || [];
  const marketItems = market.items || [];
  const messages = [...(top20.messages || []), ...(market.messages || [])];
  if (!topItems.length && !marketItems.length) {
    const message = messages[0] || "X 뉴스 영향도 미생성: X_BEARER_TOKEN 설정 또는 build_x_news_impact_analysis.py 실행 필요";
    return `<div class="empty">${esc(message)}</div>`;
  }
  return [
    `<div class="inline-metrics">
      ${inlineMetric("Top20 영향 종목", topItems.length, shortDate(top20.asof_date))}
      ${inlineMetric("Top20 변경", top20.summary?.top20_changed ?? 0, "편입/제외 변화")}
      ${inlineMetric("최대 확률 변화", pct(top20.summary?.max_probability_delta || 0), "pred_prob_top20")}
      ${inlineMetric("시장전망 변화", marketItems.length, shortDate(market.asof_date))}
    </div>`,
    `<section class="subsection"><h3>Top20 영향도</h3>${table(topItems.slice(0, 20), [
      { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
      { key: "market", label: "Market" },
      { key: "rank_delta", label: "Rank Δ" },
      { key: "pred_prob_delta", label: "Prob Δ", format: pct },
      { key: "pred_return_delta", label: "Return Δ", format: pct },
      { key: "impact_level", label: "Impact", format: impactBadge },
    ])}</section>`,
    `<section class="subsection"><h3>KOSPI/KOSDAQ 범위 영향도</h3>${table(marketItems, [
      { key: "market", label: "Market" },
      { key: "horizon", label: "Horizon" },
      { key: "expected_return_delta", label: "Return Δ", format: pct },
      { key: "range_low_delta", label: "Low Δ", format: pct },
      { key: "range_high_delta", label: "High Δ", format: pct },
      { key: "up_probability_delta", label: "Up Prob Δ", format: pct },
      { key: "shock_probability_delta", label: "Shock Δ", format: pct },
      { key: "impact_level", label: "Impact", format: impactBadge },
    ])}</section>`,
  ].join("");
}

function impactBadge(value) {
  const level = String(value || "low");
  const cls = level === "high" ? "badge risk" : level === "medium" ? "badge warn" : "badge";
  return `<span class="${cls}">${esc(level)}</span>`;
}

function renderOfficialData(status, crawl) {
  const counts = status.counts || {};
  const marketNewsFeedHeadlines = status.marketnewsfeed_headlines || [];
  const crawlItems = crawl.items || [];
  const messages = [...(status.messages || []), ...(crawl.messages || [])].slice(0, 4);
  return [
    `<div class="inline-metrics">
      ${inlineMetric("Naver/Stock News", counts.news_articles ?? 0, "news_articles")}
      ${inlineMetric("MarketNewsFeed", `${counts.marketnewsfeed_site ?? 0}/${counts.marketnewsfeed_headlines ?? 0}/${counts.telegram_marketfeed ?? 0}/${counts.x_market_news_feed ?? 0}`, "site/headlines/tg/x")}
      ${inlineMetric("BIGKinds/GDELT", `${counts.bigkinds_issue_daily ?? 0}/${counts.global_news_events ?? 0}`, "issues/global")}
      ${inlineMetric("DART/Official Price", `${counts.dart_disclosures ?? 0}/${counts.official_market_prices ?? 0}`, "공시/공식시세")}
      ${inlineMetric("URL Crawl", counts.news_url_crawl_cache ?? crawl.summary?.count ?? 0, status.status || crawl.status || "-")}
    </div>`,
    messages.length ? messages.map((message) => `<p class="muted">${esc(message)}</p>`).join("") : "",
    table(marketNewsFeedHeadlines, [
      { key: "published_at", label: "UTC", format: (value) => esc(value ? String(value).replace("T", " ").slice(0, 19) : "-") },
      { key: "site_category", label: "Category" },
      { key: "importance", label: "Importance", format: (value) => esc(value == null ? "-" : `${value}/5`) },
      { key: "title", label: "MarketNewsFeed Shadow", format: (value, row) => `<a href="${esc(row.source_url || "#")}" target="_blank" rel="noopener noreferrer">${esc(value || "-")}</a>` },
      { key: "source_name", label: "Source" },
    ]),
    table(crawlItems.slice(0, 8), [
      { key: "domain", label: "Domain" },
      { key: "crawl_status", label: "Status" },
      { key: "title", label: "Title", format: (value, row) => `<a href="${esc(row.canonical_url || row.source_url || "#")}" target="_blank" rel="noopener noreferrer">${esc(value || row.source_url || "-")}</a>` },
      { key: "excerpt", label: "Excerpt", format: (value) => esc(value || "-") },
    ]),
  ].join("");
}

function renderNewsSignals(data) {
  const items = data.items || [];
  if (!items.length) return '<div class="empty">뉴스 신호 미수집: build_news_signal_features.py 실행 또는 공식 뉴스 설정이 필요합니다.</div>';
  return items.slice(0, 8).map((item) => `
    <div class="row">
      <span>${esc(item.scope === "market" ? "시장 전체" : item.symbol)}</span>
      <b>부정 ${pct(item.news_negative_attention_score)} · 편향보정 ${pct(item.news_bias_adjusted_sentiment_score)}</b>
    </div>
  `).join("");
}

function renderNews(items) {
  if (!items.length) return '<div class="empty">뉴스 미수집: NAVER_CLIENT_ID/SECRET 또는 공식 RSS 설정 필요</div>';
  return items.slice(0, 12).map((item) => {
    const href = item.link || item.originallink || "#";
    return `<article class="news-item">
      <a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(item.title || "-")}</a>
      <p class="muted">${esc(item.name || item.source_name || item.source || "-")} · ${shortDate(item.pub_date || item.query_date)}</p>
      <p>${esc(item.description || item.summary || "")}</p>
    </article>`;
  }).join("");
}

boot().catch((error) => {
  document.body.innerHTML = `<main><div class="panel"><h1>Dashboard load failed</h1><p>${esc(error.message)}</p></div></main>`;
});
