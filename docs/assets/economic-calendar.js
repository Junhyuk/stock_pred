/* Economic observations are separate from the snapshot used by each forecast. */
(function (root) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stamp = (value, zone = 'Asia/Seoul') => {
    if (!value) return '확인 불가';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '확인 불가' : date.toLocaleString('ko-KR', {timeZone: zone});
  };
  const source = row => {
    try { const url = new URL(row.source_url); return url.protocol === 'https:' ? `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">공식 출처</a>` : '출처 미확인'; }
    catch (_) { return '출처 미확인'; }
  };
  const countries = {US:'미국', KR:'한국', JP:'일본', EA:'유로존'};
  const statuses = {confirmed:'확정 일정', tentative:'잠정', cancelled:'취소', postponed:'변경·연기'};
  const measures = {policy_rate:'정책금리', cpi_headline_yoy:'소비자물가 전년비', unemployment_rate:'실업률', nfp_monthly_change:'비농업 취업자 월증감', core_pce_yoy:'근원 PCE 전년비'};
  function renderEconomicCalendar(data) {
    const now = data.asof || data.generated_at;
    const day = now ? new Date(now).toLocaleDateString('en-CA', {timeZone:'Asia/Seoul'}) : '';
    const events = (data.events || []).filter(row => !row.scheduled_date || row.scheduled_date >= day).sort((a,b) => String(a.scheduled_date || '9999').localeCompare(String(b.scheduled_date || '9999')));
    const eventRows = events.map(row => `<tr><td>${esc(countries[row.country] || row.country)}</td><td>${esc(row.title || row.event_type)}<br><small>${esc(statuses[row.status] || row.status)}</small></td><td>${row.scheduled_at ? `${esc(stamp(row.scheduled_at))} KST<br><small>${esc(stamp(row.scheduled_at,row.timezone))} ${esc(row.timezone)}<br>${esc(row.scheduled_at)} UTC</small>` : `${esc(row.scheduled_date || '날짜 미정')} · 시간 미정<br><small>${esc(row.timezone)}</small>`}</td><td>${source(row)}<br><small>첫 수신 ${esc(stamp(row.first_seen_at))} KST</small></td></tr>`).join('');
    const releases = (data.recent_releases || []).slice().sort((a,b) => String(b.available_at).localeCompare(String(a.available_at)));
    const actualRows = releases.map(row => `<tr><td>${esc(countries[row.country] || row.country)}<br>${esc(row.reference_period)}</td><td>${esc(measures[row.measure] || row.measure)}<br><strong>${esc(row.value)} ${esc(row.unit)}</strong><br><small>${esc(row.seasonal_adjustment || '조정 방식 미확인')}</small></td><td>${row.vintage === 'current_snapshot' ? '현재 관측값' : row.vintage === 'revision' ? '수정값' : row.original_publication_verified ? '초판 확인' : '초판 미검증'}<br><small>${esc(row.revision_of ? `수정 대상 ${row.revision_of}` : '')}</small></td><td>수신 ${esc(stamp(row.available_at || row.first_seen_at))} KST<br>원 발표 ${row.actual_release_at ? esc(stamp(row.actual_release_at)) + ' KST (출처 확인 범위)' : '시간 미확인'}<br>${source(row)}</td></tr>`).join('');
    const sources = (data.last_collection?.sources || []).map(row => `<li>${esc(row.source_id)}: ${esc(row.status)} ${esc(row.reason || '')}</li>`).join('');
    const state = data.numeric_state || {};
    const features = Object.entries(state.features || {}).map(([name,value]) => `<tr><td>${esc(name)}</td><td>${value == null ? '미확인' : esc(Number(value).toLocaleString('ko-KR',{maximumFractionDigits:4}))}</td></tr>`).join('');
    return `<p>현재 캘린더 관측: ${esc(stamp(now))} KST. 각 예측 카드의 입력 마감 시각에 알았던 일정과 다를 수 있습니다.</p>
      <p>향후 30일 · Fed/BOK/BOJ/ECB 금리, 물가·고용·미국 비농업 취업자. 날짜만 확인된 일정은 시간 미정입니다. <strong>시장 예상치 없음</strong> — 예상 대비 서프라이즈를 만들지 않습니다.</p>
      <h3>앞으로의 발표</h3><div class="economic-table"><table><thead><tr><th>지역</th><th>발표</th><th>예정 시각</th><th>근거</th></tr></thead><tbody>${eventRows || '<tr><td colspan="4">확인된 예정 자료 없음 · 발표가 없다는 뜻은 아닙니다.</td></tr>'}</tbody></table></div>
      <h3>최근 7일 수신한 실제 수치</h3><div class="economic-table"><table><thead><tr><th>지역·기준기간</th><th>값·원단위</th><th>버전</th><th>수신·출처</th></tr></thead><tbody>${actualRows || '<tr><td colspan="4">수신된 실제 수치 없음</td></tr>'}</tbody></table></div>
      <details><summary>공식 소스 접근·해석 상태</summary><ul>${sources || '<li>소스 상태 미확인</li>'}</ul></details>
      <details><summary>14개 수치 연구 후보: ${state.ready ? '현재 입력 확보' : '입력 부족'} (${(state.missing_features || []).length}개 미확인)</summary><p>정책금리 변화 bp · 물가/실업률 변화 %p · 미국 비농업 취업자 월증감 천명. 현재 관측값을 실제 수신 이후에만 사용합니다. 현재 입력 확보와 학습·검증 준비는 별개입니다.</p><table><tbody>${features}</tbody></table></details>
      <p><small>공식 수치도 수정될 수 있습니다. 첫 수신은 원 발표시각의 독립 인증이 아니며 현재 API 자료로 과거 초판을 재현했다고 주장하지 않습니다. 역사 검증과 252개 이상의 새 성숙 예측 및 독립 검수 전에는 승격하지 않습니다.</small></p>`;
  }
  if (typeof module !== 'undefined') module.exports = {renderEconomicCalendar};
  if (typeof document !== 'undefined') {
    fetch('data/economic_calendar.json', {cache:'no-store'}).then(r => {if (!r.ok) throw Error(); return r.json();})
      .then(data => {document.getElementById('economic-calendar-content').innerHTML = renderEconomicCalendar(data);})
      .catch(() => {document.getElementById('economic-calendar-content').textContent = '경제일정 관측 자료가 아직 없습니다. 누락을 일정 없음으로 해석하지 않습니다.';});
    fetch('data/economic_collection_status.json', {cache:'no-store'}).then(r=>r.json()).then(data=>{
      document.getElementById('economic-collection-status').textContent = `최근 수집 ${stamp(data.finished_at)} KST · ${data.status} · ${data.event_count ?? 0}개 일정 / ${data.release_count ?? 0}개 수치 · ${data.reason || '소스별 미확인 항목은 별도 표시'}`;
    }).catch(()=>{});
  }
})(typeof window !== 'undefined' ? window : globalThis);
