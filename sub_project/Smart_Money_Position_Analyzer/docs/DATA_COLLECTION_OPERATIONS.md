# 데이터 확보 및 실행 운영 Runbook (2026-10-23 기준)

## 먼저 라이선스 확인
KRX OPEN API 사용승인, KRX 투자자별 CSV 조회·활용 조건, DART OpenAPI 키, SEC fair access, FINRA 데이터 사용조건, CFTC 역사자료 사용조건을 사전에 확인한다. 데이터 조회 가능 여부와 **기계 수집·재배포 가능 여부는 별도**이다. 약관이 애매하면 수동으로 허용된 CSV를 이용한다.

## 우선순위와 실제 가능한 경로

| 자료 | 확보 | 일/주/분기 | 자동화 | 주의 |
|---|---|---|---|---|
| KRX 일별 시세 | `KRX_AUTH_KEY`, `fetch-krx` | 일 | 승인 API | 외국인 순매수 미포함 |
| KRX 투자자별 순매수 | 웹 내 정식 다운로드 → `import-csv kr-flow` | 일 | 수동 우선 | CSV 헤더·수량 단위 검사 |
| KRX 외국인 보유율 | 공식 다운로드 → `import-csv kr-ownership` | 일 | 수동 우선 | 보유율 pp vs % 구분 |
| DART 대량보유 | `DART_API_KEY`, `fetch-dart` | 사건 | 승인 API | 공시일≠거래일 |
| SEC 13F | 공식 ZIP URL → download-sec13f → import-sec13f | 분기 | 다운로드 가능 | 실제 공개지연, CUSIP 확인, 정정신고 |
| FINRA Reg SHO | `fetch-finra` | 일 | 공개 파일 | 공매도 **거래량** |
| CFTC 금융선물 TFF | `fetch-cftc` | 주 | 공식 ZIP | 지수선물 단위 |
| 공개 HTML 표 | `web-table` + 검증 후 import | 변동 | 권한확인 후 | HTTPS, robots, 정적 table 한정 |

## 단위: 데이터 품질의 핵심
- `net_qty`: **주**, `net_value`: **원**; 다운로드 `천주`, `백만원` 표시는 수집 전에 1000 또는 1,000,000배 정규화.
- KRX `ownership_pct`: 퍼센트값, 예: 54.2 (0.542 아님). 차이는 pp.
- SEC 13F `VALUE`: 2023-01-03부터 USD(원금가치가 아닌 시가평가), 구 제출분 천 USD. `reported_value_raw`, `value_unit`, `reported_value_usd` 컬럼 유지.
- CFTC: 계약 수량은 주식 수량이 아니며 승수 없는 계약대금 계산 금지.
- 파일에 종목명만 있고 코드가 없으면 다른 회사로 잘못 연결할 수 있으므로 공식 종목코드 매핑을 검증.

## 안전한 웹 테이블 파싱
- robots 확인 실패: 자동화 중단하고 공식 CSV를 사용.
- 웹사이트 이용약관상 자동화/다운로드 허용과 서버 부하 제한을 먼저 확인.
- 공개 HTML table만 지원: JS로 생성한 데이터, 캡차, 암호 인증, 비공개 네트워크 호출 등은 사용하지 않음.
- 처음에는 `web-table`로 CSV 추출만 하고 사람이 헤더·단위·행 갯수를 확인한 다음 `import-csv`로 반입 권장.
- 사이트 파싱 중 잘못된 행 발생 시 수치 채움/보간 없이 파서 오류 처리.

## PIT(Time Travel) 검증
`trade_date`(시장 기준일) 또는 `report_period`와 `available_at`(공개정보를 관측 가능한 시점)은 다르다. 확인 가능한 개별 공시 수신 시각이 없으면 `available_at=실제 수집 시각`으로 보수적으로 기록하여 과거 기준일 분석에서 제외한다. 13F는 제출일 다음 UTC 날짜부터 사용 가능하도록 보수적으로 표시하지만 실제 공개시각이 검증되어야 정밀 분 단위 백테스트가 가능하다.

## 운영 자동화 가이드 (설치자가 선택)
별도 `.env`/secret manager를 설정하고 cron/launchd로 **영업일 확정 이후** 실행; 소스별 최대 1회/일. `ingest_runs.status`와 `quality_issues`를 점검하고, 실패 시 중복 요청 폭주를 방지하는 백오프를 사용. 본 ZIP에는 자동 스케줄을 기본 활성화하지 않는다.
