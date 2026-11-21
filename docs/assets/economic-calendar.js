/* Economic observations are separate from the snapshot used by each forecast. */
(function (root) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stamp = (value, zone = 'Asia/Seoul') => {
    if (!value) return '확인 불가';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '확인 불가';
    const parts = new Intl.DateTimeFormat('en-GB', {timeZone: zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).formatToParts(date);
    const p = Object.fromEntries(parts.map(({type, value: part}) => [type, part]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  };
  const source = row => {
    try { const url = new URL(row.source_url); return url.protocol === 'https:' ? `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">공식 출처</a>` : '출처 미확인'; }
    catch (_) { return '출처 미확인'; }
  };
  const countries = {US:'미국', KR:'한국', JP:'일본', EA:'유로존'};
  const measures = {policy_rate:'정책금리', cpi_headline_yoy:'소비자물가 전년비', unemployment_rate:'실업률', nfp_monthly_change:'비농업 취업자 월증감', core_pce_yoy:'근원 PCE 전년비'};
  function renderEconomicCalendar(data) {
    const now = data.asof || data.generated_at;
    const day = now ? new Date(now).toLocaleDateString('en-CA', {timeZone:'Asia/Seoul'}) : '';
    const events = (data.events || []).filter(row => !row.scheduled_date || row.scheduled_date >= day).sort((a,b) => String(a.scheduled_date || '9999').localeCompare(String(b.scheduled_date || '9999')));
    const eventRows = events.map(row => `<tr><td>${esc(countries[row.country] || row.country)}</td><td>${esc(row.title || row.event_type)}</td><td>${row.scheduled_at ? `${esc(stamp(row.scheduled_at))} KST` : `${esc(row.scheduled_date || '날짜 미정')} · 시간 미정`}</td><td>${source(row)}</td></tr>`).join('');
    const releases = (data.recent_releases || []).slice().sort((a,b) => String(b.available_at).localeCompare(String(a.available_at)));
    const actualRows = releases.map(row => `<tr><td>${esc(countries[row.country] || row.country)} ${esc(row.reference_period)}</td><td>${esc(measures[row.measure] || row.measure)} <strong>${esc(row.value)} ${esc(row.unit)}</strong></td><td>${row.vintage === 'current_snapshot' ? '현재 관측값' : row.vintage === 'revision' ? '수정값' : '관측값'}</td><td>${source(row)}</td></tr>`).join('');
    return `<h3>앞으로의 발표</h3><p class="credit-intro">예상치 없음</p><div class="economic-table"><table><thead><tr><th>지역</th><th>발표</th><th>예정 시각</th><th>출처</th></tr></thead><tbody>${eventRows || '<tr><td colspan="4">예정 자료 없음</td></tr>'}</tbody></table></div>
      <h3>최근 7일 수신한 실제 수치</h3><div class="economic-table"><table><thead><tr><th>지역·기준기간</th><th>값</th><th>버전</th><th>출처</th></tr></thead><tbody>${actualRows || '<tr><td colspan="4">수신된 실제 수치 없음</td></tr>'}</tbody></table></div>`;
  }
  if (typeof module !== 'undefined') module.exports = {renderEconomicCalendar};
  if (typeof document !== 'undefined') {
    fetch('data/economic_calendar.json', {cache:'no-store'}).then(r => {if (!r.ok) throw Error(); return r.json();})
      .then(data => {document.getElementById('economic-calendar-content').innerHTML = renderEconomicCalendar(data);})
      .catch(() => {document.getElementById('economic-calendar-content').textContent = '경제일정을 불러오지 못했습니다.';});
    fetch('data/economic_collection_status.json', {cache:'no-store'}).then(r=>r.json()).then(data=>{
      document.getElementById('economic-collection-status').textContent = `수집 ${stamp(data.finished_at)} KST · 일정 ${data.event_count ?? 0} · 수치 ${data.release_count ?? 0}`;
    }).catch(()=>{});
  }
})(typeof window !== 'undefined' ? window : globalThis);
