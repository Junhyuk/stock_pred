# MODEL_STRATEGY.md

## 1. Problem decomposition

하나의 거대한 모델보다 다음 구조를 우선 실험한다.

### Model A — Price/Technical
가격, 거래량, breadth

### Model B — Flow
외국인/기관/개인/프로그램/공매도

### Model C — Derivatives
선물/옵션/OI/basis

### Model D — Macro/Cross Asset
FX/rates/global

### Model E — News/Event
뉴스/DART

### Meta Model
A~E output + regime을 입력하여 최종 확률 생성.

---

## 2. Baseline Models

필수:
- Logistic Regression
- Ridge/ElasticNet
- HistGradientBoosting

추천:
- LightGBM
- XGBoost
- CatBoost

---

## 3. Time-Series Models

비교 후보:
- ARIMA/ARIMAX
- VAR
- state-space
- HMM regime

예측 수익률 자체보다 regime/volatility에 더 유용할 수 있다.

---

## 4. Deep Learning

충분한 데이터가 있을 때만:
- MLP
- LSTM/GRU
- TCN
- Temporal Fusion Transformer
- Transformer encoder

Deep model은 반드시 tree baseline 대비:
- OOS
- calibration
- stability
- compute cost
를 비교한다.

---

## 5. Multi-task Learning

추천 연구:

동시에 예측:
- direction_1d
- return_1d
- vol_1d
- direction_5d
- return_5d
- vol_5d

shared representation이 일반화를 개선하는지 검증한다.

---

## 6. Uncertainty

signal은 point estimate만 사용하지 않는다.

방법:
- ensemble dispersion
- conformal prediction
- quantile regression
- calibrated probability
- bootstrap uncertainty

---

## 7. Regime-aware ensemble

예:
- high-vol regime: flow/vol model weight 증가
- trend regime: momentum model weight 증가
- event regime: news/event weight 증가

단, regime-dependent weight 역시 training/test 분리를 지킨다.

---

## 8. Explainability

필수:
- permutation importance
- SHAP (tree model)
- feature group contribution
- reason codes

사용자 화면에는 SHAP raw 값 전체보다:
- 외국인 수급 긍정
- 환율 부정
- 미국 기술주 긍정
- breadth 중립
- 뉴스 위험 상승
형태로 요약한다.
