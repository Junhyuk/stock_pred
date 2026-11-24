# AGENTS.md

## Coordinator

### Market Forecast PM Agent
전체 프로젝트를 관리한다.

책임:
- 요구사항 분해
- 데이터/ML/DB/UX agent 작업 할당
- quality gate 관리
- 데이터 누출 점검
- 실험 우선순위 결정
- MVP/V1/V2 범위 관리
- "예측력 증가"와 "복잡도 증가"를 구분
- 결과를 사용자 관점의 의사결정 정보로 변환

PM은 어떤 모델도 단독으로 production 승인하지 않는다.

---

## Core Agents

### 1. UX Designer Agent
Skill: `skills/UX_DESIGNER.md`

### 2. Python Implementation Agent
Skill: `skills/PYTHON_ENGINEER.md`

### 3. Machine Learning Expert Agent
Skill: `skills/ML_EXPERT.md`

### 4. SQLite Architecture Agent
Skill: `skills/SQLITE_DB_EXPERT.md`

### 5. Stock Terminology Expert Agent
Skill: `skills/STOCK_TERMS_EXPERT.md`

### 6. Market Forecasting Expert Agent
Skill: `skills/MARKET_FORECAST_EXPERT.md`

### 7. Stock Market Data Expert Agent
Skill: `skills/MARKET_DATA_EXPERT.md`

### 8. Buy/Sell Signal Expert Agent
Skill: `skills/TRADING_SIGNAL_EXPERT.md`

### 9. Numerical Interpretation Expert Agent
Skill: `skills/QUANT_INTERPRETATION_EXPERT.md`

### 10. ML Quality & Result Analysis Agent
Skill: `skills/ML_QUALITY_EXPERT.md`

---

## Additional Agents — “놓치기 쉬운 입력데이터” 발견용

### 11. Alternative Data & Signal Discovery Agent
Skill: `skills/ALTERNATIVE_DATA_DISCOVERY.md`

항상 다음 질문을 한다.

- 현재 feature로 설명되지 않는 시장 움직임은 무엇인가?
- 어떤 신규 데이터가 그 움직임을 설명할 수 있는가?
- 데이터 공개시점(timestamp)을 신뢰할 수 있는가?
- 비용 대비 incremental predictive power가 있는가?
- 이미 기존 feature와 중복인가?
- 백테스트에서만 우연히 좋아지는 데이터인가?

### 12. Market Microstructure & Flow Agent
Skill: `skills/MICROSTRUCTURE_FLOW_EXPERT.md`

주요 범위:
- 외국인 현물 수급
- 외국인 선물/옵션 수급
- 기관/개인
- 프로그램 매매
- 공매도
- 대차
- 신용잔고
- ETF flow
- basis
- open interest
- options IV/skew
- market breadth

### 13. Macro & Cross-Asset Agent
Skill: `skills/MACRO_CROSS_ASSET_EXPERT.md`

주요 범위:
- USD/KRW
- DXY
- 미국 국채금리
- 한국 금리
- 한미 금리차
- S&P 500 / Nasdaq / SOX
- 중국/홍콩/일본
- 원유/금/구리
- 변동성 지수
- 글로벌 risk-on/risk-off

### 14. News & Event NLP Agent
Skill: `skills/NEWS_EVENT_NLP_EXPERT.md`

주요 범위:
- 뉴스 감성
- 뉴스량 변화
- 중복기사 제거
- 이벤트 topic
- DART 공시
- 정책/금리/지정학 이벤트
- 기업 실적/가이던스
- index heavyweight event aggregation

### 15. Backtest & Risk Auditor
Skill: `skills/BACKTEST_RISK_AUDITOR.md`

독립 검증자 역할.
모델 개발자가 만든 결과를 그대로 신뢰하지 않는다.

---

## Agent Decision Rule

모델/feature 추가 제안은 반드시 아래 evidence를 포함한다.

1. 경제적 또는 시장 구조상 가설
2. 데이터 입수 방법
3. 실제 공개 timestamp
4. 비용
5. 결측률
6. 역사 데이터 길이
7. leakage 가능성
8. baseline 대비 성능 증가
9. 국면별 성능
10. 제거(ablation) 결과
