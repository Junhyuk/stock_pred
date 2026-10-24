# Smart Money 재사용 설계와 최소 연동

기준: 2026-10-23 소스와 보관된 감사 증거. 사용자 추가 요청에 따라 **한국 투자주체 관측의 읽기 전용 bridge·고정 feature 후보·관측적 영향 보고를 구현했다.** 실제 데이터 확보와 예측력 증가는 별도 gate다.

## 1. 권고와 현재 범위

**서브프로젝트를 검증된 관측·출처·포지션 해석의 생산자로, 메인프로젝트를 지수 예측·확률 보정·독립 검증의 소비자로 둔다.** 최소 연결은 버전이 있는 JSON+manifest와 서브 SQLite의 읽기 전용 adapter로 구현했다. 서브의 표준 라이브러리 의존성 계약을 유지하고, 메인이 서브 내부 모델·DB 함수를 직접 import하거나 두 프로그램이 같은 관측 DB에 쓰는 구조는 채택하지 않는다.

메인의 이번 승인 범위는 **KOSPI/KOSDAQ 다음 거래일 지수 예측**이다. KOSPI 시가총액 상위 보통주 100개·KOSDAQ 50개는 입력 종목 범위다. 공식 KOSPI100 지수나 별도 KOSDAQ50 예측 대상으로 바꾸지 않는다. 이 연구 경로는 개별 종목별 수익률·목표가 예측을 출력하지 않는다. 시장 전체 투자자 수급은 별도의 지수 맥락 자료이고, 이 100/50개 종목 수급의 합계와 구분한다.

현재 서브의 실제 가격·수급·보유율 관측은 모두 0건이고, 10개 테스트는 합성 fixture 검증이다. KR 분석은 `FOREIGN`만 읽는다. 기관 CSV 저장은 가능하지만 기관 판정은 없고, 연기금 분류는 현행 스키마/importer에도 없다. 메인도 적격 PIT 구성종목·수급 이력이 없어 현재 가격 전용으로 실행된다. 두 지수의 신뢰도 gate는 `rejected`, production weight는 0이며, 데이터 재사용에 따른 예측력 증가는 측정되지 않았다. [원인 검토](../sub_project/Smart_Money_Position_Analyzer/docs/evidence/2026-10-23-positioning/root_cause_review.md), [메인 운영 계약](RELIABLE_FORECAST_RUNBOOK.md).

대안인 공유 DB는 초기 복사가 줄지만 소유권·수정 이력·장애 범위가 얽힌다. 공통 Python 패키지는 함수 재사용에 유리하지만 두 프로젝트의 배포와 의존성을 결합한다. 파일 계약과 읽기 전용 adapter는 cutoff별 재현과 독립 운영이 쉬워 채택했다. 미국 데이터 확대는 현재 범위에서 제외한다.

## 2. 실제로 재사용할 부분과 자격 조건

| 서브의 현재 구성요소·자료 | 소유자 | 메인 활용 | 현재 자격·필요 작업 |
|---|---|---|---|
| `normalize.py`, `importers.py`: 숫자·날짜·헤더 매핑, 주/원 수급, % 보유율 | 서브 데이터 담당 | 동일 자료를 두 번 해석하지 않는 관측 계약 | 재사용할 파싱 규칙이 있음. 천주/백만원 배율은 자동 감지하지 않으므로 원본 단위 검증·환산 기록 필요. 메인의 KST naive 시각 처리와 서브의 timezone 필수 처리가 달라 bridge는 명시적 offset만 허용. |
| `sources`, `ingest_runs`, `quality_issues`: 출처·URL·파일 SHA-256·적재 결과 | 서브 DB 담당 | 예측 run에서 원본·변환 과정을 추적 | 구조는 있음. 행별 단위·명시적 revision 이력은 없음. 원본 URL이 로컬 파일 URI와 별도로 남도록 manifest 확장 필요. |
| `prices`: 종목별 OHLCV·`traded_value`·`listed_shares` | 서브 관측 소유자 / 메인 가격 담당 | 중복 수집 감소, 적격 종목 breadth의 원자료 | 실제 0건. 시장·종목·거래일·원천·가격 조정 방식·기업행동·availability가 일치할 때만 공유. 조정 여부 필드가 없어 manifest 보완 전 조정가격으로 취급 금지. 종목 가격으로 메인의 `index_eod` 지수 종가를 대체하지 않음. 메인 보유 자료를 반대로 공급할 때도 같은 계약 적용. |
| `kr_investor_flows`: 종목별 외국인/기관/개인/기타 순매수 수량·금액 | 서브 데이터 담당 | 먼저 관측 설명, 이후 종목별 수급 강도 후보 | 실제 0건. 메인 `reliable_participant_flow`와 직접 호환되지 않음: 시장별 `net_value`, 동일 범위 `turnover`, `is_final`이 추가로 필요. 종목 부분집합 합계를 시장 전체 값으로 승격 금지. 기관 총계와 그 부분집합인 연기금을 합산하지 않음. |
| `kr_foreign_ownership`: 보유주수·보유율 | 서브 데이터 담당 | 외국인 수급 교차검증, 20일 보유율 변화 후보 | 실제 0건. `%`와 `pp` 구별, 기업행동·분모 변화·발표 지연·최신성 검증 필요. 기관/연기금 보유율 자료가 아님. |
| `signals.py`, `signal_snapshots`: 근거·상충·결측·cutoff·규칙 판정 | 서브 신호 담당 | 관측/추정/미확인 설명 | 기존 `--position-evidence`는 JSON 첨부 경로이며 학습 입력이 아님. 새 연동은 규칙 라벨을 학습하지 않고 실제 수급·보유율 관측으로 고정 연속 feature를 계산한다. `매집 정황`, `MEDIUM`은 보유 확정·보정된 상승확률이 아니다. |
| `dart_major_holdings`, DART collector | 서브 공시 담당 | 대형 구성종목의 공개 대량보유 사건 후보 | 구조·수집 경로만 있고 실제 0건. 공시일은 매매일이 아님. 현재 KR 판정은 이 테이블을 읽지 않는다. 검증된 8자리 corp_code↔6자리 종목코드, 정정·공개시각·기업행동과 사건 변환기를 별도로 마련해야 함. |
| `archives.py`: SEC 13F/CFTC, FINRA·공매도 구분 | 서브 해외 데이터 담당 | 원본 보존·단위·신고/기준일 분리 설계만 우선 참고 | 한국 지수의 즉시 입력 대상에서 제외. 13F 평가액은 순매수, FINRA short volume은 잔고, CFTC 시장 선물은 한국 개별종목 포지션으로 바꿀 수 없음. 추가 교차자산 연구는 별도 가설·검증 필요. |
| 메인 `reliable_inputs.py`·`reliable_pipeline.py` | 메인 ML/DB 담당 | 날짜별 구성종목 선별, 지수 라벨, WFO·보정·가격 baseline·불변 예측 아카이브 | 서브에서 재구현하지 않음. 서브는 자료를 제공하고 메인이 사용 자격과 예측 기여를 판단. |

서브의 `instruments`에는 `KR/US`와 현재 exchange만 있고, 과거 보통주·정상거래 상태·시가총액 구성 이력이 없다. 따라서 이 마스터나 현재 상위 목록을 메인의 역사적 100/50개 구성으로 사용할 수 없다.

## 3. 권고 파일 계약과 PIT 경계

서브 DB는 적재 후 정규화 관측의 관리 원본이다. 내려받은 원본 파일과 그 hash가 원천 증거이며, export는 **특정 시점·변환 버전의 불변 snapshot**이다. 메인의 반입 테이블은 이 snapshot의 연구용 사본으로 두고 원천을 수정하지 않는다. 예측값·라벨·보정기·채택 여부는 메인이 소유한다.

구현된 `smartmoney_bridge_v1`은 `observations`와 embedded `manifest`를 가진 JSON이다. `write_participant_bridge()`가 내용 hash·행수를 기록하고 동일 파일 재출력은 no-op, 같은 경로의 다른 내용은 실패한다. loader는 버전·내용 hash·행수를 검증한다. CSV 추가 export와 수정 이력 DB migration은 이후 작업이다.

구현 파일은 [participant_impact.py](../src/roboquant/index_forecast/participant_impact.py)다. `build_participant_impact_inputs(market, asof_cutoffs, sqlite_path/bridge_path, membership, stock_prices, index_history, target_count)`가 `features`, `feature_groups`, `report`, `bridge_manifest`를 반환한다. bridge가 있으면 이를 사용하고, 없으면 지정된 서브 SQLite를 `mode=ro`로 읽는다. 관측이 비어 있거나 자격이 없으면 `insufficient_data`와 빈 feature 그룹을 반환한다. 알려지지 않은 보유량·효과를 0으로 만들지 않는다.

| 계약 항목 | 필수 의미와 현재 열 매핑 |
|---|---|
| 식별·범위 | `source`, `source_url`, `market`, `symbol`, `investor_type`, `scope`. 서브 `source_code→source`, `trade_date/report_date→observation_date`. `market`은 KOSPI/KOSDAQ로 명시하고 `country=KR`을 별도 보관. 서브 `market=KR`을 임의로 KOSPI로 바꾸지 않는다. 시장 전체는 `scope=market_total`, `symbol=null`; 종목은 `scope=stock`, 6자리 symbol. |
| 값·단위 | `observation_date`, metric 이름, 값, `unit`, `currency`, 확정 상태. 주·KRW·%·pp를 구별한다. 메인 수급의 `turnover`는 같은 날짜·시장·모집단의 총거래대금이다. `buy_value+sell_value`를 임의로 denominator로 쓰지 않는다. |
| 시각·수정 | `available_at`, `ingested_at`, `revision`, `availability_basis`와 공개시각 증거. 시간은 offset을 가진 ISO8601로 고정. 실제 공개시각 미확인은 실제 수집/관측 시각을 사용하며 거래일로 소급하지 않는다. `available_at`과 `ingested_at` 둘 다 cutoff 이하여야 한다. 재반입으로 최초 관측 시각을 변경하지 않는 원천 보강은 별도 작업이다. |
| manifest | `schema_version`, export ID·생성시각, source DB/schema 버전, 파일·원본 SHA-256, 변환 코드/설정 hash, 날짜 범위·행수·결측률, 단위/조정 정책, 이용조건 확인 상태, 제외 사유. 해석 JSON에는 입력 hash·계산 버전·data cutoff와 observed/inferred/unverified·결측·상충을 포함. |
| 예측 연결 | 메인 `asof_date`는 입력 세션, `prediction_cutoff`는 정보 마감, `issued_at`은 실제 발행시각, `target_date`는 다음 KRX 세션이다. 관측 기준일이나 서브 `evaluated_at`으로 서로를 대체하지 않는다. 해석 계산에 사용한 모든 관측·수정본이 해당 cutoff에 적격이어야 한다. |

**행 자격:** 기준일 ≤ 입력 세션, `available_at`과 `ingested_at` ≤ `prediction_cutoff`, 원천·URL·단위·확정 상태·revision 식별을 모두 만족해야 한다. 모르는 값은 NULL로 보존한다. 외국인 보유율 파생값은 정확한 현재/t−20 비교 끝점만 사용해 stale 보유율을 carry-forward하지 않는다. 주가는 명시적 availability·가격 조정 정책·기업행동 검증 상태와 동일 종목 전체 coverage를 요구한다.

**구성 자격:** 날짜별 cutoff 이전에 공개된 완전한 시가총액 단면에서 보통주·정상거래 여부를 확인하고 우선주/ETF/ETN/SPAC/거래정지 종목을 제외한 뒤 100/50개를 고른다. 보유 데이터가 있는 종목만 추려 다시 순위를 매기지 않는다. 새 stock participant helper는 매 관측일의 정확한 날짜별 단면을 요구해, 두 날짜의 목록으로 중간 기간을 채우지 않는다. 서브의 `market=KR`은 이 검증된 구성에 들어가는 종목에 한해서만 KOSPI/KOSDAQ로 연결된다. 현재 exchange나 `KR`만 보고 추정하지 않는다.

**수급 경계:** 기존 메인 시장 전체 helper의 3개 군 완전패널 계약과 별도로, 새 helper는 시장 전체/적격 종목 부분집합과 외국인/기관/연기금을 각각 독립 판정한다. `participant_{market|stock}_{actor}_raw`는 적격 20일에서 1/5/20일 순매수금액÷동일 범위 거래대금을 계산한다. 서브의 외국인 종목 두 개를 시장 전체로 올리지 않는다. 외국인 `..._stock_foreign_position`은 raw 값에 관측 보유율 20일 변화(pp)·순매수 양수일 비율을 추가한다. 기관·연기금의 실제 보유 데이터나 포지션 판정은 구현하지 않았고, 없으면 해당 그룹을 제외한다.

**중복·revision:** 현재 서브는 자연키 UPSERT와 파일 hash 기록이 있으나 같은 키 수정 시 이전 값을 덮고 재반입 시 ingestion metadata도 바뀐다. 완전한 수정 이력은 구현되어 있지 않다. adapter는 현재 행 hash를 식별자로 남기되 진짜 revision 역사라고 주장하지 않고, finality·가격조정 metadata 부재도 보고한다. 따라서 실제 서브 행이 생겨도 확인되지 않은 필드를 자동으로 인증하지 않는다. bridge는 revision별 immutable 파일을 보존하고 cutoff 이전의 실제 이용가능 수정본만 사용한다. 원천 UPSERT를 이력 보존형으로 바꾸는 migration은 이후 필요하다.

## 4. 작은 단위의 시작 경로와 제안 작업

1. **기존 기능으로 실제 관측부터 확보 — 서브 데이터 담당, 운영 실행 Luna 6 xhigh.** 정식 KRX CSV를 허용된 방식으로 수동 확보하고 삼성전자·SK하이닉스 두 종목부터 원본·URL·단위·실제 다운로드 시각을 보관한다. 원본 배포 DB는 보존하고 새 연구 DB를 사용한다. 이는 100/50개 범위 충족이나 지수 학습 시작이 아닌 반입 경로 검증이다. 외부 화면·접근조건·가격을 이번 제안에서 새로 확인하지 않았다.

   ```bash
   # 서브프로젝트에서, 검증된 실제 파일 준비 후 실행할 기존 명령의 예시
   python -m smartmoney --db data/research.sqlite init-db
   python -m smartmoney --db data/research.sqlite import-csv price downloads/verified_prices.csv --market KR
   python -m smartmoney --db data/research.sqlite import-csv kr-flow downloads/verified_foreign_flow.csv --market KR --investor FOREIGN
   python -m smartmoney --db data/research.sqlite import-csv kr-ownership downloads/verified_ownership.csv --market KR
   python -m smartmoney --db data/research.sqlite status
   python -m smartmoney --db data/research.sqlite analyze --market KR --symbol 005930 --no-save
   ```

   최소 입력은 종목당 21 가격 거래일, 최근 20 가격일 중 외국인 `net_qty` 16일 이상, t−20 기준 비교가 가능한 보유율 2개 이상이다. 가능하면 수급 20일을 모두 확보한다. 수량은 주, 금액은 원, 보유율은 54.2와 같은 % 값으로 맞춘다. CSV의 investor 열은 CLI 기본값보다 우선하므로 분류를 확인한다. 검증하지 않은 `available_at/published_at` 열과 `--published-at`을 넣지 않는다. 과거 자료를 오늘 처음 받았다면 오늘 관측 이후 연구에만 쓴다. API 키 없이 가능한 경로이며 키를 채팅에 붙일 필요가 없다. 자세한 실행 조건은 [기존 원인 보고서](../sub_project/Smart_Money_Position_Analyzer/docs/evidence/2026-10-23-positioning/root_cause_review.md#실제-데이터를-준비한-뒤-실행할-경로)를 따른다.

2. **관측 계약 보강 — 서브 DB/데이터 담당이 다음에 할 작업.** revision/최초관측·단위·확정 상태·거래시장 provenance를 보강한다. 새 JSON writer/loader는 있으나 source schema의 빈 metadata나 과거 공개시각을 채우는 기능이 아니다. 두 종목의 observed/inferred/unverified를 먼저 검토한다. 기관 수급은 기존 importer로 저장 가능하지만 `PENSION` 보존·수집·판정은 별도 구현이 필요하다.
3. **연동 — 최소 구현됨.** read-only SQLite adapter 또는 검증된 JSON을 메인 feature frame에 연결한다. raw 지수 라벨은 공유하지 않고 메인이 만든다. 메인 CLI/config 연결은 core 통합 변경에서 관리한다. source DB에 쓰거나 서브 내부 함수를 import하지 않는다. 적격 가격은 bridge 자체에서도 공급할 수 있다. 시장 전체 수급, 날짜별 과거 구성, 누락된 source finality·가격조정 증거는 여전히 확보해야 한다.
4. **예측 실험 — ML 설계 6.1-sol xhigh, 실행·검증 Luna 6 xhigh.** 고정 feature 후보만 메인의 prior-only 학습·보정·ablation에 전달한다. helper의 전체 기간 상관/회귀값으로 군·열·가중치를 선택하지 않는다. PM 6.1-sol high가 범위와 증거를 조정하되 ML/risk 독립 검토 없이 production을 승인하지 않는다. 수집 자동화·유료 구독·미국 데이터 확대는 즉시 범위가 아니다.

## 5. 통과 조건과 10개 증거 항목

데이터 연결의 통과와 예측 채택은 별도다. 연결 gate는 단위·종목/시장 분류·실제 거래일·source hash·재반입 no-op·revision 보존·cutoff 직전/직후 제외·시장전체/종목 혼동 방지·구성종목 생존자 편향 차단을 검사한다. 잘못된 단위·시간대·불완전한 manifest는 거부하고, 결측은 결측으로 남겨야 한다. 테스트 fixture 통과와 실제 데이터 coverage 결과를 나란히 보고한다.

예측 비교는 **가격만 → 가격+원시 수급 → 가격+원시 수급+포지션 파생값** 순서로, 같은 적격 날짜·동일 모델/학습 예산으로 한다. 파생값 제거와 규칙 라벨 제거를 각각 확인해 원시 수급 대비 추가 기여를 구분한다. breadth·DART는 이 기본 비교 이후 별도 한 그룹씩 추가한다. 자료가 없는 군은 제외하고 전체 기간 가격 baseline도 별도 보고해 표본 선택 효과를 드러낸다. 방향 적중률은 50%와 관측된 다수 클래스 비율, prior up-rate·기존 가격 Logistic/Ridge와 함께 비교하며 50% 초과만으로 채택하지 않는다.

구현된 영향 보고는 동시일 수익률 상관과 **T시점 적격 feature→다음 세션 수익률**의 관측적 연관을 분리한다. 후자는 직전 1/5일 지수 수익률 통제 회귀·표본수·20세션 circular block bootstrap 200회의 95% 구간을 제공하며, 실제 다음 세션 target·성숙 label만 사용한다. 표본 20개 미만이면 효과를 내지 않는다. 이는 탐색적 연관 보고이고 인과효과·OOS 개선·production gate가 아니다. 채택에는 메인의 2,000회 검증과 국면별 OOS 결과가 필요하다. 실제 관측 0건인 현재는 계수·상관·0 효과를 만들지 않는다.

승인된 WFO·purge·과거 OOF 보정·개발 구간 선택 고정을 유지한다. 최소 252일 terminal OOS에서 Brier 개선과 paired 20일 block bootstrap 2,000회 95% CI, logloss/MAE 비악화, 80% 구간 coverage 75–85%·폭 baseline 1.1배 이하·하단 이탈 악화 2pp 이하를 확인한다. 연도·추세·변동성 국면, 결측/공개지연에 따른 coverage와 성능도 제시한다. 이미 확인한 terminal 결과를 보고 feature를 선택하면 새로운 사전 고정 평가 기간을 확보해야 한다. 과거 재구성 성공만으로 실제 premarket 60회 관측이나 독립 검토를 대체하지 않는다.

다음 표는 **후보별 AGENTS.md 10개 evidence의 현재 상태**다. 미측정은 성능 향상을 뜻하지 않는다.

| evidence | 원시 투자자 수급 | 외국인 보유율·포지션 파생값 | 적격 100/50개 breadth | DART 사건 |
|---|---|---|---|---|
| 1. 시장 가설 | 거래대금 대비 순매수가 수요 압력을 설명할 수 있음 | 수급 지속성과 보유율 변화의 합치가 추가 정보일 수 있음 | 지수 상승의 대형주 집중/확산 차이 | 대형 구성종목 공개 지분 사건이 지수에 영향 가능 |
| 2. 입수 방법 | 정식 KRX CSV → 기존 종목 importer; 시장총계는 새 경로 필요 | KRX CSV → ownership + 현행 외국인 규칙 | 검증된 날짜별 시총·분류·거래상태 + 종목 가격 필요 | 기존 OpenDART collector + 공식 corp_code 매핑 |
| 3. 실제 공개시각 | 미확보; 모르면 실제 최초 수집시각 | 미확보; 입력 전체의 최대 이용가능시각 검증 | 공개/유효 구간 미확보 | 원공개시각 미확보; 현행 collector는 수집시각 사용 |
| 4. 비용 | 현 이용조건·운영공수 미확인; 유료 구매 없음 | 동일 | 과거 구성 이력 확보 비용 미확인 | 승인·운영 조건 미확인; 유료 구매 없음 |
| 5. 결측률 | 서브 관측 100% 결측; 기관/연기금 확장도 미준비 | 서브 관측 100% 결측 | 적격 역사 구성·가격 패널 미확보, 산출 불가 | 서브 관측 100% 결측 |
| 6. 역사 길이 | 실제 적재 0일 | 실제 적재 0일 | 적격 PIT 이력 0일 | 실제 적재 0일 |
| 7. leakage | 공개지연·수정·시장 범위 혼합 | stale 보유율·기업행동·후행 규칙/라벨 | 현재 구성 소급·생존자 편향·기업행동 | 접수일/매매일 혼동·정정 소급 |
| 8. baseline 증가 | 미측정 | 미측정 | 미측정 | 미측정 |
| 9. 국면별 성능 | 미측정 | 미측정 | 미측정 | 미측정 |
| 10. ablation | 미실행 | 원시 수급 대비 미실행 | 미실행 | 미실행 |

검증은 Luna가 실행했다. [새 synthetic contract 테스트](../tests/test_participant_impact.py) **18개 통과**, 해당 모듈/테스트 Ruff 통과. 실제 서브 DB read-only 실행은 **flow/ownership/price 각 0행, feature 0행·0그룹, `insufficient_data`, 추정 `inconclusive`, forecast uplift `unmeasured`**였고 DB SHA-256은 전후 `102d201e1ab850979d765b5e429ec67483c78edab4d3a5c7ea9a48745746ef09`로 같았다. [실제 source 점검 산출물](../.superpowers/sdd/index-calibration/data-probes/participant_impact_actual_db.json). 이 모듈 결과와 메인 통합 변경 전체 테스트/독립 감사의 완료 여부는 구분한다.

근거 소스: [승인 계획](superpowers/plans/2026-10-23-index-calibration.md), [메인 입력](../src/roboquant/index_forecast/reliable_inputs.py), [메인 orchestration](../src/roboquant/index_forecast/reliable_pipeline.py), [서브 schema](../sub_project/Smart_Money_Position_Analyzer/smartmoney/schema.sql), [importer](../sub_project/Smart_Money_Position_Analyzer/smartmoney/importers.py), [신호](../sub_project/Smart_Money_Position_Analyzer/smartmoney/signals.py), [DB UPSERT](../sub_project/Smart_Money_Position_Analyzer/smartmoney/db.py), [출처 목록](../sub_project/Smart_Money_Position_Analyzer/docs/SOURCE_CATALOG.md), [검증 계획](../sub_project/Smart_Money_Position_Analyzer/docs/VALIDATION_PLAN.md). 설계/코드는 6.1-sol xhigh가 맡았고 실제 테스트·DB 검증은 Luna 6 xhigh가 담당했다. 유료 자료·외부 수집은 이번 연동 구현에서 실행하지 않았다.
