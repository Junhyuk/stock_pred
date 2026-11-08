function pct(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function num(value, digits) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function signalLabel(signal) {
  if (signal === "LONG") return "상승 기울기 (매수 지시 아님)";
  if (signal === "SHORT") return "하락 기울기 (매도 지시 아님)";
  if (signal === "NEUTRAL") return "중립";
  return "판단 보류";
}

function renderStatus(payload) {
  const info = payload.data_freshness || {};
  const status = document.getElementById("runStatus");
  status.textContent = `종가 ${info.latest_asof || "없음"} · ${payload.status || "unknown"} · 파일 작성 ${payload.generated_at || ""}`;
  const freshness = document.getElementById("freshness");
  const stale = info.stale_vs_calendar
    ? `마지막 확정 종가 ${info.latest_asof || "없음"} 기준입니다. 이 날짜는 가장 최근 거래일 종가보다 이릅니다.`
    : `마지막 확정 종가 ${info.latest_asof || "없음"} 기준입니다. 가장 최근 거래일 종가와 같습니다.`;
  freshness.textContent = `${stale} ${info.note || ""} ${payload.method_note || ""}`.trim();
  document.getElementById("disclaimer").textContent = payload.disclaimer || "";
}

function renderSummary(payload) {
  const leaderboard = payload.leaderboard || [];
  const ready = (payload.sequential7 || {}).ready_indexes || [];
  const missing = (payload.sequential7 || {}).not_ready_indexes || [];
  const top = leaderboard[0];
  document.getElementById("summary").innerHTML = [
    metric("평가 기간", `${payload.eval_years || 3}년 OOS`),
    metric("데이터 있는 지수", ready.join(", ") || "없음"),
    metric("not_ready", missing.join(", ") || "없음"),
    metric("최고 방향 적중", top ? `${top.slot} ${pct(top.direction_hit)}` : "—"),
  ].join("");
}

function metric(label, value) {
  return `<article class="metric"><span>${label}</span><strong>${value}</strong></article>`;
}

function renderCards(payload) {
  const root = document.getElementById("cards");
  root.innerHTML = (payload.indexes || []).map((card) => {
    const boxes = Object.values(card.horizons || {}).map((block) => horizonBox(block)).join("");
    return `
      <article class="panel">
        <h2>${card.korean_name} <span class="tiny">${card.display_name} · pykrx ${card.pykrx_code || "없음"}</span></h2>
        <p class="tiny">상태 ${card.status}${card.reason ? ` · ${card.reason}` : ""} · 종가 ${card.last_close ?? "—"} · 기준일 ${card.asof_date || "—"}</p>
        <div class="horizon-split">
          ${boxes}
        </div>
      </article>`;
  }).join("");
}

function horizonBox(block) {
  const forecast = block.forecast;
  const champion = block.champion || {};
  if (!forecast && block.status === "not_ready") {
    return `<div class="horizon-box"><h3>${block.title || ""}</h3><p class="empty">${block.reason || "데이터 없음. 구성종목으로 지수를 만들지 않습니다."}</p></div>`;
  }
  const pUp = forecast ? pct(forecast.p_up) : "—";
  const pDown = forecast ? pct(forecast.p_down) : "—";
  const gate = champion.gate_status || block.status || "—";
  const rejected = gate === "rejected" || block.status === "rejected";
  const probability = forecast && forecast.p_up == null
    ? "상승 확률은 추정하지 않습니다"
    : `상승 확률 ${pUp} · 하락 확률 ${pDown}`;
  return `
    <div class="horizon-box">
      <h3>${block.title || ""}</h3>
      <div class="inline-metric"><span>우승 모델</span><strong>${champion.model_type || "—"} / ${champion.group || "—"}</strong></div>
      <div class="inline-metric"><span>3년 방향 적중</span><strong>${pct(champion.metrics?.direction_hit)}</strong></div>
      <div class="inline-metric"><span>3년 MAE</span><strong>${num(champion.metrics?.mae, 5)}</strong></div>
      <div class="inline-metric"><span>예상 수익률</span><strong>${forecast ? pct(forecast.expected_return) : "—"}</strong></div>
      <div class="inline-metric"><span>80% 범위</span><strong>${forecast ? `${pct(forecast.range_low)} ~ ${pct(forecast.range_high)}` : "—"}</strong></div>
      <p class="prob">${probability} · ${signalLabel(forecast?.signal)}${rejected ? " · 검증 미통과" : ""}</p>
      <p class="tiny">게이트 ${gate} · 연구 예측이며 수익을 보장하지 않습니다.</p>
    </div>`;
}

function renderTable(id, headers, rows) {
  const el = document.getElementById(id);
  if (!rows.length) {
    el.innerHTML = `<p class="empty">표시할 행이 없습니다.</p>`;
    return;
  }
  el.innerHTML = `<table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function renderLeaderboard(payload) {
  const rows = (payload.leaderboard || []).map((item) => `
    <tr>
      <td>${item.slot}</td>
      <td>${item.model_type}</td>
      <td>${item.group}</td>
      <td>${pct(item.direction_hit)}</td>
      <td>${num(item.mae, 5)}</td>
      <td>${item.n ?? "—"}</td>
      <td>${item.gate_status}</td>
    </tr>`);
  renderTable("leaderboardTable", ["슬롯", "모델", "입력군", "방향 적중", "MAE", "OOS N", "게이트"], rows);
}

function renderCompare(payload) {
  const rows = [];
  for (const card of payload.indexes || []) {
    for (const [horizon, block] of Object.entries(card.horizons || {})) {
      for (const item of block.champion?.recent_actual_vs_pred || []) {
        rows.push(`
          <tr>
            <td>${card.index_code}</td>
            <td>${horizon}</td>
            <td>${item.asof_date}</td>
            <td>${pct(item.expected_return)}</td>
            <td>${pct(item.actual_return)}</td>
            <td>${item.direction_hit ? "적중" : "불일치"}</td>
          </tr>`);
      }
    }
  }
  renderTable("compareTable", ["지수", "horizon", "예측일", "예측 수익률", "실제 수익률", "방향"], rows.slice(-24));
}

function renderModels(payload) {
  const rows = [];
  for (const card of payload.indexes || []) {
    for (const [horizon, block] of Object.entries(card.horizons || {})) {
      for (const item of block.models || []) {
        rows.push(`
          <tr>
            <td>${card.index_code}</td>
            <td>${horizon}</td>
            <td>${item.model_type}${item.champion ? " ★" : ""}</td>
            <td>${item.group}</td>
            <td>${pct(item.direction_hit)}</td>
            <td>${num(item.mae, 5)}</td>
            <td>${num(item.coverage, 3)}</td>
            <td>${item.n ?? "—"}</td>
          </tr>`);
      }
    }
  }
  renderTable("modelsTable", ["지수", "horizon", "모델", "입력군", "방향 적중", "MAE", "80% 커버", "N"], rows);
}

function renderChecklist(payload) {
  const items = payload.sequential7?.items || [];
  document.getElementById("checklistTable").innerHTML = items.map((item) => `
    <div class="row">
      <span><b>${item.agent}</b> — ${item.check}</span>
      <b class="${item.pass ? "pass" : "fail"}">${item.pass ? "PASS" : "FAIL"}</b>
    </div>`).join("");
}

async function main() {
  try {
    const response = await fetch("data/index_forecast.json", { cache: "no-store" });
    if (!response.ok) throw new Error("index_forecast.json missing");
    const payload = await response.json();
    renderStatus(payload);
    renderSummary(payload);
    renderCards(payload);
    renderLeaderboard(payload);
    renderCompare(payload);
    renderModels(payload);
    renderChecklist(payload);
  } catch (error) {
    document.getElementById("runStatus").textContent = "not_ready";
    document.getElementById("freshness").textContent = `리포트를 불러오지 못했습니다: ${error.message}`;
  }
}

main();
