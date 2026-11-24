# PROJECT_SPEC.md

## 1. Mission

한국 주식시장 4개 대표 지수:

- KOSPI
- KOSPI 100
- KOSDAQ
- KOSDAQ 100

에 대해 다음을 예측한다.

### Horizon
- Next session: 1D
- Next week: 5D

### Task
A. 방향 분류
- UP
- DOWN
- FLAT

B. 수익률 회귀
- forward return

C. 변동성 회귀
- forward realized volatility

D. 시장 상태 분류
- Risk-On
- Risk-Off
- Trend-Up
- Trend-Down
- Range
- High-Volatility
- Event-Risk

## 2. Label Definition

단순 `close[t+1] > close[t]` 라벨은 noise가 많으므로 threshold를 둔다.

예:
- 1D: 수익률 > +threshold => UP
- 1D: 수익률 < -threshold => DOWN
- 나머지 => FLAT

threshold는 고정 bp 또는 변동성 적응형으로 비교한다.

예:
`threshold_t = k * rolling_volatility_t`

5D도 동일한 방식으로 forward 5-session return을 사용한다.

## 3. Prediction Timing Contract

예측시점별로 실제 사용 가능한 정보만 사용한다.

### 장 종료 후 EOD prediction
예: 16:30 KST 이후
- 당일 종가
- 당일 거래량
- 확정된 투자자별 수급
- 당일까지 공개된 공시/뉴스
- 당일까지 공개된 금리/환율 데이터
- 다음 거래일 1D/5D 예측

### 장 시작 전 Pre-open prediction
- 전일 한국시장 EOD
- 미국 overnight market
- 야간 선물/FX
- 장전 뉴스/공시
- pre-open 정보

장중 데이터와 장마감 데이터가 섞이지 않도록 feature timestamp를 보존한다.

## 4. Target Output Schema

```json
{
  "index": "KOSPI",
  "as_of": "YYYY-MM-DDTHH:MM:SS+09:00",
  "horizon": "1D",
  "p_up": 0.00,
  "p_flat": 0.00,
  "p_down": 0.00,
  "expected_return": 0.00,
  "expected_volatility": 0.00,
  "regime": "RISK_ON",
  "signal": "NEUTRAL",
  "confidence": 0.00,
  "data_quality": 0.00,
  "reason_codes": []
}
```

## 5. Non-goals

초기 버전에서는 다음을 목표로 하지 않는다.

- 개별주식 목표가 산출
- 100% 승률
- 고빈도 자동매매
- 설명 불가능한 black-box 모델 단독 운용
- 훈련 데이터 전체에 대한 최고 정확도 추구
