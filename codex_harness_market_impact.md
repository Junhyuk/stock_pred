# Codex Harness 지시서: market-impact-ai-robo

## 1. 목적

이 Harness는 Codex가 구현한 `market-impact-ai-robo` 프로젝트가 다음 조건을 만족하는지 검증한다.

1. 프로젝트 구조가 맞는지
2. 필수 파일이 존재하는지
3. DB 초기화가 동작하는지
4. 샘플 데이터로 급등락 탐지가 동작하는지
5. 영향도 점수 계산이 동작하는지
6. 웹 대시보드가 import 가능한지
7. 토큰 최적화 규칙을 지키는지
8. 자동 매매 기능이 포함되지 않았는지

---

## 2. 필수 폴더

```text
market-impact-ai-robo/
├── data/
├── logs/
├── reports/
├── src/
│   ├── collectors/
│   ├── features/
│   ├── analysis/
│   └── web/
└── tests/
```

---

## 3. 필수 파일

```text
README.md
requirements.txt
.env.example
app.py
src/config.py
src/db.py
src/scheduler.py
src/collectors/collect_pykrx.py
src/collectors/collect_dart.py
src/collectors/collect_news.py
src/collectors/collect_rss.py
src/collectors/collect_telegram.py
src/collectors/collect_us_prices.py
src/collectors/collect_finnhub.py
src/collectors/collect_sec.py
src/collectors/collect_fred.py
src/features/price_features.py
src/features/flow_features.py
src/features/news_features.py
src/features/disclosure_features.py
src/features/macro_features.py
src/features/theme_features.py
src/analysis/detect_large_moves.py
src/analysis/score_impact.py
src/analysis/explain_move.py
src/analysis/market_mood.py
src/analysis/summarize_ai.py
src/web/components.py
src/web/charts.py
tests/test_large_move_detection.py
tests/test_impact_score.py
tests/test_news_features.py
tests/test_flow_features.py
tests/test_token_budget.py
```

---

## 4. 검증 명령

```bash
python src/db.py
pytest -q
python src/analysis/detect_large_moves.py --sample
python src/analysis/score_impact.py --sample
python -m py_compile app.py
```

---

## 5. 샘플 테스트 데이터

### 5.1 가격 이벤트

```json
{
  "ticker": "005930",
  "name": "삼성전자",
  "event_date": "2026-06-23",
  "return_pct": 4.2,
  "index_relative_return_pct": 3.1,
  "volume_ratio_20d": 2.4,
  "trading_value_ratio_20d": 2.1,
  "rolling_volatility_20d": 1.4
}
```

### 5.2 수급 데이터

```json
{
  "individual_net_buy": -168000000000,
  "foreign_net_buy": 125000000000,
  "institution_net_buy": 42000000000
}
```

### 5.3 뉴스 데이터

```json
{
  "title": "반도체 업황 개선 기대에 대형주 강세",
  "summary": "HBM 수요와 메모리 가격 회복 기대가 언급됨",
  "sentiment_score": 0.65,
  "relevance_score": 0.90
}
```

---

## 6. 필수 함수 인터페이스

### 6.1 detect_large_move

```python
def detect_large_move(
    return_pct: float,
    rolling_volatility_20d: float,
    volume_ratio_20d: float,
    index_relative_return_pct: float
) -> bool:
    ...
```

기대 동작:

```python
assert detect_large_move(5.1, 1.2, 1.0, 1.0) is True
assert detect_large_move(1.0, 1.2, 1.0, 0.5) is False
assert detect_large_move(2.5, 1.0, 1.0, 0.5) is True
assert detect_large_move(1.0, 1.0, 2.1, 0.5) is True
assert detect_large_move(1.0, 1.0, 1.0, 3.1) is True
```

### 6.2 calculate_final_impact_score

```python
def calculate_final_impact_score(
    price_move_score: float,
    volume_spike_score: float,
    investor_flow_score: float,
    news_sentiment_score: float,
    disclosure_score: float,
    sector_theme_score: float,
    macro_market_score: float,
    rumor_risk_penalty: float
) -> float:
    ...
```

산식:

```text
final_impact_score =
    price_move_score        * 0.15
  + volume_spike_score      * 0.15
  + investor_flow_score     * 0.20
  + news_sentiment_score    * 0.15
  + disclosure_score        * 0.15
  + sector_theme_score      * 0.10
  + macro_market_score      * 0.10
  - rumor_risk_penalty      * 0.10
```

결과는 0.0 ~ 1.0 사이로 clip한다.

### 6.3 build_evidence_bundle

```python
def build_evidence_bundle(
    ticker: str,
    event_date: str,
    price_features: dict,
    flow_features: dict,
    evidence_items: list[dict],
    max_items: int = 5,
    max_chars_per_item: int = 500
) -> dict:
    ...
```

규칙:

```text
evidence 최대 5개
evidence summary 500자 이하
URL 포함 가능
원문 전문 포함 금지
반환값은 compact JSON 가능 구조
```

---

## 7. 토큰 예산 테스트

`tests/test_token_budget.py`는 다음을 검증한다.

1. Evidence Bundle의 evidence 개수는 5개 이하
2. 각 evidence summary는 500자 이하
3. 전체 JSON 문자열 길이는 12,000자 이하
4. prompt 생성 함수가 raw article body를 포함하지 않음
5. 중복 URL이 제거됨

---

## 8. 금지 기능 테스트

자동 매매 기능은 구현하면 안 된다.

금지 키워드:

```text
place_order
send_order
buy_market
sell_market
auto_trade
auto_order
kis_order
```

Harness는 소스 코드에서 위 이름의 함수가 있는지 검사한다.

---

## 9. README 검증 항목

README.md에는 반드시 다음 섹션이 있어야 한다.

```text
설치 방법
환경변수 설정
데이터 소스
실행 순서
웹 대시보드 실행
투자 유의사항
자동 매매 미지원 안내
```

---

## 10. 완료 판정

아래 조건이 모두 만족되면 MVP 완료로 본다.

1. `pytest -q` 통과
2. `python src/db.py` 성공
3. `python src/analysis/detect_large_moves.py --sample` 성공
4. `python src/analysis/score_impact.py --sample` 성공
5. `python -m py_compile app.py` 성공
6. README 필수 섹션 존재
7. 자동 매매 금지 키워드가 실제 함수로 존재하지 않음
