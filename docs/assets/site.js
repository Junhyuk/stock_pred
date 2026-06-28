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
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function statusBadge(status) {
  const value = String(status || "-");
  const cls = value === "ready" || value === "exported" ? "badge" : value.includes("error") ? "badge risk" : "badge warn";
  return `<span class="${cls}">${esc(value)}</span>`;
}

async function boot() {
  const [manifest, dashboard, today, tomorrow, top20, top50, longShort, moves, news, newsSignals, validation] = await Promise.all([
    load("manifest"),
    load("dashboard"),
    load("today"),
    load("tomorrow"),
    load("top20_upside_3M"),
    load("top50_3M"),
    load("long_short_2M"),
    load("market_move_explanations"),
    load("news_latest"),
    load("news_signal_latest"),
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

function renderQuality(quality, validation) {
  const components = quality.components || {};
  const rows = Object.entries(components).map(([key, value]) => `<div class="row"><span>${esc(key)}</span><b>${statusBadge(value)}</b></div>`);
  const counts = validation.counts || {};
  const countRows = Object.entries(counts).map(([key, value]) => `<div class="row"><span>${esc(key)}</span><b>${esc(value)}</b></div>`);
  const messages = [...(validation.messages || []), ...(quality.messages || [])].slice(0, 6).map((message) => `<p class="muted">${esc(message)}</p>`);
  return [...rows, ...countRows, ...messages].join("") || '<div class="empty">품질 메시지 없음: 생성된 데이터가 정상 범위입니다.</div>';
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
