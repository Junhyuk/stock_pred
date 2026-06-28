# 주식 급등락 원인·시장 분위기·수급 영향도 분석 웹 대시보드 개발 지시서

작성일: 2026-06-23  
프로젝트명: `market-impact-ai-robo`

## 0. 목적

주식의 등락폭이 클 때 그 원인을 자동 분석하고, 시장 분위기, 기관/외국인/개인 매매 동향, 뉴스, 공시, 텔레그램 속보성 정보를 주기적으로 수집하여 웹 대시보드로 보여주는 시스템을 구현한다.

초기 목표는 자동매매가 아니라 아래 형태의 분석 웹 서비스다.

```text
종목 급등락 원인 분석
시장 분위기 요약
기관/외국인/개인 수급 분석
뉴스/공시 영향도 분석
관련 근거 링크 제공
AI 요약 리포트 생성
```

투자 권유 또는 자동 매매 기능은 구현하지 않는다.

---

## 1. 사용자 시나리오

### 1.1 종목 급등락 분석

사용자 질문 예시:

```text
삼성전자 오늘 왜 많이 올랐어?
SK하이닉스 급락 원인 분석해줘
테슬라 어제 급등 원인을 뉴스와 수급으로 분석해줘
오늘 급등락 종목 중 뉴스 영향이 큰 종목 보여줘
```

대시보드 출력 예시:

```text
[삼성전자 급등 원인 분석]

오늘 등락률: +4.2%
거래량: 20일 평균 대비 2.4배
외국인 순매수: +1,250억
기관 순매수: +420억
개인 순매수: -1,680억

주요 원인 후보:
1. 반도체 업황 개선 뉴스: 영향도 32%
2. 외국인 대규모 순매수: 영향도 28%
3. AI/HBM 테마 강세: 영향도 21%
4. 시장 전체 위험선호 회복: 영향도 12%
5. 기타: 7%

AI 요약:
오늘 상승은 단순 개별 종목 이슈보다 외국인 수급, 반도체 섹터 강세,
AI/HBM 테마 뉴스가 동시에 작용한 것으로 추정됩니다.
```

### 1.2 시장 분위기 분석

사용자 질문 예시:

```text
오늘 시장 분위기 어때?
외국인과 기관이 사는 종목 위주로 보여줘
개인이 많이 파는 종목 중 기관/외국인이 사는 종목 찾아줘
오늘 뉴스가 시장에 긍정적이야 부정적이야?
```

---

## 2. 전체 시스템 아키텍처

```text
[데이터 수집]
KRX / pykrx / OpenDART / KIS Open API
yfinance / Alpha Vantage / Finnhub / SEC EDGAR / FRED
Naver Search API / RSS / Telegram Telethon

        ↓

[저장]
SQLite MVP
PostgreSQL 운영 확장
Redis 캐시 선택

        ↓

[가공]
가격·거래량 피처
투자자별 수급 피처
뉴스/공시 피처
텔레그램 피처
매크로/시장 분위기 피처
섹터/테마 피처

        ↓

[분석]
급등락 탐지
원인 후보 매칭
영향도 점수화
Evidence Bundle 생성
LLM 요약

        ↓

[웹]
Streamlit MVP
FastAPI + React 확장 가능
```

---

## 3. 데이터를 가져오거나 구독할 사이트·방법

### 3.1 수집 우선순위

```text
1순위: 공식 API
2순위: 공개 데이터 라이브러리
3순위: RSS/Telegram 구독
4순위: 약관과 robots.txt 확인 후 웹 수집
5순위: 관리자 수동 등록
```

웹 크롤링은 마지막 수단으로만 사용한다. 뉴스와 공시는 저작권/약관 이슈가 생길 수 있으므로 원문 전문을 저장하지 말고 아래 필드만 저장한다.

```text
title
url
published_at
short_summary
source
related_tickers
sentiment_score
impact_score
```

---

## 4. 한국 주식 데이터 소스

### 4.1 KRX 정보데이터시스템

- 사이트: https://data.krx.co.kr
- 용도:
  - 전종목 시세
  - 지수
  - 투자자별 매매동향
  - 업종별 등락률
  - 공매도/대차 관련 데이터
- 구현 방법:
  - MVP에서는 `pykrx`를 우선 사용한다.
  - 운영 안정성이 필요하면 KRX 공식 데이터 구매/다운로드 구조를 검토한다.
- 주의:
  - 화면 구조가 바뀌면 비공식 수집 라이브러리가 깨질 수 있다.
  - retry, fallback, 데이터 검증 로직을 넣는다.

### 4.2 pykrx

- GitHub: https://github.com/sharebook-kr/pykrx
- 설치:

```bash
pip install pykrx
```

- 예시:

```python
from pykrx import stock

# KOSPI 종목 리스트
tickers = stock.get_market_ticker_list("20260623", market="KOSPI")

# 일별 OHLCV
df_price = stock.get_market_ohlcv_by_date("20260601", "20260623", "005930")

# 투자자별 거래실적
df_flow = stock.get_market_trading_value_by_investor(
    "20260623", "20260623", "005930"
)
```

- 수집 항목:
  - 종목별 OHLCV
  - 거래량/거래대금
  - 투자자별 매매동향
  - 지수
  - 업종별 등락률

### 4.3 OpenDART API

- 사이트: https://opendart.fss.or.kr
- 용도:
  - 한국 상장사 공시 검색
  - 사업보고서/분기보고서
  - 주요사항보고서
  - 단일판매공급계약
  - 유상증자/전환사채
  - 최대주주 변경
- 구현 방법:
  - API 키 발급
  - 종목코드와 고유번호 매핑
  - `list.json`으로 공시 검색
  - 중요 공시 유형을 rule-based로 분류
- 저장 필드:
  - corp_code
  - stock_code
  - corp_name
  - report_nm
  - rcept_no
  - rcept_dt
  - url
  - disclosure_type
  - impact_score

### 4.4 한국투자증권 KIS Open API

- 사이트: https://apiportal.koreainvestment.com
- 용도:
  - 국내/해외 주식 시세
  - 호가
  - 체결
  - 실시간 WebSocket
- 본 프로젝트 사용 범위:
  - 시세/호가/체결 조회까지만 사용
  - 자동 주문은 구현하지 않음
- 주의:
  - 실전/모의투자 도메인 구분
  - 토큰 만료 관리 필요
  - 주문 API는 프로젝트 범위에서 제외

### 4.5 네이버 뉴스 검색 API

- 사이트: https://developers.naver.com/docs/serviceapi/search/news/news.md
- 용도:
  - 국내 종목 뉴스 검색
  - 특정 종목명/키워드 기반 뉴스 수집
- 구현 방법:
  - 네이버 개발자센터에서 Client ID/Secret 발급
  - `query=삼성전자`, `query=SK하이닉스 HBM` 등으로 검색
  - 원문 전문 저장 금지
  - 제목, 링크, description, pubDate 중심 저장
- 예시:

```python
import requests

def search_naver_news(query: str, client_id: str, client_secret: str):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {"query": query, "display": 20, "sort": "date"}
    res = requests.get(url, headers=headers, params=params, timeout=10)
    res.raise_for_status()
    return res.json()["items"]
```

### 4.6 RSS 구독

- 용도:
  - 경제지/증권사 리포트/글로벌 뉴스 속보 구독
- 구현 방법:
  - `feedparser` 사용
  - RSS URL을 `config/rss_sources.yaml`에 저장
- 예시:

```python
import feedparser

def fetch_rss(feed_url: str):
    feed = feedparser.parse(feed_url)
    rows = []
    for entry in feed.entries:
        rows.append({
            "title": entry.get("title"),
            "link": entry.get("link"),
            "published": entry.get("published"),
            "summary": entry.get("summary"),
        })
    return rows
```

---

## 5. 미국 주식 데이터 소스

### 5.1 yfinance

- 사이트: https://ranaroussi.github.io/yfinance/
- 용도:
  - 빠른 프로토타입 가격 데이터
  - 일봉/분봉/ETF/지수
- 예시:

```python
import yfinance as yf

df = yf.download("NVDA", period="6mo", interval="1d")
```

- 주의:
  - 운영용 핵심 데이터로는 유료/공식 API 검토
  - 백테스트/프로토타입에 우선 사용

### 5.2 Alpha Vantage

- 사이트: https://www.alphavantage.co/documentation/
- 용도:
  - 미국 주식 가격
  - 기술지표
  - FX
  - 경제지표
- 구현:
  - API key 발급
  - 호출량 제한을 고려해 캐시 필수

### 5.3 Finnhub

- 사이트: https://finnhub.io/docs/api
- 용도:
  - 회사 뉴스
  - 뉴스 감성
  - 실적 캘린더
  - 기업 기본정보
- 저장 필드:
  - symbol
  - headline
  - source
  - datetime
  - url
  - summary
  - sentiment_score
  - impact_score

### 5.4 SEC EDGAR API

- 사이트: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- 용도:
  - 미국 상장사 공시
  - 10-K, 10-Q, 8-K
  - XBRL 재무 데이터
- 구현:
  - ticker → CIK 매핑
  - submissions JSON 수집
  - companyfacts JSON 수집
- 주의:
  - User-Agent 명시
  - 과도한 요청 금지
  - 원문 전체를 LLM에 넣지 말고 필요한 섹션만 압축

### 5.5 FRED API

- 사이트: https://fred.stlouisfed.org/docs/api/fred/
- 용도:
  - 금리
  - CPI
  - 실업률
  - VIX
  - 장단기 금리차
- 추천 시리즈:
  - DGS10
  - DGS2
  - FEDFUNDS
  - CPIAUCSL
  - UNRATE
  - VIXCLS

---

## 6. Telegram/커뮤니티 구독 데이터

### 6.1 Telegram 수집 방식

- 라이브러리: Telethon
- 문서: https://docs.telethon.dev/
- 수집 방식:
  - 공개 채널 메시지를 주기적으로 수집
  - channel + message_id로 중복 방지
  - 텍스트에서 티커, 종목명, 테마, 감성, 위험 키워드 추출
- 권장 채널:
  - kwusa
  - FastStockNewsUSA
  - mkglobalinvest
  - Barbarianglobal
  - itechkorea
  - insidertracking

### 6.2 Telegram 위험 키워드

```python
RISK_PROMOTION_WORDS = [
    "수익보장", "원금보장", "무료방", "유료방", "리딩방",
    "VIP", "선착순", "비밀정보", "100% 상승", "급등 확정"
]
```

위험 키워드가 있으면 `risk_score`를 올리고 영향도 계산에서 감점한다.

---

## 7. 웹 크롤링 규칙

크롤링은 다음 규칙을 반드시 지킨다.

```text
1. robots.txt와 약관 확인
2. 로그인/유료/우회가 필요한 페이지 수집 금지
3. 기사 전문 저장 금지
4. 제목, URL, 발행시각, 짧은 요약, 키워드만 저장
5. 요청 간격과 rate limit 준수
6. User-Agent 명시
7. 같은 URL 중복 저장 방지
8. 실패 시 무한 retry 금지
9. 저작권 있는 기사 전문을 LLM 프롬프트에 장문으로 넣지 않음
10. 대시보드에는 원문 링크 제공
```

---

## 8. 급등락 탐지 기준

### 8.1 공통 기준

```text
조건 1: abs(today_return) >= 5%
조건 2: abs(today_return) >= 2 * rolling_volatility_20d
조건 3: volume_today >= 2.0 * average_volume_20d
조건 4: trading_value_today >= 2.0 * average_trading_value_20d
조건 5: abs(index_relative_return) >= 3%
```

### 8.2 한국 시장 기준

```text
대형주:
- 등락률 3% 이상
- 거래대금 20일 평균 대비 1.5배 이상
- 외국인/기관 순매수 강도 상위 10%

중소형주:
- 등락률 5% 이상
- 거래량 20일 평균 대비 2배 이상
- 뉴스/공시 발생 여부 필수 확인

상한가/하한가:
- 별도 이벤트로 저장
- 공시/테마/거래정지/투자경고 여부 즉시 확인
```

---

## 9. 영향도 점수 산식

### 9.1 최종 영향도

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

### 9.2 투자자별 매매 영향도

```text
investor_flow_score =
    foreign_net_buy_strength      * 0.45
  + institution_net_buy_strength  * 0.35
  - individual_net_sell_strength  * 0.10
  + consecutive_buy_score         * 0.10
```

### 9.3 뉴스 영향도

```text
news_impact_score =
    recency_score          * 0.30
  + source_reliability     * 0.20
  + sentiment_strength     * 0.20
  + ticker_relevance       * 0.20
  + duplicate_confirmation * 0.10
```

### 9.4 공시 영향도

```text
disclosure_score =
    disclosure_type_weight * 0.40
  + financial_materiality  * 0.30
  + recency_score          * 0.20
  + market_reaction_score  * 0.10
```

---

## 10. 데이터베이스 스키마

### 10.1 large_move_events

```sql
CREATE TABLE IF NOT EXISTS large_move_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    event_date TEXT NOT NULL,
    return_pct REAL,
    index_relative_return_pct REAL,
    volume_ratio_20d REAL,
    trading_value_ratio_20d REAL,
    detected_reason TEXT,
    created_at TEXT,
    UNIQUE(market, ticker, event_date)
);
```

### 10.2 investor_flows

```sql
CREATE TABLE IF NOT EXISTS investor_flows (
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    individual_net_buy REAL,
    foreign_net_buy REAL,
    institution_net_buy REAL,
    financial_investment_net_buy REAL,
    pension_net_buy REAL,
    trust_net_buy REAL,
    private_fund_net_buy REAL,
    other_net_buy REAL,
    source TEXT,
    PRIMARY KEY(market, ticker, trade_date)
);
```

### 10.3 news_items

```sql
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    title TEXT,
    url TEXT UNIQUE,
    published_at TEXT,
    summary TEXT,
    related_tickers TEXT,
    sentiment_score REAL,
    relevance_score REAL,
    impact_score REAL,
    raw_hash TEXT,
    created_at TEXT
);
```

### 10.4 disclosures

```sql
CREATE TABLE IF NOT EXISTS disclosures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT,
    ticker TEXT,
    company_name TEXT,
    report_name TEXT,
    receipt_no TEXT UNIQUE,
    receipt_date TEXT,
    url TEXT,
    disclosure_type TEXT,
    impact_score REAL,
    summary TEXT,
    created_at TEXT
);
```

### 10.5 market_mood

```sql
CREATE TABLE IF NOT EXISTS market_mood (
    market TEXT NOT NULL,
    asof_time TEXT NOT NULL,
    index_return REAL,
    advance_count INTEGER,
    decline_count INTEGER,
    foreign_net_buy REAL,
    institution_net_buy REAL,
    individual_net_buy REAL,
    vix REAL,
    usdkrw REAL,
    us10y REAL,
    mood_score REAL,
    mood_label TEXT,
    PRIMARY KEY(market, asof_time)
);
```

### 10.6 impact_explanations

```sql
CREATE TABLE IF NOT EXISTS impact_explanations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT,
    ticker TEXT,
    event_date TEXT,
    cause_type TEXT,
    cause_title TEXT,
    source_url TEXT,
    evidence_text TEXT,
    impact_score REAL,
    confidence REAL,
    created_at TEXT
);
```

---

## 11. 웹 대시보드 요구사항

MVP는 Streamlit으로 구현한다.

### 11.1 탭 구성

```text
1. 시장 요약
2. 급등락 종목
3. 종목 상세 분석
4. 투자자별 수급
5. 뉴스/공시 영향도
6. Telegram/속보 분위기
7. AI 요약 리포트
8. 데이터 상태/스케줄러
```

### 11.2 시장 요약 탭

표시 항목:

```text
KOSPI/KOSDAQ 등락률
S&P500/NASDAQ 선물 또는 전일 등락률
상승/하락 종목 수
외국인/기관/개인 순매수
업종별 등락률
주요 뉴스 Top 10
시장 분위기 점수
```

### 11.3 급등락 종목 탭

표시 항목:

```text
등락률 상위
거래량 급증
거래대금 급증
수급 급변
뉴스 영향도 상위
공시 영향도 상위
```

### 11.4 종목 상세 분석 탭

표시 항목:

```text
종목명 / 티커
오늘 등락률
거래량 비율
외국인/기관/개인 순매수
관련 뉴스
관련 공시
텔레그램 언급
섹터/테마
영향도 산식 결과
AI 요약
주의 문구
```

---

## 12. Codex 구현 지시 Prompt

아래 내용을 Codex에 그대로 전달한다.

```text
너는 Python 기반 주식 시장 분석 웹 대시보드와 데이터 파이프라인을 구현하는 시니어 엔지니어다.

프로젝트명:
market-impact-ai-robo

목표:
주식의 등락폭이 클 때 그 원인을 자동 분석하고, 시장 분위기, 기관/외국인/개인 매매동향, 뉴스, 공시, 텔레그램 정보를 주기적으로 수집하여 웹 대시보드로 보여주는 시스템을 구현해라.

핵심 요구사항:
1. Python 3.11 이상을 기준으로 작성한다.
2. MVP 웹 대시보드는 Streamlit으로 구현한다.
3. DB는 SQLite로 시작하되 PostgreSQL로 확장 가능한 구조로 만든다.
4. 한국 주식 데이터는 pykrx를 우선 사용한다.
5. 한국 공시는 OpenDART API를 사용한다.
6. 미국 주식 데이터는 yfinance, Alpha Vantage, Finnhub 중 yfinance를 MVP 기본값으로 사용한다.
7. 미국 공시는 SEC EDGAR API 확장 골격을 만든다.
8. 매크로 데이터는 FRED API 확장 골격을 만든다.
9. 뉴스는 Naver Search API, RSS, Finnhub Company News를 수집할 수 있도록 모듈화한다.
10. Telegram은 Telethon으로 공개 채널 메시지를 수집한다.
11. 자동 매매 주문 기능은 구현하지 않는다.
12. 모든 API 키는 .env에서 읽는다.
13. 수집 실패 시 전체 프로그램이 죽지 않도록 예외처리한다.
14. 모든 수집 데이터는 중복 저장을 방지한다.
15. 웹 대시보드에는 투자 유의 문구를 항상 표시한다.

프로젝트 구조:
market-impact-ai-robo/
├── README.md
├── requirements.txt
├── .env.example
├── app.py
├── data/
├── logs/
├── reports/
├── src/
│   ├── config.py
│   ├── db.py
│   ├── collectors/
│   │   ├── collect_krx.py
│   │   ├── collect_pykrx.py
│   │   ├── collect_dart.py
│   │   ├── collect_kis.py
│   │   ├── collect_us_prices.py
│   │   ├── collect_finnhub.py
│   │   ├── collect_sec.py
│   │   ├── collect_fred.py
│   │   ├── collect_news.py
│   │   ├── collect_rss.py
│   │   └── collect_telegram.py
│   ├── features/
│   │   ├── price_features.py
│   │   ├── flow_features.py
│   │   ├── news_features.py
│   │   ├── disclosure_features.py
│   │   ├── macro_features.py
│   │   └── theme_features.py
│   ├── analysis/
│   │   ├── detect_large_moves.py
│   │   ├── score_impact.py
│   │   ├── explain_move.py
│   │   ├── market_mood.py
│   │   └── summarize_ai.py
│   ├── web/
│   │   ├── components.py
│   │   └── charts.py
│   └── scheduler.py
└── tests/

필수 구현:
1. src/db.py
   - SQLite 연결
   - init_db()
   - 모든 테이블 생성

2. src/collectors/collect_pykrx.py
   - 종목별 OHLCV 수집
   - 투자자별 매매동향 수집
   - 지수 데이터 수집
   - 실패 시 로그 기록

3. src/collectors/collect_dart.py
   - OpenDART 공시 검색 골격
   - API key는 .env에서 읽기
   - 주요 공시 유형 분류

4. src/collectors/collect_news.py
   - Naver Search API 기반 뉴스 검색
   - 제목, 링크, description, pubDate 저장
   - 본문 전문 저장 금지

5. src/collectors/collect_rss.py
   - RSS URL 리스트 기반 수집
   - feedparser 사용

6. src/collectors/collect_telegram.py
   - Telethon으로 공개 채널 수집
   - message_id 중복 방지
   - 리딩방/광고 위험 키워드 감지

7. src/analysis/detect_large_moves.py
   - 등락률, 거래량, 지수 대비 수익률 기반 급등락 탐지
   - 조건:
     a. abs(return) >= 5%
     b. abs(return) >= 2 * rolling_volatility_20d
     c. volume_ratio_20d >= 2
     d. abs(index_relative_return) >= 3%

8. src/analysis/score_impact.py
   - 가격, 거래량, 수급, 뉴스, 공시, 섹터, 매크로 점수를 결합
   - final_impact_score 산식 구현

9. src/analysis/explain_move.py
   - 급등락 이벤트별 원인 후보 생성
   - 수급/뉴스/공시/섹터/매크로 중 어떤 요인이 큰지 랭킹
   - 근거 URL과 evidence_text 저장

10. src/analysis/market_mood.py
    - 시장 분위기 점수 계산
    - 외국인/기관/개인 수급, 지수 등락률, 상승/하락 종목 수 반영

11. app.py
    - Streamlit 대시보드
    - 시장 요약 탭
    - 급등락 종목 탭
    - 종목 상세 분석 탭
    - 수급 동향 탭
    - 뉴스/공시 영향도 탭
    - AI 요약 리포트 탭
    - 데이터 상태 탭

12. src/scheduler.py
    - 10분마다 뉴스/텔레그램 수집
    - 장 종료 후 가격/수급/공시 정리
    - 매일 리포트 생성

13. 토큰 최적화
    - LLM에는 원문 전체를 넣지 말고 압축된 Evidence Bundle만 넣는다.
    - 각 종목별 최대 evidence 5개, 각 evidence 500자 이하로 제한한다.
    - prompt에는 DB row 전체를 넣지 않는다.
    - 숫자 피처는 compact JSON으로 전달한다.
    - 같은 뉴스/텔레그램 중복은 hash로 제거한다.

완료 기준:
1. python src/db.py 실행 시 DB와 테이블이 생성된다.
2. python src/collectors/collect_pykrx.py 실행 시 샘플 종목 가격/수급 데이터가 저장된다.
3. python src/analysis/detect_large_moves.py 실행 시 급등락 이벤트가 생성된다.
4. python src/analysis/score_impact.py 실행 시 영향도 점수가 계산된다.
5. streamlit run app.py 실행 시 웹 대시보드가 뜬다.
6. 종목 상세 화면에서 등락 원인, 수급, 뉴스, 공시, 시장 분위기가 보인다.
7. 테스트가 통과한다.
8. README.md에 실행 방법과 데이터 소스별 API 키 발급 방법을 적는다.
```

---

## 13. 최종 실행 순서

```bash
pip install -r requirements.txt

cp .env.example .env
vi .env

python src/db.py

python src/collectors/collect_pykrx.py --date 20260623 --tickers 005930 000660

python src/collectors/collect_news.py --query 삼성전자

python src/collectors/collect_telegram.py

python src/analysis/detect_large_moves.py --date 20260623

python src/analysis/score_impact.py --date 20260623

streamlit run app.py
```

---

## 14. README에 반드시 넣을 투자 유의 문구

```text
본 프로젝트는 투자 참고용 시장 분석 도구입니다.
자동 매매 기능은 포함하지 않습니다.
뉴스/텔레그램/크롤링 데이터에는 오류, 지연, 루머, 중복, 저작권 제한이 있을 수 있습니다.
실제 투자 판단은 공식 공시, 거래소 데이터, 증권사 자료를 함께 확인한 뒤 사용자가 직접 해야 합니다.
```
