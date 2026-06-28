function bootMarketUpDown() {
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "2M";
  try {
    renderMarketUpDown(readInlineJson("up-down-" + horizon));
  } catch (_) {
    document.getElementById("marketUpDownMetrics").innerHTML = errorState("상승·하락 추천");
  }
}
function selectMarketUpDownHorizon(button) {
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootMarketUpDown();
}
function renderMarketUpDown(data) {
  const markets = data.markets || {};
  const kospi = markets.KOSPI || { upside: [], downside: [] };
  const kosdaq = markets.KOSDAQ || { upside: [], downside: [] };
  document.getElementById("marketUpDownMetrics").innerHTML = [
    metric("기준일", shortDate(data.asof_date), data.horizon || "-"),
    metric("KOSPI 상승", (kospi.upside || []).length, "목표 6"),
    metric("KOSPI 하락", (kospi.downside || []).length, "목표 6"),
    metric("KOSDAQ 상승", (kosdaq.upside || []).length, "목표 4"),
    metric("KOSDAQ 하락", (kosdaq.downside || []).length, "목표 4")
  ].join("");
  document.getElementById("marketUpDownKospiUp").innerHTML = marketUpDownTable(kospi.upside || [], "long_score", "KOSPI 상승");
  document.getElementById("marketUpDownKospiDown").innerHTML = marketUpDownTable(kospi.downside || [], "short_score", "KOSPI 하락");
  document.getElementById("marketUpDownKosdaqUp").innerHTML = marketUpDownTable(kosdaq.upside || [], "long_score", "KOSDAQ 상승");
  document.getElementById("marketUpDownKosdaqDown").innerHTML = marketUpDownTable(kosdaq.downside || [], "short_score", "KOSDAQ 하락");
  document.getElementById("marketUpDownDisclaimer").textContent = data.disclaimer || "";
}
function marketUpDownTable(items, scoreKey, emptyLabel) {
  if (!items || items.length === 0) {
    return `<div class="empty">${escapeHtml(emptyLabel)} 추천이 아직 없습니다.</div>`;
  }
  return table(items, [
    { key: "rank", label: "#" },
    { key: "name", label: "종목", format: stockLabel },
    { key: "symbol", label: "코드", format: symbolText },
    { key: scoreKey, label: "Score", format: num },
    { key: "pred_return", label: "예측수익", format: pct },
    { key: "pred_prob_bottom20", label: "하락확률", format: pct },
    { key: "confidence", label: "신뢰도", format: pct },
    { key: "risk_flags", label: "리스크", format: value => escapeHtml(Array.isArray(value) ? value.join(", ") : (value || "-")) }
  ]);
}
document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("up-down-2M")) bootMarketUpDown();
});
