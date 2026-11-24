# VALIDATION_AND_RISK.md

## 1. Validation

금지:
- random train_test_split
- 전체 데이터 scaler fit
- test set 반복 tuning
- 미래 구성종목 사용

추천:
- expanding window
- rolling window
- purged time split
- embargo for overlapping labels

5D forward return은 label이 겹치므로 단순 일별 sample을 독립이라고 가정하지 않는다.

---

## 2. Metrics

### Classification
- balanced accuracy
- ROC-AUC
- PR-AUC
- F1
- MCC
- log loss
- Brier score
- calibration error

### Regression
- MAE
- RMSE
- rank correlation
- directional accuracy

### Trading
- CAGR
- annualized return
- volatility
- Sharpe
- Sortino
- max drawdown
- Calmar
- hit rate
- profit factor
- turnover
- exposure
- average holding period

---

## 3. Statistical significance

성능 차이가 작은 경우 우연일 수 있다.

검토:
- bootstrap confidence interval
- block bootstrap
- permutation where appropriate
- Diebold-Mariano style forecast comparison where appropriate
- multiple hypothesis correction

feature 수백 개를 시험하고 최고 결과 하나만 선택하지 않는다.

---

## 4. Leakage Audit

각 feature에:
`available_at <= prediction_time`
불변조건을 적용한다.

특히:
- macro revision
- DART timestamp
- news timestamp
- foreign flow finalization
- index constituent
- quarterly fundamentals
주의.

---

## 5. Drift

모니터:
- PSI
- feature distribution
- missing rate
- prediction distribution
- calibration drift
- rolling hit rate
- rolling PnL

drift 발생시:
- NO_TRADE
- fallback baseline
- retrain
중 정책 적용.

---

## 6. Risk controls

연구 모델이 실제 주문에 연결되는 경우 별도 risk engine 필요.

- max position
- max daily loss
- max drawdown
- no-trade during data outage
- stale quote reject
- kill switch
- duplicate order protection

Prediction service가 직접 주문 위험한도를 우회하지 못하게 한다.
