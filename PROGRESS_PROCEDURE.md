# PROGRESS_PROCEDURE.md

## Stage 1 — Data Foundation

### 1.1 Index targets
- KOSPI
- KOSPI 100
- KOSDAQ
- KOSDAQ 100

### 1.2 Market calendar
한국거래소 휴장일을 기준으로 canonical session table 생성.

### 1.3 Minimum historical range
가능하면 10년 이상.
KRX Open API의 공개 가능한 범위/정책을 확인하여 확보한다.

### Done
- 4개 지수 EOD 데이터 정상화
- 결측/중복 검사
- market calendar 정렬
- raw snapshot 보존

---

## Stage 2 — Core Flow Features

- foreign net buy amount
- foreign net buy ratio
- institutional net buy
- individual net buy
- foreign consecutive buy/sell days
- foreign rolling 5/20D flow
- foreign flow acceleration
- flow / market turnover
- flow / market cap
- foreign cash + futures combined signal

### Done
OOS baseline 대비 incremental performance 평가 완료.

---

## Stage 3 — Derivatives / Positioning

- index futures return
- basis
- basis z-score
- foreign futures net position
- open interest
- OI change
- options volume
- put/call
- implied volatility
- skew
- expiry D-day

---

## Stage 4 — Breadth / Internal Market

- advance/decline
- up volume / down volume
- 52w high-low
- % above 20/60/120MA
- equal-weight vs cap-weight divergence
- large-cap contribution
- small-cap relative strength
- sector breadth
- dispersion

KOSPI100은 대형주 집중도 때문에 breadth와 heavyweight contribution을 반드시 구분한다.

---

## Stage 5 — Macro / Cross Asset

- USDKRW
- DXY
- US 2Y / 10Y
- Korea 3Y / 10Y
- KR-US spread
- S&P500
- Nasdaq
- SOX
- VIX
- Nikkei
- Hang Seng
- China indices
- WTI
- Gold
- Copper

한국장 개장 이전에 이미 관찰할 수 있는 overnight 데이터로 별도 feature set을 구성한다.

---

## Stage 6 — News / Disclosure

- article count
- positive/negative sentiment
- surprise sentiment
- topic
- entity
- source
- duplication cluster
- publication time
- weighted index impact

DART:
- earnings
- capital increase
- buyback
- merger
- spin-off
- major shareholder
- large holding
- issuance
- material events

---

## Stage 7 — Baselines

### Classification
- Logistic Regression
- Random Forest
- HistGradientBoosting
- LightGBM
- XGBoost

### Regression
- Linear/Ridge/ElasticNet
- LightGBM regressor
- XGBoost regressor

---

## Stage 8 — Ensemble

분리된 모델:
- price model
- flow model
- macro model
- news model
- derivatives model

Meta model 또는 가중 평균으로 ensemble.

장점:
어떤 정보군이 신호를 만들었는지 설명하기 쉬움.

---

## Stage 9 — Regime

regime feature 후보:
- realized vol
- VIX
- trend
- USDKRW shock
- liquidity
- credit spread
- breadth
- foreign flow

HMM, clustering, rule-based regime을 비교한다.

---

## Stage 10 — Simulation

예측확률 threshold를 바꿔 다음 curve를 계산한다.

- coverage
- hit rate
- return
- volatility
- drawdown
- Sharpe
- turnover

`NO_TRADE` 구간을 적극 사용한다.

---

## Stage 11 — Paper Trading

최소 수개월 동안 실제 시점으로 prediction snapshot을 저장한다.

예측 후 데이터를 다시 쓰지 않는다.

---

## Stage 12 — Continuous Research Loop

매주 Alternative Data Agent가 신규 feature 후보를 제출한다.

1. 후보 5개
2. 데이터 비용
3. history
4. leakage risk
5. hypothesis
6. quick test
7. ablation
8. keep/drop 결정

Feature count를 늘리는 것이 목표가 아니다.
Out-of-sample information gain을 늘리는 것이 목표다.
