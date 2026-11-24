# HARNESS.md

## Operating Harness

모든 AI coding agent는 다음 절차를 따라야 한다.

## Phase 0 — Repository Recon

먼저 읽는다.

1. README
2. PROJECT_SPEC
3. AGENTS
4. DATA_SOURCES
5. FEATURE_CATALOG
6. DB_SCHEMA
7. MODEL_STRATEGY
8. VALIDATION_AND_RISK
9. QUALITY_GATES

코드를 작성하기 전에 기존 구조/테스트/DB migration을 확인한다.

---

## Phase 1 — Define the hypothesis

예:
> 외국인의 KOSPI 현물 + KOSPI200 선물 동시 순매수는 단순 현물수급보다
> 다음날 KOSPI 방향 예측에 추가적인 정보를 제공할 것이다.

가설 없는 feature 추가를 금지한다.

---

## Phase 2 — Data contract

각 데이터셋은 다음 metadata를 가진다.

- source
- symbol
- market
- event_time
- published_time
- ingested_time
- timezone
- frequency
- revision flag
- raw value
- adjusted value
- data version

`published_time`을 모르면 미래누출 위험으로 표시한다.

---

## Phase 3 — Raw ingestion

원본은 가능한 수정하지 않는다.

`raw -> normalized -> feature -> model`

단계를 분리한다.

---

## Phase 4 — Data validation

체크:
- duplicates
- missing
- impossible values
- stale data
- split/holiday mismatch
- timestamp mismatch
- timezone
- market calendar
- late revision

---

## Phase 5 — Feature generation

feature는 반드시 point-in-time safe하게 생성한다.

금지:
- 미래 종가를 rolling feature에 포함
- 다음날 공개되는 통계를 당일 feature로 사용
- revised macro data를 최초 발표값처럼 과거에 삽입
- 현재 index constituents를 과거 전체 기간에 사용

---

## Phase 6 — Baseline first

항상 baseline부터 실행한다.

1. majority class
2. previous direction
3. moving-average rule
4. logistic regression
5. simple tree

복잡한 모델이 baseline을 안정적으로 이기지 못하면 채택하지 않는다.

---

## Phase 7 — Walk-forward training

예:
- Train: 과거 N년
- Validation: 다음 M개월
- Test: 다음 K개월
- window 이동
- 반복

random shuffle split 금지.

---

## Phase 8 — Calibration

분류 확률을 reliability curve / Brier score / log loss로 검증한다.

`p_up=0.70`인 샘플이 실제로 비슷한 비율로 상승하는지 확인한다.

---

## Phase 9 — Trading simulation

신호:
- LONG
- SHORT
- NEUTRAL
- NO_TRADE

거래 대상은 실제 지수 자체가 아니라 ETF/선물 등 구현 상품에 따라 별도 비용모델을 적용한다.

반영:
- commission
- tax where applicable
- spread
- slippage
- tracking error
- futures roll cost

---

## Phase 10 — Stress tests

- 2020급 고변동성 구간
- 금리 급변
- 원/달러 급변
- 옵션 만기
- 선물 만기
- 미국 급락 overnight
- 외국인 대규모 순매도
- 거래량 급증
- 뉴스 shock

---

## Phase 11 — Ablation

feature group을 하나씩 빼서 성능을 비교한다.

- Price only
- + Flow
- + Derivatives
- + Macro
- + News
- + DART
- + Alternative

신규 데이터가 효과가 없으면 제거한다.

---

## Phase 12 — Production gate

다음을 만족하지 못하면 production signal에 사용하지 않는다.

- reproducible
- leakage audit pass
- OOS improvement
- calibration acceptable
- transaction-cost-adjusted result acceptable
- drift monitor enabled
- fallback model exists
- data outage behavior defined
