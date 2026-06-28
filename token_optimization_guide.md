# AI Robo 시장분석 프로젝트 토큰 사용량 최적화 가이드

## 1. 기본 원칙

LLM은 원천 DB가 아니라 해석기로 사용한다.

```text
DB/파이프라인 역할:
- 원문 수집
- 중복 제거
- 티커 매핑
- 수치 피처 계산
- 영향도 점수 계산
- evidence 후보 선별

LLM 역할:
- 이미 선별된 근거를 자연어로 설명
- 원인 후보를 사용자 친화적으로 정리
- 리스크 문구 작성
```

---

## 2. LLM에 넣지 말아야 할 것

```text
뉴스 기사 전문
공시 보고서 전문
Telegram 메시지 수백 개
OHLCV 전체 시계열
DB 전체 dump
중복 기사
광고/리딩방 메시지
HTML 원문
긴 표 전체
```

---

## 3. LLM에 넣을 것

```text
급등락 이벤트 요약
가격/거래량 핵심 수치
투자자별 순매수 핵심 수치
영향도 상위 뉴스 3~5개
영향도 상위 공시 1~3개
시장 분위기 핵심 지표
섹터/테마 핵심 키워드
각 근거의 URL
```

---

## 4. Evidence Bundle 포맷

```json
{
  "ticker": "005930",
  "name": "삼성전자",
  "date": "2026-06-23",
  "event": {
    "return_pct": 4.2,
    "volume_ratio_20d": 2.4,
    "index_relative_return_pct": 3.1
  },
  "flows": {
    "foreign_net_buy_krw": 125000000000,
    "institution_net_buy_krw": 42000000000,
    "individual_net_buy_krw": -168000000000
  },
  "market": {
    "kospi_return_pct": 1.1,
    "usdkrw_change_pct": -0.3,
    "sector": "반도체",
    "sector_return_pct": 2.8
  },
  "top_evidence": [
    {
      "type": "news",
      "title": "반도체 업황 개선 기대",
      "summary": "HBM 수요와 메모리 가격 회복 기대가 언급됨",
      "impact_score": 0.82,
      "url": "https://example.com/news/1"
    }
  ]
}
```

---

## 5. 토큰 예산

### 5.1 종목 1개 분석

```text
시스템 지시문: 500~800 tokens
수치 JSON: 300~800 tokens
evidence 5개: 1,000~2,000 tokens
출력 요구사항: 300~500 tokens

총합 목표: 2,500~4,000 tokens
상한: 8,000 tokens
```

### 5.2 시장 전체 분석

```text
시장 지표 요약: 500~1,000 tokens
수급 랭킹: 500~1,000 tokens
뉴스 Top 10: 1,000~2,000 tokens
섹터 요약: 500~1,000 tokens

총합 목표: 4,000~6,000 tokens
상한: 10,000 tokens
```

---

## 6. Prompt 템플릿

### 6.1 종목 급등락 원인 분석

```text
너는 투자 참고용 시장 분석 리포트를 작성하는 애널리스트다.
아래 Evidence Bundle만 근거로 종목의 급등락 원인을 분석해라.

주의:
- 투자 권유 표현을 쓰지 마라.
- 확정적으로 말하지 마라.
- 원인 후보는 영향도 순서로 정리해라.
- 뉴스/공시/수급/시장 분위기를 분리해서 설명해라.
- 루머 가능성이 있는 정보는 낮은 신뢰도로 표시해라.
- 제공되지 않은 사실을 추측하지 마라.

출력 형식:
1. 한 줄 요약
2. 주요 원인 Top 5
3. 수급 해석
4. 뉴스/공시 해석
5. 시장 분위기 영향
6. 리스크
7. 투자 유의 문구

Evidence Bundle:
{evidence_bundle_json}
```

### 6.2 시장 분위기 분석

```text
너는 시장 분위기를 요약하는 애널리스트다.
아래 데이터만 근거로 오늘 시장 분위기를 분석해라.

출력:
1. 시장 분위기 한 줄 요약
2. 외국인/기관/개인 수급 해석
3. 강한 섹터와 약한 섹터
4. 뉴스/매크로 영향
5. 주의할 리스크

Market Bundle:
{market_bundle_json}
```

---

## 7. 캐시 전략

LLM 호출 비용과 토큰 사용량을 줄이기 위해 캐시를 사용한다.

### 7.1 캐시 키

```text
summary_cache_key =
    ticker + event_date + evidence_hash + prompt_version
```

### 7.2 캐시 테이블

```sql
CREATE TABLE IF NOT EXISTS llm_summary_cache (
    cache_key TEXT PRIMARY KEY,
    ticker TEXT,
    event_date TEXT,
    prompt_version TEXT,
    evidence_hash TEXT,
    summary TEXT,
    created_at TEXT
);
```

### 7.3 캐시 규칙

```text
같은 evidence_hash면 LLM 재호출 금지
뉴스/수급 데이터가 바뀌면 evidence_hash 변경
prompt_version이 바뀌면 재생성
시장 마감 후 생성된 일간 리포트는 고정
```

---

## 8. 중복 제거

### 8.1 URL 중복 제거

```python
def dedupe_by_url(items):
    seen = set()
    result = []
    for item in items:
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(item)
    return result
```

### 8.2 제목 유사도 중복 제거

```python
from difflib import SequenceMatcher

def is_similar(a, b, threshold=0.86):
    return SequenceMatcher(None, a, b).ratio() >= threshold
```

---

## 9. 요약 압축 규칙

```text
1단계: 기사/공시별 300~500자 요약
2단계: 종목별 evidence bundle 생성
3단계: 최종 LLM 분석
```

고비용 LLM은 최종 분석에만 사용한다.

---

## 10. 출력 길이 제한

```text
종목 상세 분석:
- 전체 1,500자 이내
- 원인 Top 5
- 각 원인 2문장 이내

시장 요약:
- 전체 2,000자 이내
- 수급/뉴스/섹터/리스크로 구분

대시보드 카드:
- 한 카드당 300자 이내
```

---

## 11. Codex 구현 지시

```text
토큰 최적화를 위해 다음을 반드시 구현해라.

1. build_evidence_bundle() 함수
2. truncate_text(text, max_chars=500) 함수
3. dedupe_by_url() 함수
4. dedupe_by_title_similarity() 함수
5. evidence_hash 생성 함수
6. llm_summary_cache 테이블
7. prompt_version 관리
8. tests/test_token_budget.py

LLM 호출 함수는 원문 전체를 받지 않고 Evidence Bundle만 받도록 설계해라.
뉴스/공시/텔레그램 원문 전문을 prompt에 넣는 코드는 작성하지 마라.
```
