# 일일 연구 리포트 자동 갱신

이 문서는 2026-11-26에 갱신한 운영 계약입니다. 기본 브랜치 `master`의
`Morning research forecast` workflow와
[연구 화면·경제 캘린더](https://junhyuk.github.io/stock_pred/reliable-index-forecast.html#economic-calendar)를
함께 확인하세요. GitHub 예약 실행은 지연되거나 누락될 수 있어 정확한 시각의 갱신을
보장하지 않습니다. 각 시장의 기준 종가일, 입력 마감, 실제 공개 시각과 Actions 결과가 기준입니다.

## 오전 수집과 발행 시각

| 단계 | 한국 시각 | UTC cron | 동작 |
|---|---|---|---|
| `morning` | 월~토 08:00 시작 | `0 23 * * 0-5` | 실제 자료 수집·불변 준비 → 다음 거래일 연구 전망·검증 → Git·Pages 갱신 |

메인 화면은 이 예약으로 갱신합니다. `Premarket smoke`는 수동 검사입니다. 일요일은
예약에서 제외합니다. 토요일과 휴장일에는 마지막으로 완료된 종가를 기준으로 다음
거래일을 판정합니다. 한국과 미국의 개장 시각은 거래소 달력으로 각각 확인합니다.

08:00은 예약 시작이며 입력 마감과 공개 시각은 실제 시각을 기록합니다. 가격 SQLite,
미국 관측 CSV, 경제 SQLite, 모델 상태와 최신 종가 관측을 실제 수집 완료 후 고정합니다.
준비본의 날짜·해시·소스/설정 지문·완료 영수증을 검사하고 그 스냅샷 이후 받은 자료는
이번 예측에 넣지 않습니다. SQLite backup은 committed WAL까지 포함합니다. 가격·모델
파일은 동결 후 바뀌지 않으며 수신 시각을 그대로 기록합니다.

한국은 최신 완료 한국 거래일 종가로 다음 XKRX 거래일을 예측합니다. 미국은 당시 마지막
완료 미국 종가로 다음 XNYS 거래일을 예측하므로 한국과 기준일·대상일이 다를 수 있습니다.
각 목표 시장이 개장한 뒤에는 새 예측으로 발행하지 않습니다. 지연·부분 실패는 해당 시장의
이전 자료를 날짜와 함께 유지합니다. 완료 표식은 프로토콜·시장·대상 거래일을 구분하여
재시도와 중복 발행을 통제하고, 전환일의 기존 아침 완료 기록은 장후 실행을 막지 않습니다.

최신 종가는 예측 준비 상태와 별도로 공개합니다. 화면의 “예측 기준 종가”와 최신 종가의
거래일을 혼동하지 마세요. 최근 한국 종가는 네이버의 거래일별 관측을 수집하고 Yahoo
이력과 대조합니다. 완료 거래일·양수 유한값·OHLC를 검사하고 출처 간 값이 충돌하면
해당 시장 예측을 보류합니다. 출처와 확인 시각을 표시하며 거래소 공식 발표 시각을
확인하지 못한 자료에는 이를 만들어 넣지 않습니다. 정정 관측은 기존 원본과 발행본을
덮어쓰지 않는 별도 버전으로 기록합니다.

CFD는 예측 단계가 끝난 뒤 최대 2분 동안 조회하고, 금융투자협회 공개 시장 집계를
원문과 함께 참고용 차트에 표시합니다. 조회가 실패하면 마지막으로 검증된 잔고와
실제 기준일을 유지합니다. 종목별 원본은 공개 집계 파일에 포함하지 않습니다.

## 수동 실행과 공개 확인

수동 기본값은 비게시 `validate`입니다. 정기 예약과 같은 실시간 실행은 `morning`을 선택합니다.

| 모드 | 용도 | 작업 시간 상한 |
|---|---|---:|
| `validate` | 합성 통합·회귀 검사와 제한 시간의 공식 경제자료 수집, 예측·Pages 발행 없음 | 10분 |
| `morning` | 오전 수집·스냅샷·시장별 검증·발행 | 25분 |
| `postclose` | 기존 수동 장후 실행 | 25분 |
| `daily` / `prepare` / `predict` | 호환성을 위한 기존 수동 장전 경로. 원래08:00 경계를 유지 | 25분 / 6분 / 25분 |
| `latest` | 기존 실제시각 참고 경로, 시장별 발행 가능 시간 준수 | 25분 |

실제 발행 manifest의 `publication_checked_at`, run ID, 파일 체크섬과 각 시장의 마감을
확인합니다. CLI 정상 종료만으로 공개 성공을 판단하지 않습니다. 미발행·차단·실패는
성공 예측으로 집계하지 않습니다. 과거 시각을 지정한 재현은 retrospective이며 실제
전향 검증에서 제외합니다. 검증된 공개 자료만 허용 목록으로 Git에 반영하며 기본 브랜치가
전진해 push가 실패하면 강제 push하지 않습니다.

독립 종가 공개와 사용 가능한 전망 발행은 별개입니다. 모델 실행 오류는 운영 상태에
부분 실패로 기록하고, 주 모델과 연구 참고 모두 수치를 산출하지 못한 시장은 완료로
표시하지 않아 이후 입력 복구 시 재시도할 수 있습니다. 이미 검증된 카드를 재사용하는
부분 재시도는 원래 대상일·발행 시각·예측 식별자를 유지합니다.
Actions의 같은 실행 ID를 재실행해도 시도 번호별 입력 묶음을 별도로 고정합니다.
발행 마감 초과는 기존 검증된 예측·영수증을 덮어쓰지 않고 실패한 시도로 기록합니다.

Pages Source는 GitHub Actions여야 합니다. 토큰 push는 별도 push workflow를 자동 실행하지
않으므로 동일 workflow에서 push 성공 후 Pages를 배포합니다. 추적된 허용 문서만 복사하고
비공개 DB·전체 학습 아카이브를 포함하지 않습니다. 같은 실행의 Pages artifact 업로드와
조회 가능 상태를 확인한 다음 배포하며 Pages job은5분으로 제한합니다.

## 비공개 입력과 암호화 캐시

가격 DB·원시 CSV·원문 응답·전체 학습 행을 공개 Git이나 평문 Actions cache에 넣지 않습니다.
공개 저장소의 cache는 fork의 pull request에서도 접근될 수 있으므로, 캐시의 비밀성을
가정하지 않습니다. [GitHub의 cache 접근 안내](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)

저장소 Actions secret 이름은 `FORECAST_CACHE_KEY`입니다. 값은 32바이트 난수의
Base64 인코딩이며, 표준·URL-safe 형식과 한 개의 `=` padding 유무를 지원합니다
(43자 또는 44자). 잘못된 문자·공백·길이는 거부합니다. 키 값은 문서·로그·명령 인자·Git에
기록하지 않으며 기존 키를 임의로 회전하지 않습니다.

허용된 비공개 입력만 묶어 검증된 `cryptography`의 AES-256-GCM으로 암호화하고,
Actions cache에는 `.forecast-cache/state.enc`만 저장합니다. 키 누락·인증 실패·손상·
만료된 준비본은 실패로 처리하며 평문 대체 경로를 사용하지 않습니다. 개인 로컬 DB나
참가자 하위 프로젝트 자료는 이 묶음에 포함하지 않습니다. 기존 평문
`premarket-forecast-history-*` cache는 새 암호화 복원이 검증된 뒤 해당 workflow 소유
키만 정리하며, pip 등 다른 cache를 지우지 않습니다.

Cache는 없어질 수 있는 가속 수단입니다. 정규화한 경제 관측 이력, 전향 발행 장부,
훈련 행이 없는 검증된 모델 계수·변환 상태 및 실패 표식은 허용된 Git 경로에 영속 보존합니다.
고정 준비본이 없으면 최종 예측 단계에서 소급 재수집하지 않습니다. 이후 준비 단계에서
공개 가격 이력을 다시 받을 수 있어도 과거 수신 시각이나 수정 이력을 복원했다고 주장하지 않습니다.

## 경제자료와 모델 준비 상태

Fed·한국은행·일본은행·ECB의 정책 일정, 한국·미국·일본·유로존의 물가·고용 및 미국
비농업 취업자·PCE를 다룹니다. 화면은 향후 30일 일정과 최근 7일 수신한 실제 수치,
원단위·기준기간·수정 여부·출처·첫 수신 시각을 표시합니다. 현재 화면의 관측 자료와
각 예측의 입력 마감 당시 자료를 구분하세요.

소스 수집은 전체 120초의 외부 프로세스 제한, 내부 118초 예산, 요청당 최대 10초와
한 번의 재시도로 제한합니다. HTTP 성공이나 프로세스 종료를 완전 수집과 혼동하지 않고
`partial`·`unavailable` 및 미확인 항목을 표시합니다. 실패하면 직전 공개 화면을 보존하고
별도 시도 상태를 남깁니다. 키가 필요한 API나 해석하지 못한 문서는 미확인으로 남습니다.
한국은행의 공식 PDF/HWP 일정은 원문 URL·첨부 해시·실제 수신 증거가 있는 정형 수동
가져오기로 보완할 수 있습니다. 공식 자료에 시각이 없으면 날짜만 저장하며 임의 발표 시각을 만들지 않습니다.

현재 API 수치는 실제 수신 이후의 관측값입니다. 원 발표 시각이나 초판을 검증하지
못한 자료를 과거 시점에 이용 가능했던 값으로 바꾸지 않습니다. 합법적으로 확보한
시장 예상치가 없으면 예상 대비 서프라이즈도 만들지 않습니다. 고정된 14개 수치 후보는
단위·정의·기간이 맞는 완전한 관측 이력과 기존 표본 기준을 만족해야 실제 학습됩니다.
자료 부족은 0으로 채우지 않고 준비 미완료로 표시합니다.

주 모델은 2019-01-02부터 시장별 예정 거래소 세션 순번에 따라 21세션 간격으로
학습 상태를 고정합니다. 결측이나 지연이 과거 기준일을 이동시키지 않습니다. 도입 당시의
과거 실제 모델 상태가 없으면 주 모델은 `not_ready`이며, 현재 자료로 과거 상태를
만들어내지 않습니다. 화면의 기존 가격모형 참고 결과는 별도의 retrospective 역할입니다.
다음 학습 기준 종가일과 실제 포착·발행 시각도 구분합니다. 한국은 해당 정기 학습
완료 종가 이후 첫 적격 월~토 08:00 실행에서 포착합니다. 기존 장후 및 장전 수동 경로는
각자의 발행 창을 사용합니다.
실패한 실제 정기 학습 기준일은 다음 정기 기준일까지 실패 상태를 유지합니다.

변동성 보정 구간·이벤트별 구간·14개 수치 후보는 기존 가격 예측과 분리한 연구 후보입니다.
이미 열람한 과거 terminal 결과는 탐색 자료이며 독립 정확도 증거가 아닙니다. 검증된 실제
발행 시각과 성숙한 첫 관측 정답만 전향 장부에 기록하고, 주 모델·참고 예측·수치 후보의
분모를 분리합니다. 최소 252개의 새로운 성숙 예측과 독립 검토 전에는 승격하지 않으며,
그 표본 수를 채웠다고 자동 승인하지도 않습니다. `production_weight=0`을 유지합니다.

장후 프로토콜 `after_close_daily_v1`은 장전 모델 파일·보정 자료·성과와 분리됩니다.
과거 OOF는 한국 종가일18:00, 미국 종가 이후 첫 적격 한국시간 평일18:00 기준으로
다시 계산하며 이를 실제 과거 발행 기록으로 주장하지 않습니다. 화면은 주 모델과
새 자료로 재계산한 연구 참고 전망을 구분하고, 참고 전망의 회고 학습·검증 미통과 사유를
표시합니다. 확률 보정이나 입력 조건이 부족하면 수치를 임의로 채우지 않습니다.
전향 성적은 발행 당시 고정한 기준 종가와 목표 거래일의 첫 관측 종가를 비교합니다.
정정 전 가격이나 다른 역할·프로토콜의 기준가로 수익률을 대신 계산하지 않습니다.
발행 가격 기준을 확인할 수 없는 새 예측은 성적 계산에서 보류합니다.

## 비용과 측정의 범위

이 workflow는 공개 저장소의 표준 `ubuntu-latest` runner만 사용하고 유료 larger runner나
저장 용량 증액을 사용하지 않습니다. 공개 저장소의 표준 runner compute는 무료이며,
artifact·Packages 공유 저장량과 별도 cache 저장량은 따로 확인해야 합니다. 다른 저장소를
포함한 계정 전체 사용량이나 청구 여부는 이 저장소의 측정만으로 보장할 수 없습니다.
[GitHub Actions 결제 정책](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

연속 30일에는 평일이 최대 22일입니다. 수집·예측을 합친 job 상한 25분과
같은 실행의 Pages 상한 5분을 모두 소비하는 보수적 상한은
`22×(25+5)=660 runner-minutes`입니다. 31일 달력월은 최대 23평일로
`23×(25+5)=690 runner-minutes`입니다. 별도 예약 smoke는 없습니다.
이는 저장소의 예약 실행 상한이며 실제 사용량·청구액이 아닙니다. 문서 수동 push의
기존 Pages 작업은 회당 최대 5분이며 수동 실행·재시도는 실제 횟수만큼 추가합니다.
OOF와 검증 상태 artifact의 보관은 14일입니다. Cache 퇴출과 저장량 제한이 있으므로
실제 실행 횟수·job 시간·artifact/cache/모델 상태 크기와 30일 추정치를 함께 검수합니다.
[Cache 용량과 퇴출 정책](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy)

아래는 **2026-11-13의 이전 단일 실행 방식**에서 측정한 기록입니다. 현재 운영의
실측이나 현재 계정 저장량을 뜻하지 않습니다.

| 당시 측정 항목 | 기록 |
|---|---:|
| OOF artifact 한 개 | 5,201,975 bytes |
| 동일 크기를 매일 14개 보관한 단순 추정 | 약 72.8MB |
| Pages artifact | 약 0.25MB |
| 저장소 cache 전체, 대부분 pip | 104,834,962 bytes |
| 수동 실행 `43431083091`, Pages까지 경과 | 9분 42초 |
| 한국 예측 / 미국 예측 | 6분 18초 / 2분 19초 |

당시 300행 합성 fixture에서 중복 변환·추론 제거 전후 OOF/평가 JSON은 같았고,
3회 중앙값 평가 시간은 5.796초→5.233초였습니다. 이는 당시의 합성 성능 기록이며
현재 일일 속도나 시장 정확도 향상을 보장하지 않습니다.

2026-11-14의 전체 로컬 회귀 검수는 542개 통과, 기존 paused 하위 프로젝트의
schema 부재 오류 1개, 건너뜀 1개였습니다. 이후 출처별 저장 격리 보완은 관련
저장소·자동화 회귀 20개와 독립 실패 사례 검사로 별도 확인했습니다. 별도 합성 장기 양성 경로는 2,219세션·
1,554개 경제 관측에서 실제 수치모형을 계산했으며 수치 경로 39.11초, 최대 RSS
439,292KiB였습니다. 이는 구현·로컬 자원 검증이며 GitHub runner의 실시간이나 시장
예측력 증거가 아닙니다. 실제 Actions 실행·암호화 복원·공개 결과·Pages·예산은 별도로 확인합니다.

## 2026-11-14 KST actual-operation snapshot

The fixed Actions window ends at 2026-11-13 17:43:16 UTC. Run [43473230792](https://github.com/Junhyuk/stock_pred/actions/runs/43473230792) succeeded and deployed Pages.

| Measurement | Observed |
|---|---:|
| 30-day repository Actions usage | 42 runs, 81 job records, 4,163 seconds / 69.38 runner-minutes; 130 per-job rounded minutes |
| Scheduled monthly cap | 1,286 minutes; manual retries and other Pages pushes are additional; account-wide usage remains unknown |
| Cache | 6 entries, 388,544,655 bytes; four encrypted private entries and two setup-python entries; zero owned legacy plaintext entries |
| Unexpired artifacts | 25 entries, 33,168,655 bytes; 8 OOF archives total 28,754,557 bytes |
| Latest OOF archive | 5,625,181 bytes; one same-sized archive per day for 14 days projects to 78,752,534 bytes (not a cap or both-market guarantee) |

The run restored encrypted state, collected 89 events and 25 releases with partial-source status, and prepared KOSPI/KOSDAQ from 2026-11-13 for target 2026-11-16. A checksum-verified **research reference** was published at 02:37:30 KST; the main model remains not-ready and there are no forward-scored forecasts yet. The US market was open, so its prior verified 2026-11-12 reference was retained. The live page is [reliable-index-forecast.html](https://junhyuk.github.io/stock_pred/reliable-index-forecast.html). JPEG render checks and sanitized per-job measurements are retained only under ignored `logs/accuracy-calendar-20261113/`.

## 2026-11-14 자동화 사고 정정

- Prepare [#15](https://github.com/Junhyuk/stock_pred/actions/runs/43522635869)는 가격 갱신 중 미국 immutable CSV 충돌로 실패했습니다.
- [#16](https://github.com/Junhyuk/stock_pred/actions/runs/43525093140)은 성공했지만 준비 manifest가 없어 한국·미국 예측을 건너뛰었습니다.
- 수정 `ce7cc33`은 날짜와 수집 시각별 스냅샷을 사용하며 기존 최초 관측값과 시각을 보존합니다.
- #18은 가격 갱신 후 스냅샷의 초·소수초 ISO 시각 파싱 오류로 중단되어 발행하지 못했습니다.
- 수정 `0d574fe`는 시각을 엄격한 ISO8601 UTC로 복원하고 필수 시각 누락을 거부합니다.
- CLI 모의 검증 24개와 시각 복원 회귀 4개가 통과했습니다. 전체 검증은 592개 통과·기존 하위 프로젝트 schema 누락 1개 실패·1개 건너뜀이었습니다.
- 실제 branch validate [#19](https://github.com/Junhyuk/stock_pred/actions/runs/43543433301)는 171개 통과(135.21초)했습니다.
- 실제 latest [#20](https://github.com/Junhyuk/stock_pred/actions/runs/43543940428)의 forecast job은 성공했고 KOSPI·KOSDAQ·US 입력 기준일 2026-11-13과 목표일 2026-11-16의 검증 영수증을 기록했습니다. 산출물은 research-only/not-ready(생산 가중치 0)이며 공개 데이터 commit은 `365464e`입니다.
- 첫 Pages 배포는 artifact `14093901236` 업로드 뒤 metadata 조회에서 실패했습니다. 두 workflow에 동일 artifact ID/name을 최대 60초 동안 Twirp Results API로 확인하는 attempt별 배포 대기 단계를 추가했으며, Node 13개와 관련 52개 검증을 통과했습니다.
