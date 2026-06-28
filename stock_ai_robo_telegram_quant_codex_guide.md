# 미국 주식 AI Robo / Quant 프로젝트 개발 지시서

작성일: 2026-06-21  
목적: Telegram 미국 주식/매크로 채널을 주기적으로 수집하고, 가격·거래량·공시·매크로 데이터와 결합하여 AI Robo 또는 퀀트 기반 관심종목 랭킹/예측 시스템을 구축한다.

> 주의: 이 프로젝트는 투자 참고용 데이터 분석 시스템이다. 자동 매매 신호로 바로 사용하지 말고, 초기 버전은 반드시 `관심종목 랭킹 + 근거 요약 + 리스크 경고` 형태로 구현한다.

---

## 1. 프로젝트 목표

### 1.1 핵심 목표

Telegram 공개 채널의 짧은 미국 주식 뉴스/리서치/속보 텍스트를 주기적으로 수집하고, 아래 데이터와 결합한다.

- 미국 주식 가격/거래량/OHLCV
- 티커별 뉴스/감성 데이터
- SEC 공시/재무 데이터
- FRED 매크로 데이터
- Telegram 채널별 언급량, 감성, 키워드, 이벤트성 점수

최종 출력은 다음 형태로 한다.

```text
관심 티커 Top 10
- 상승확률
- 변동성 위험도
- 최근 언급량 증가
- 긍정/부정 감성
- 근거 Telegram 글 3개
- 관련 뉴스/공시/매크로 근거
- 리스크 키워드
```

### 1.2 하지 말아야 할 것

- Telegram 글만 보고 매수/매도 신호를 생성하지 않는다.
- “수익 보장”, “급등 확정” 같은 표현을 쓰지 않는다.
- 개인 채널의 내용을 공식 데이터처럼 취급하지 않는다.
- 사칭 채널, 유료 리딩방, 송금 유도 채널은 수집 대상에서 제외한다.
- 운영 초기에는 자동 매매 주문 기능을 넣지 않는다.

---

## 2. 전체 아키텍처

```text
Telegram 공개 채널
   ↓
Python Telethon 수집기
   ↓
SQLite / PostgreSQL 저장
   ↓
티커 추출 + 키워드 분류 + 감성 점수화
   ↓
가격/거래량 API + SEC 공시 + FRED 매크로 데이터 결합
   ↓
피처 엔지니어링
   ↓
LightGBM / XGBoost / RandomForest 모델
   ↓
AI Robo 관심종목 랭킹
   ↓
리포트 / 웹 대시보드 / 알림
```

### 2.1 권장 MVP 구조

```text
수집 채널:
- kwusa
- FastStockNewsUSA
- mkglobalinvest

가격:
- yfinance 또는 Alpha Vantage

매크로:
- FRED

공시:
- SEC EDGAR

DB:
- SQLite

모델:
- LightGBM 또는 XGBoost

출력:
- 관심 티커 Top 10
- 상승확률
- 근거 텍스트 3개
- 리스크 키워드
```

---

## 3. 추천 Telegram 채널

| 우선순위 | 채널 | 주소 | 수집 목적 |
|---|---|---|---|
| 1 | 키움증권 미국주식 톡톡 | `https://t.me/kwusa` | 증권사 리서치/미국주식 주요 뉴스. 데이터 품질이 비교적 안정적이므로 1차 수집 대상 |
| 2 | 급등일보 미국주식 속보·매크로·리서치 | `https://t.me/FastStockNewsUSA` | 속보성, 매크로, 개별 종목 이벤트 감지용 |
| 3 | 매경 월가월부 | `https://t.me/mkglobalinvest` | 시장 해설, 빅테크, 금리, ETF, 미국시장 분위기 요약용 |
| 4 | The Barbarian 해외주식 | `https://t.me/Barbarianglobal` | AI, 반도체, 전력, 클라우드 등 테마 분석용 |
| 5 | 미국주식과 투자이야기 | `https://t.me/itechkorea` | 장전/마감 브리핑, 매크로 이벤트, 시장 분위기 요약용 |
| 6 | 미국 주식 인사이더 | `https://t.me/insidertracking` | 내부자 거래/관심 종목 이벤트 감지용. SEC Form 4 등으로 재검증 필요 |

### 3.1 MVP에서는 3개만 먼저 사용

초기 버전에서는 아래 3개만 수집한다.

```text
kwusa
FastStockNewsUSA
mkglobalinvest
```

이유:

- 데이터 품질 확인이 쉽다.
- 중복/루머/재가공 글이 과도하게 들어오는 것을 막을 수 있다.
- 모델 피처 품질을 먼저 검증할 수 있다.

---

## 4. 외부 데이터 API 추천

| 데이터 | 추천 API | 용도 |
|---|---|---|
| 가격/거래량/OHLCV | Alpha Vantage, Polygon, Finnhub, yfinance | 일봉/분봉, 수익률, 변동성, 거래량 피처 |
| 뉴스/감성 | Finnhub | 회사 뉴스, 뉴스 감성, 실적 캘린더 |
| 공시/재무 | SEC EDGAR API | 10-K, 10-Q, 8-K, Form 4, XBRL 재무 데이터 |
| 매크로 | FRED API | 금리, CPI, 실업률, VIX, 장단기 금리차 |
| 실험용 가격 | yfinance | 빠른 프로토타입용. 운영 서비스에는 공식/유료 API 권장 |

### 4.1 참고 링크

- Telegram Channels FAQ: https://telegram.org/faq_channels
- Telegram API - Channels: https://core.telegram.org/api/channel
- Telethon Documentation: https://docs.telethon.dev/
- Alpha Vantage API: https://www.alphavantage.co/documentation/
- Finnhub API: https://finnhub.io/docs/api
- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- FRED API: https://fred.stlouisfed.org/docs/api/fred/
- yfinance Documentation: https://ranaroussi.github.io/yfinance/

---

## 5. 데이터베이스 설계

### 5.1 telegram_posts

Telegram 원문 저장 테이블.

```sql
CREATE TABLE IF NOT EXISTS telegram_posts (
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    date_utc TEXT,
    text TEXT,
    tickers TEXT,
    url TEXT,
    inserted_at TEXT,
    PRIMARY KEY (channel, message_id)
);
```

### 5.2 ticker_mentions

티커별 언급량 집계 테이블.

```sql
CREATE TABLE IF NOT EXISTS ticker_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    date_utc TEXT,
    sentiment_score REAL,
    urgency_score REAL,
    theme TEXT,
    source_weight REAL,
    created_at TEXT
);
```

### 5.3 price_daily

일봉 가격 데이터.

```sql
CREATE TABLE IF NOT EXISTS price_daily (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume INTEGER,
    source TEXT,
    PRIMARY KEY (ticker, date)
);
```

### 5.4 macro_daily

매크로 데이터.

```sql
CREATE TABLE IF NOT EXISTS macro_daily (
    date TEXT NOT NULL,
    series_id TEXT NOT NULL,
    value REAL,
    source TEXT,
    PRIMARY KEY (date, series_id)
);
```

### 5.5 model_features

모델 학습용 피처 테이블.

```sql
CREATE TABLE IF NOT EXISTS model_features (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    mention_count_1h INTEGER,
    mention_count_24h INTEGER,
    sentiment_avg_24h REAL,
    urgency_avg_24h REAL,
    source_weighted_score REAL,
    return_1d REAL,
    return_5d REAL,
    volume_ratio_20d REAL,
    volatility_20d REAL,
    spy_relative_return_1d REAL,
    macro_rate_10y REAL,
    macro_vix REAL,
    label_outperform_spy_next_1d INTEGER,
    PRIMARY KEY (ticker, asof_date)
);
```

---

## 6. Python 패키지

### 6.1 requirements.txt

```txt
telethon
pandas
numpy
requests
python-dotenv
apscheduler
yfinance
scikit-learn
lightgbm
xgboost
sqlalchemy
beautifulsoup4
lxml
```

선택 패키지:

```txt
transformers
torch
sentencepiece
```

FinBERT 등 금융 감성 모델을 붙일 경우 사용한다.

---

## 7. 환경변수

### 7.1 .env 예시

```env
TELEGRAM_API_ID=your_telegram_api_id
TELEGRAM_API_HASH=your_telegram_api_hash

ALPHA_VANTAGE_API_KEY=your_alpha_vantage_key
FINNHUB_API_KEY=your_finnhub_key
FRED_API_KEY=your_fred_key

DB_PATH=market_telegram.db
```

### 7.2 Telegram API ID / HASH

Telegram API ID와 API HASH는 Telegram 개발자 페이지에서 발급받는다.

- https://my.telegram.org

---

## 8. Telegram 수집기 예시 코드

파일명: `collect_telegram.py`

```python
import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
DB_PATH = os.getenv("DB_PATH", "market_telegram.db")

CHANNELS = [
    "kwusa",
    "FastStockNewsUSA",
    "mkglobalinvest",
]

TICKER_PATTERN = re.compile(r"\b[A-Z]{1,5}\b|#[A-Z]{1,5}")

IGNORE_WORDS = {
    "AI", "CEO", "CPI", "PCE", "FOMC", "ETF", "EPS", "GDP",
    "USA", "USD", "SEC", "FED", "PMI", "ISM", "IPO"
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_posts (
            channel TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            date_utc TEXT,
            text TEXT,
            tickers TEXT,
            url TEXT,
            inserted_at TEXT,
            PRIMARY KEY (channel, message_id)
        )
    """)
    conn.commit()
    conn.close()

def extract_tickers(text: str) -> list[str]:
    if not text:
        return []

    raw = TICKER_PATTERN.findall(text)
    tickers = sorted({
        x.replace("#", "").upper()
        for x in raw
        if x.replace("#", "").upper() not in IGNORE_WORDS
    })

    return tickers

async def fetch_channel(client: TelegramClient, channel: str, limit: int = 100):
    conn = sqlite3.connect(DB_PATH)

    row = conn.execute(
        "SELECT COALESCE(MAX(message_id), 0) FROM telegram_posts WHERE channel = ?",
        (channel,)
    ).fetchone()

    last_id = row[0] or 0

    async for msg in client.iter_messages(
        channel,
        limit=limit,
        min_id=last_id,
        reverse=True
    ):
        text = msg.message or ""
        tickers = ",".join(extract_tickers(text))
        url = f"https://t.me/{channel}/{msg.id}"

        conn.execute("""
            INSERT OR IGNORE INTO telegram_posts
            (channel, message_id, date_utc, text, tickers, url, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            channel,
            msg.id,
            msg.date.astimezone(timezone.utc).isoformat() if msg.date else None,
            text,
            tickers,
            url,
            datetime.now(timezone.utc).isoformat()
        ))

    conn.commit()
    conn.close()

async def main():
    init_db()

    async with TelegramClient("stock_robo_session", API_ID, API_HASH) as client:
        for channel in CHANNELS:
            try:
                await fetch_channel(client, channel, limit=100)
                print(f"OK: {channel}")
            except Exception as e:
                print(f"FAIL: {channel}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 9. 주기 실행

### 9.1 cron 방식

10분마다 Telegram 데이터를 수집한다.

```bash
*/10 * * * * cd /home/stock_robo && /usr/bin/python3 collect_telegram.py >> logs/telegram.log 2>&1
```

### 9.2 APScheduler 방식

파일명: `scheduler.py`

```python
from apscheduler.schedulers.blocking import BlockingScheduler
import subprocess

scheduler = BlockingScheduler(timezone="Asia/Seoul")

@scheduler.scheduled_job("interval", minutes=10)
def run_collector():
    subprocess.run(["python3", "collect_telegram.py"], check=False)

scheduler.start()
```

---

## 10. 텍스트 피처 설계

Telegram 텍스트는 원문 그대로 모델에 넣지 말고 숫자 피처로 변환한다.

| 피처 | 설명 |
|---|---|
| mention_count_1h | 최근 1시간 특정 티커 언급 횟수 |
| mention_count_24h | 최근 24시간 특정 티커 언급 횟수 |
| mention_delta_24h | 전일 대비 언급량 증가율 |
| sentiment_score | 긍정/부정/중립 점수 |
| urgency_score | 속보성/이벤트성 점수 |
| source_weight | 채널 신뢰도 가중치 |
| keyword_theme | AI, 반도체, 금리, 유가, 전력, 방산, 바이오 등 |
| duplicate_score | 같은 내용을 여러 채널이 반복했는지 |
| price_reaction_30m | 글 게시 후 30분 수익률 |
| price_reaction_1d | 글 게시 후 다음 거래일 수익률 |
| volume_spike | 평소 대비 거래량 증가율 |
| macro_regime | 금리 상승/하락, VIX 상승/하락, 달러 강세/약세 |

---

## 11. 키워드 분류 예시

### 11.1 테마 키워드

```python
THEME_KEYWORDS = {
    "AI": ["AI", "인공지능", "데이터센터", "GPU", "LLM", "엔비디아", "NVIDIA"],
    "SEMICONDUCTOR": ["반도체", "HBM", "DRAM", "파운드리", "TSMC", "마이크론", "Micron"],
    "RATE": ["금리", "FOMC", "10년물", "국채", "연준", "Fed", "yield"],
    "ENERGY": ["유가", "원유", "WTI", "천연가스", "전력", "전력망"],
    "BIO": ["FDA", "임상", "신약", "바이오", "제약"],
    "DEFENSE": ["방산", "국방", "미사일", "드론"],
    "CRYPTO": ["비트코인", "BTC", "이더리움", "ETH", "코인"]
}
```

### 11.2 긴급도 키워드

```python
URGENCY_KEYWORDS = {
    "HIGH": ["속보", "급등", "급락", "서프라이즈", "가이던스", "인수", "합병", "SEC", "소송"],
    "MEDIUM": ["실적", "매출", "EPS", "목표가", "상향", "하향", "리포트"],
    "LOW": ["전망", "분석", "코멘트", "브리핑"]
}
```

---

## 12. 감성 분석 전략

### 12.1 1단계: 룰 기반

MVP에서는 복잡한 LLM보다 룰 기반 점수부터 구현한다.

```python
POSITIVE_WORDS = ["상향", "호조", "강세", "서프라이즈", "수혜", "성장", "돌파", "최고"]
NEGATIVE_WORDS = ["하향", "부진", "약세", "쇼크", "규제", "소송", "감소", "둔화"]

def simple_sentiment(text: str) -> float:
    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)

    if pos + neg == 0:
        return 0.0

    return (pos - neg) / (pos + neg)
```

### 12.2 2단계: FinBERT 또는 금융 특화 모델

MVP 이후에는 FinBERT 계열 모델로 영어 뉴스/리포트 감성 점수를 추가한다.

권장 방식:

- Telegram 한국어 텍스트는 룰 기반 + 키워드 분류
- 영어 원문 뉴스는 FinBERT 감성 분석
- 최종 점수는 채널 가중치와 함께 결합

---

## 13. 라벨 설계

초기 분류 라벨은 아래처럼 단순하게 만든다.

### 13.1 다음 거래일 SPY 대비 초과수익 여부

```text
label = 1 if ticker_next_1d_return > spy_next_1d_return else 0
```

### 13.2 다음 5거래일 SPY 대비 초과수익 여부

```text
label = 1 if ticker_next_5d_return > spy_next_5d_return else 0
```

### 13.3 변동성 경고 라벨

```text
label = 1 if abs(ticker_next_1d_return) > 3% else 0
```

---

## 14. 모델 추천

### 14.1 MVP 모델

아래 순서로 개발한다.

1. Logistic Regression
2. RandomForest
3. LightGBM
4. XGBoost

처음부터 LSTM/Transformer로 가지 않는다. Telegram 데이터는 노이즈가 많기 때문에 피처 품질 검증이 먼저다.

### 14.2 성능 평가 지표

| 지표 | 설명 |
|---|---|
| Accuracy | 방향성 예측 정확도 |
| Precision@10 | Top 10 추천 티커의 적중률 |
| ROC-AUC | 확률 예측 품질 |
| Backtest Return | 단순 랭킹 전략 수익률 |
| Max Drawdown | 최대 낙폭 |
| Turnover | 종목 교체율 |
| Hit Ratio | 추천 종목 중 양수 수익률 비율 |

---

## 15. 출력 리포트 예시

```text
[AI Robo 미국주식 관심종목 리포트]

기준일: 2026-06-21

1. NVDA
- 상승확률: 63%
- 언급량 24h: 12건
- 감성 점수: +0.42
- 주요 테마: AI, GPU, 데이터센터
- 근거:
  1) FastStockNewsUSA: 데이터센터 수요 관련 속보
  2) mkglobalinvest: 빅테크 AI 투자 확대 언급
  3) kwusa: 반도체 섹터 리서치
- 리스크:
  - 고평가
  - 금리 상승
  - 차익실현 가능성

2. MU
- 상승확률: 58%
- 언급량 24h: 7건
- 감성 점수: +0.31
- 주요 테마: HBM, DRAM, 반도체
```

---

## 16. 프로젝트 디렉터리 구조

```text
stock-ai-robo/
├── README.md
├── requirements.txt
├── .env.example
├── data/
│   └── market_telegram.db
├── logs/
│   └── telegram.log
├── src/
│   ├── config.py
│   ├── db.py
│   ├── collect_telegram.py
│   ├── collect_prices.py
│   ├── collect_macro.py
│   ├── collect_sec.py
│   ├── text_features.py
│   ├── build_features.py
│   ├── train_model.py
│   ├── predict_rank.py
│   └── report.py
├── notebooks/
│   └── eda_telegram_features.ipynb
└── tests/
    ├── test_ticker_extract.py
    ├── test_sentiment.py
    └── test_feature_build.py
```

---

## 17. Codex 개발 지시 Prompt

아래 내용을 Codex에 그대로 전달한다.

```text
너는 Python 기반 미국 주식 AI Robo / Quant 데이터 파이프라인을 개발하는 시니어 엔지니어다.

목표:
Telegram 공개 채널에서 미국 주식 관련 짧은 뉴스/리서치/속보 텍스트를 주기적으로 수집하고, 이를 가격/거래량/매크로/공시 데이터와 결합하여 관심종목 랭킹과 예측 피처를 생성하는 프로젝트를 구현해라.

프로젝트 이름:
stock-ai-robo

핵심 요구사항:
1. Python 3.11 이상 기준으로 작성한다.
2. Telegram 수집은 Telethon을 사용한다.
3. 초기 수집 채널은 아래 3개로 한다.
   - kwusa
   - FastStockNewsUSA
   - mkglobalinvest
4. 수집한 Telegram 메시지는 SQLite에 저장한다.
5. message_id와 channel을 조합하여 중복 저장을 방지한다.
6. 메시지에서 미국 주식 티커를 추출한다.
7. 티커 추출 시 AI, CEO, CPI, PCE, FOMC, ETF, EPS, GDP, USA, USD, SEC, FED 같은 일반 약어는 제외한다.
8. 텍스트에서 테마 키워드를 분류한다.
   - AI
   - SEMICONDUCTOR
   - RATE
   - ENERGY
   - BIO
   - DEFENSE
   - CRYPTO
9. 텍스트에서 룰 기반 감성 점수를 계산한다.
10. 텍스트에서 urgency_score를 계산한다.
11. yfinance 또는 Alpha Vantage를 이용해 티커별 일봉 가격 데이터를 수집하는 모듈을 만든다.
12. FRED API를 통해 매크로 데이터를 수집하는 모듈을 만든다.
13. SEC EDGAR API를 통해 공시 데이터를 확장할 수 있도록 collect_sec.py 골격을 만든다.
14. model_features 테이블을 생성하고, 티커별 1일/5일 수익률, 거래량 증가율, 언급량, 감성 점수, 매크로 피처를 결합한다.
15. 첫 번째 모델은 Logistic Regression 또는 RandomForest로 구현한다.
16. 이후 LightGBM/XGBoost로 확장 가능한 구조로 만든다.
17. predict_rank.py는 관심 티커 Top 10을 출력해야 한다.
18. report.py는 Markdown 리포트를 생성해야 한다.
19. 절대 자동 매매 주문 기능은 넣지 않는다.
20. 모든 API 키와 Telegram 인증값은 .env에서 읽는다.

디렉터리 구조:
stock-ai-robo/
├── README.md
├── requirements.txt
├── .env.example
├── data/
├── logs/
├── src/
│   ├── config.py
│   ├── db.py
│   ├── collect_telegram.py
│   ├── collect_prices.py
│   ├── collect_macro.py
│   ├── collect_sec.py
│   ├── text_features.py
│   ├── build_features.py
│   ├── train_model.py
│   ├── predict_rank.py
│   └── report.py
└── tests/

구현 세부사항:
- src/config.py:
  - dotenv로 환경변수 로드
  - TELEGRAM_API_ID, TELEGRAM_API_HASH, DB_PATH, API 키 관리
- src/db.py:
  - SQLite 연결 함수
  - init_db() 구현
  - telegram_posts, ticker_mentions, price_daily, macro_daily, model_features 테이블 생성
- src/collect_telegram.py:
  - Telethon으로 채널 메시지 수집
  - 마지막 message_id 이후 글만 수집
  - URL은 https://t.me/{channel}/{message_id} 형태로 저장
- src/text_features.py:
  - extract_tickers()
  - classify_theme()
  - simple_sentiment()
  - urgency_score()
- src/collect_prices.py:
  - yfinance 기반 일봉 수집
  - price_daily 테이블에 저장
- src/collect_macro.py:
  - FRED API 골격 구현
  - 주요 series_id: DGS10, DGS2, CPIAUCSL, UNRATE, VIXCLS
- src/build_features.py:
  - Telegram 언급량과 가격 데이터를 결합
  - label_outperform_spy_next_1d 생성
- src/train_model.py:
  - scikit-learn 기반 모델 학습
  - train/test split
  - accuracy, precision, ROC-AUC 출력
- src/predict_rank.py:
  - 최근 피처 기준으로 상승확률 또는 관심점수 Top 10 출력
- src/report.py:
  - Markdown 리포트 생성

테스트:
- tests/test_ticker_extract.py:
  - AAPL, NVDA, MSFT 같은 티커는 추출되어야 한다.
  - AI, CEO, CPI, ETF 같은 단어는 제외되어야 한다.
- tests/test_sentiment.py:
  - 긍정 키워드가 많으면 양수
  - 부정 키워드가 많으면 음수
- tests/test_feature_build.py:
  - 최소 샘플 데이터로 model_features 생성이 동작해야 한다.

완료 기준:
1. python src/db.py 실행 시 SQLite DB와 테이블이 생성된다.
2. python src/collect_telegram.py 실행 시 3개 채널에서 메시지가 수집된다.
3. 중복 실행해도 같은 메시지가 중복 저장되지 않는다.
4. python src/text_features.py 또는 테스트 실행 시 티커/감성/테마 추출이 동작한다.
5. python src/collect_prices.py --tickers AAPL NVDA MSFT 실행 시 가격 데이터가 저장된다.
6. python src/build_features.py 실행 시 model_features가 생성된다.
7. python src/train_model.py 실행 시 모델 학습 결과가 출력된다.
8. python src/predict_rank.py 실행 시 관심 티커 Top 10이 출력된다.
9. python src/report.py 실행 시 reports/daily_report.md가 생성된다.
10. README.md에 설치 방법, .env 설정 방법, 실행 순서, 주의사항을 작성한다.

주의사항:
- 이 시스템은 투자 참고용이다.
- Telegram 데이터는 루머/중복/재가공 정보가 많으므로 source_weight와 duplicate_score를 고려한다.
- 자동 매매 기능은 구현하지 않는다.
- 사칭 채널이나 유료 리딩방 유도 메시지는 별도 위험 키워드로 분류한다.
- 예측 결과에는 반드시 근거 텍스트와 리스크 문구를 함께 출력한다.

먼저 전체 파일 구조를 만들고, MVP가 실행 가능한 상태까지 구현해라.
```

---

## 18. Codex 추가 개선 Prompt

MVP 구현 이후 아래 프롬프트로 고도화한다.

```text
위 stock-ai-robo 프로젝트를 고도화해라.

추가 요구사항:
1. Telegram 메시지의 중복/유사도 제거 기능을 추가한다.
2. 같은 뉴스가 여러 채널에 반복될 경우 duplicate_score를 계산한다.
3. 티커 추출 정확도를 높이기 위해 미국 상장 티커 마스터 파일을 사용한다.
4. 종목명과 티커 매핑을 지원한다.
   예: 엔비디아 → NVDA, 테슬라 → TSLA, 애플 → AAPL
5. source_weight를 채널별로 다르게 적용한다.
   - kwusa: 1.0
   - mkglobalinvest: 0.8
   - FastStockNewsUSA: 0.7
   - 개인/불명확 채널: 0.5 이하
6. report.py에 아래 항목을 추가한다.
   - 오늘의 시장 요약
   - 가장 많이 언급된 티커
   - 언급량 급증 티커
   - 긍정 감성 상위 티커
   - 부정 감성 상위 티커
   - 리스크 키워드 상위
7. 예측 모델은 LightGBM을 기본 모델로 추가한다.
8. 백테스트 모듈 backtest.py를 추가한다.
9. 백테스트는 Top 10 equal weight 전략으로 구현한다.
10. 성과지표는 누적수익률, CAGR, MDD, Hit Ratio, Turnover를 출력한다.
11. Streamlit 대시보드 app.py를 추가한다.
12. 대시보드에는 관심종목 랭킹, 근거 메시지, 최근 가격 차트, 리스크 키워드를 보여준다.

완료 기준:
- python src/backtest.py 실행 시 백테스트 결과가 출력된다.
- streamlit run app.py 실행 시 대시보드가 실행된다.
- 보고서와 대시보드에는 투자 유의 문구가 포함된다.
```

---

## 19. 운영 체크리스트

### 19.1 매일 확인할 것

- Telegram 수집 로그에 에러가 없는지
- DB에 신규 메시지가 저장되는지
- 중복 메시지가 과도하게 늘지 않는지
- 티커 추출 오류가 없는지
- 가격 데이터 수집이 정상인지
- Top 10 추천 종목의 근거가 실제로 존재하는지

### 19.2 매주 확인할 것

- 모델 예측 성능
- 백테스트 수익률
- 추천 종목 Hit Ratio
- 특정 채널의 노이즈 비율
- 사칭/리딩방 유도 메시지 유입 여부

### 19.3 매월 확인할 것

- 채널별 source_weight 재조정
- 모델 재학습
- API 비용/호출량 확인
- 데이터베이스 백업
- 신규 채널 추가/제외 검토

---

## 20. 리스크 관리

### 20.1 데이터 리스크

Telegram 데이터는 다음 문제가 있다.

- 루머 가능성
- 중복 글
- 재가공 글
- 광고/유료방 유도
- 사칭 채널
- 특정 종목 편향
- 장 종료 후 뒤늦은 뉴스 재공유

따라서 반드시 아래 보정값을 둔다.

```text
final_signal_score =
    mention_score * 0.25
  + sentiment_score * 0.20
  + source_weighted_score * 0.20
  + price_momentum_score * 0.20
  + macro_score * 0.10
  - risk_penalty * 0.15
```

### 20.2 투자 리스크 문구

리포트 하단에 반드시 아래 문구를 넣는다.

```text
본 리포트는 Telegram 공개 채널, 가격 데이터, 매크로 데이터, 공시 데이터를 기반으로 한 자동 분석 결과입니다.
투자 권유가 아니며, 실제 투자 판단과 책임은 사용자 본인에게 있습니다.
Telegram 기반 정보에는 루머, 중복, 지연, 재가공 정보가 포함될 수 있으므로 반드시 공식 공시와 가격 데이터를 함께 확인해야 합니다.
```

---

## 21. 추천 개발 순서

### Phase 1: 데이터 수집 MVP

- SQLite DB 생성
- Telegram 3개 채널 수집
- 중복 방지
- 티커 추출
- 감성/테마/긴급도 점수화

### Phase 2: 가격/매크로 결합

- yfinance 또는 Alpha Vantage로 가격 수집
- FRED 매크로 수집
- SPY 대비 초과수익 라벨 생성

### Phase 3: 모델 학습

- Logistic Regression
- RandomForest
- LightGBM
- Precision@10 평가

### Phase 4: 리포트

- Markdown 일간 리포트
- 관심 티커 Top 10
- 근거 메시지
- 리스크 키워드

### Phase 5: 대시보드

- Streamlit
- 티커별 근거 메시지
- 가격 차트
- 피처 테이블
- 백테스트 결과

---

## 22. 최종 MVP 실행 순서

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
vi .env

# 3. DB 생성
python src/db.py

# 4. Telegram 데이터 수집
python src/collect_telegram.py

# 5. 가격 데이터 수집
python src/collect_prices.py --tickers AAPL NVDA MSFT TSLA AMZN GOOGL META

# 6. 매크로 데이터 수집
python src/collect_macro.py

# 7. 피처 생성
python src/build_features.py

# 8. 모델 학습
python src/train_model.py

# 9. 관심종목 랭킹 출력
python src/predict_rank.py

# 10. 리포트 생성
python src/report.py
```

---

## 23. 결론

이 프로젝트는 Telegram을 “예측 정답지”로 사용하는 것이 아니라, 시장 이벤트와 투자자 관심도를 빠르게 감지하는 텍스트 센서로 사용하는 구조가 적합하다.

초기에는 아래 세 가지를 가장 중요하게 본다.

1. 수집 안정성
2. 티커/키워드 추출 정확도
3. 추천 결과의 근거와 리스크 표시

자동 매매는 나중 단계로 미루고, 먼저 `관심종목 랭킹 + 근거 요약 + 백테스트`까지 완성하는 것을 목표로 한다.
