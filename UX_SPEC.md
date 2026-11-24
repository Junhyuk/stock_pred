# UX_SPEC.md

## Home Dashboard

4개 카드:
- KOSPI
- KOSPI100
- KOSDAQ
- KOSDAQ100

각 카드:
- 1D: 상승/중립/하락 확률
- 5D: 상승/중립/하락 확률
- expected return
- expected volatility
- confidence
- signal
- last update
- data health

## Signal colors

UI에서 색상만으로 의미를 전달하지 않는다.
텍스트/아이콘/확률을 같이 표시한다.

## Explain panel

예:
### 상승 요인
- 외국인 현물/선물 동시 순매수
- Nasdaq/SOX overnight 상승
- 시장 breadth 개선

### 하락 요인
- USD/KRW 급등
- 옵션 변동성 상승
- 부정 뉴스량 증가

### 불확실성
- FOMC 이벤트
- 모델 간 disagreement

## Flow Screen

- 외국인 1/5/20D 순매수
- 기관
- 개인
- 프로그램
- 선물
- 공매도
- breadth

## Model Health Screen

- 최근 20/60D hit rate
- Brier
- calibration
- drift
- missing data
- model version

## UX safety

`70% 상승`을 `상승 확정`으로 표현하지 않는다.

추천 문구:
`모델 기준 상승 확률 70%. 과거 유사 신뢰도 구간의 성능과 데이터 상태를 함께 확인하세요.`

강제 표현 금지:
- 무조건 매수
- 확실한 상승
- 손실 없음
