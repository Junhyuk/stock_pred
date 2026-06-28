function readInlineJson(id) {
  const element = document.getElementById(id);
  if (!element) throw new Error("missing snapshot: " + id);
  return JSON.parse(element.textContent || "{}");
}
function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return (Number(value) * 100).toFixed(1) + "%";
}
function num(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(3);
}
function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Math.round(Number(value)).toLocaleString();
}
function count(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "0";
  return Math.round(Number(value)).toLocaleString();
}
function num1(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(1);
}
function shortDate(value) {
  return value ? String(value).slice(0, 10) : "-";
}
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function errorState(label) {
  return `<div class="empty">${escapeHtml(label)}을 불러오지 못했습니다.</div>`;
}
function table(items, columns) {
  if (!items || items.length === 0) return '<div class="empty">아직 생성된 데이터가 없습니다.</div>';
  const head = columns.map(c => `<th>${c.label}</th>`).join("");
  const rows = items.map(item => `<tr>${columns.map(c => `<td>${c.format ? c.format(item[c.key], item) : (item[c.key] ?? "-")}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}
function metric(label, value, sub = "") {
  return `<article class="metric"><span class="muted">${label}</span><strong>${value}</strong><p class="muted">${sub}</p></article>`;
}
function compactText(parts) {
  return (parts || []).filter(part => part !== null && part !== undefined && String(part).trim() !== "" && String(part) !== "-").join(" · ");
}
function truncateText(value, maxLength = 500) {
  const text = String(value ?? "");
  return text.length > maxLength ? text.slice(0, maxLength - 1) + "…" : text;
}
function uniqueText(values, limit = 3) {
  return [...new Set((values || []).filter(Boolean).map(value => String(value)))].slice(0, limit);
}
function latestShortDate(items, key = "date") {
  const dates = (items || []).map(item => shortDate(item?.[key])).filter(value => value && value !== "-").sort();
  return dates.length ? dates[dates.length - 1] : "-";
}
function statusLabel(status) {
  const labels = { ready: "정상", partial_ready: "부분 준비", not_collected: "미수집", missing: "부족", stale: "지연" };
  return labels[String(status || "")] || String(status || "-");
}
function componentValue(value) {
  return statusLabel(value || "missing");
}
function directionLabel(value) {
  const labels = { UP: "상승", DOWN: "하락", FLAT: "중립", BULLISH: "강세", BEARISH: "약세", NEUTRAL: "중립" };
  return labels[String(value || "").toUpperCase()] || String(value || "-");
}
function stockLabel(value, row) {
  return escapeHtml(row?.name || value || row?.symbol || "-");
}
function symbolText(value) {
  return escapeHtml(value || "-");
}
