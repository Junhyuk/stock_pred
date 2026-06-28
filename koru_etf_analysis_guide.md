# KORU ETF 데이터 수집 및 한국시장 연동 분석 개발 지시서

작성일: 2026-06-23  
대상 프로젝트: `market-impact-ai-robo`  
추가 모듈명: `koru_etf_analysis`

---

## 1. 목적

미국 시장에서 거래되는 `KORU` ETF 정보를 수집하고, 한국시장 데이터와 연결하여 급등락 원인, 한국시장 심리, 외국인 수급, 환율, 반도체 대형주 영향을 함께 분석한다.

`KORU`는 단순한 미국 ETF가 아니라 **한국시장 레버리지 심리 지표**로 활용할 수 있다. 따라서 기존 `market-impact-ai-robo` 프로젝트에 KORU 분석 모듈을 추가한다.

---

## 2. KORU 기본 정보

```text
ticker: KORU
name: Direxion Daily MSCI South Korea Bull 3X ETF
market: US
exchange: NYSE Arca
issuer: Direxion
type: leveraged_etf
underlying_index: MSCI Korea 25/50 Index
target_exposure: 300% daily
related_etf: EWY
```

KORU는 Direxion에서 운용하는 한국시장 3배 레버리지 ETF다. 공식 설명 기준으로 수수료와 비용 차감 전 **MSCI Korea 25/50 Index 일간 성과의 300%**를 목표로 한다.

주의할 점은 `일간` 3배 목표라는 점이다. 장기 보유 시 한국시장 누적 수익률의 단순 3배와 다르게 움직일 수 있다. 변동성, 복리 효과, 일일 리밸런싱 때문에 괴리가 발생할 수 있다.

---

## 3. KORU를 프로젝트에 넣는 이유

### 3.1 한국시장 심리 지표

KORU는 미국 시장에서 거래되지만 한국 주식시장과 밀접하게 연결되어 있다. 특히 미국 장중에 한국 관련 뉴스, 반도체 테마, 환율, 글로벌 위험자산 선호가 KORU 가격에 반영될 수 있다.

### 3.2 한국시장 연동 분석에 유용한 이유

```text
KORU 상승 가능 원인:
- 한국 반도체 대형주 강세
- 삼성전자/SK하이닉스 관련 긍정 뉴스
- 외국인 한국 주식 순매수 확대
- 원화 강세 / 달러 약세
- EWY 상승
- MSCI Korea 관련 글로벌 자금 유입
- 미국 위험자산 선호 심리 회복
- AI/HBM/메모리 업황 개선 뉴스

KORU 하락 가능 원인:
- 한국 반도체 대형주 약세
- 외국인 한국 주식 순매도
- 원화 약세 / 달러 강세
- 미국 금리 상승
- 중국/아시아 리스크 확대
- 글로벌 위험회피 심리
- 레버리지 ETF 특유의 변동성 확대
```

---

## 4. KORU 분석에 필요한 데이터

### 4.1 미국 시장 데이터

| 데이터 | 티커/소스 | 목적 |
|---|---|---|
| KORU 가격/거래량 | KORU | 분석 대상 |
| EWY 가격/거래량 | EWY | 한국시장 대표 ETF 비교 |
| SPY | SPY | 미국 전체 위험자산 분위기 |
| QQQ | QQQ | 기술주/성장주 분위기 |
| VIX | FRED 또는 yfinance | 위험회피 지표 |
| 미국 10년물 금리 | FRED DGS10 | 금리 부담 |
| 달러 인덱스 | DXY 또는 대체 데이터 | 달러 강세/약세 영향 |

### 4.2 한국 시장 데이터

| 데이터 | 소스 | 목적 |
|---|---|---|
| KOSPI | pykrx/KRX | 한국시장 전체 흐름 |
| KOSDAQ | pykrx/KRX | 성장주/중소형 분위기 |
| 삼성전자 | 005930 | KORU 핵심 영향 종목 |
| SK하이닉스 | 000660 | 반도체/HBM 대표 종목 |
| 외국인 순매수 | pykrx/KRX | 글로벌 자금 흐름 |
| 기관 순매수 | pykrx/KRX | 국내 기관 수급 |
| 개인 순매수 | pykrx/KRX | 개인 반대매매/차익실현 확인 |
| 업종별 등락률 | pykrx/KRX | 반도체/전기전자 섹터 영향 |
| USD/KRW | 환율 API/yfinance/KIS | 원화 강세/약세 영향 |

### 4.3 뉴스/속보 데이터

| 데이터 | 소스 | 목적 |
|---|---|---|
| 한국시장 뉴스 | Naver Search API | 삼성전자, SK하이닉스, 반도체 뉴스 |
| 공시 | OpenDART | 한국 상장사 주요 공시 |
| 미국 뉴스 | Finnhub, RSS | 글로벌 ETF/반도체/매크로 뉴스 |
| 텔레그램 | Telethon | 속보성 시장 분위기 |
| SEC/ETF 관련 공시 | SEC EDGAR | 미국 ETF/관련 기업 공시 확장 |

---

## 5. 데이터 수집 구현 방법

### 5.1 yfinance로 KORU 가격 가져오기

MVP에서는 `yfinance`를 사용한다.

```python
import yfinance as yf

def fetch_koru_price(period="6mo", interval="1d"):
    ticker = yf.Ticker("KORU")
    df = ticker.history(period=period, interval=interval)
    return df

df = fetch_koru_price()
print(df.tail())
```

### 5.2 KORU, EWY, SPY, QQQ 비교 수집

```python
import yfinance as yf

def fetch_us_etf_prices(symbols=None, period="6mo", interval="1d"):
    if symbols is None:
        symbols = ["KORU", "EWY", "SPY", "QQQ"]

    data = yf.download(
        symbols,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=True,
        threads=True
    )
    return data

data = fetch_us_etf_prices()
```

### 5.3 수익률 비교 계산

```python
def calculate_return_comparison(data):
    koru_close = data["KORU"]["Close"]
    ewy_close = data["EWY"]["Close"]
    spy_close = data["SPY"]["Close"]
    qqq_close = data["QQQ"]["Close"]

    result = {
        "koru_return_1d": koru_close.pct_change().iloc[-1],
        "ewy_return_1d": ewy_close.pct_change().iloc[-1],
        "spy_return_1d": spy_close.pct_change().iloc[-1],
        "qqq_return_1d": qqq_close.pct_change().iloc[-1],
    }

    if result["ewy_return_1d"] != 0:
        result["koru_to_ewy_ratio"] = result["koru_return_1d"] / result["ewy_return_1d"]
    else:
        result["koru_to_ewy_ratio"] = None

    return result
```

### 5.4 pykrx로 한국시장 데이터 가져오기

```python
from pykrx import stock

def fetch_korea_core_data(date: str):
    kospi = stock.get_index_ohlcv_by_date(date, date, "1001")
    kosdaq = stock.get_index_ohlcv_by_date(date, date, "2001")

    samsung = stock.get_market_ohlcv_by_date(date, date, "005930")
    hynix = stock.get_market_ohlcv_by_date(date, date, "000660")

    samsung_flow = stock.get_market_trading_value_by_investor(date, date, "005930")
    hynix_flow = stock.get_market_trading_value_by_investor(date, date, "000660")

    return {
        "kospi": kospi,
        "kosdaq": kosdaq,
        "samsung": samsung,
        "hynix": hynix,
        "samsung_flow": samsung_flow,
        "hynix_flow": hynix_flow,
    }
```

---

## 6. DB 스키마 추가

### 6.1 ETF 메타데이터 테이블

```sql
CREATE TABLE IF NOT EXISTS etf_metadata (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,
    exchange_name TEXT,
    etf_type TEXT,
    leverage_ratio REAL,
    underlying_index TEXT,
    issuer TEXT,
    related_tickers TEXT,
    updated_at TEXT
);
```

초기 데이터:

```sql
INSERT OR REPLACE INTO etf_metadata
(ticker, name, market, exchange_name, etf_type, leverage_ratio, underlying_index, issuer, related_tickers, updated_at)
VALUES
('KORU', 'Direxion Daily MSCI South Korea Bull 3X ETF', 'US', 'NYSE Arca',
 'leveraged_etf', 3.0, 'MSCI Korea 25/50 Index', 'Direxion',
 'EWY,KOSPI,KOSDAQ,005930,000660,USD/KRW,SPY,QQQ', datetime('now'));
```

### 6.2 ETF 가격 테이블

```sql
CREATE TABLE IF NOT EXISTS etf_price_daily (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume INTEGER,
    source TEXT,
    created_at TEXT,
    PRIMARY KEY(ticker, trade_date)
);
```

### 6.3 KORU 한국시장 연동 분석 테이블

```sql
CREATE TABLE IF NOT EXISTS koru_korea_linkage (
    trade_date TEXT PRIMARY KEY,
    koru_return_pct REAL,
    ewy_return_pct REAL,
    spy_return_pct REAL,
    qqq_return_pct REAL,
    kospi_return_pct REAL,
    kosdaq_return_pct REAL,
    samsung_return_pct REAL,
    hynix_return_pct REAL,
    usdkrw_change_pct REAL,
    foreign_net_buy_krw REAL,
    institution_net_buy_krw REAL,
    individual_net_buy_krw REAL,
    semi_theme_score REAL,
    market_risk_score REAL,
    korea_sentiment_score REAL,
    koru_impact_score REAL,
    summary TEXT,
    created_at TEXT
);
```

---

## 7. KORU 영향도 점수 산식

### 7.1 기본 산식

```text
koru_impact_score =
    ewy_sync_score              * 0.20
  + korea_index_score           * 0.15
  + semiconductor_score         * 0.20
  + foreign_flow_score          * 0.15
  + usdkrw_score                * 0.10
  + us_risk_on_score            * 0.10
  + news_sentiment_score        * 0.10
```

### 7.2 항목별 의미

| 항목 | 의미 |
|---|---|
| ewy_sync_score | EWY와 KORU의 동조화 정도 |
| korea_index_score | KOSPI/KOSDAQ 흐름 |
| semiconductor_score | 삼성전자/SK하이닉스 및 반도체 테마 |
| foreign_flow_score | 외국인 한국시장 순매수 |
| usdkrw_score | 원화 강세/약세 |
| us_risk_on_score | SPY/QQQ/VIX 기반 미국 위험자산 선호 |
| news_sentiment_score | 한국시장/반도체/ETF 관련 뉴스 감성 |

### 7.3 급등락 원인 후보 분류

```python
def classify_koru_move_causes(features: dict) -> list[dict]:
    causes = []

    if features.get("ewy_return_pct", 0) > 1.0:
        causes.append({
            "type": "ETF_SYNC",
            "title": "EWY와 한국시장 ETF 동반 상승",
            "impact": 0.20
        })

    if features.get("samsung_return_pct", 0) > 2.0 or features.get("hynix_return_pct", 0) > 2.0:
        causes.append({
            "type": "SEMICONDUCTOR",
            "title": "삼성전자/SK하이닉스 등 반도체 대형주 강세",
            "impact": 0.20
        })

    if features.get("foreign_net_buy_krw", 0) > 100_000_000_000:
        causes.append({
            "type": "FOREIGN_FLOW",
            "title": "외국인 한국시장 순매수 확대",
            "impact": 0.15
        })

    if features.get("usdkrw_change_pct", 0) < -0.3:
        causes.append({
            "type": "FX",
            "title": "원화 강세에 따른 한국자산 선호 개선",
            "impact": 0.10
        })

    if features.get("spy_return_pct", 0) > 0.5 and features.get("qqq_return_pct", 0) > 0.5:
        causes.append({
            "type": "US_RISK_ON",
            "title": "미국 시장 위험자산 선호 회복",
            "impact": 0.10
        })

    return sorted(causes, key=lambda x: x["impact"], reverse=True)
```

---

## 8. 웹 대시보드 추가 요구사항

기존 Streamlit 대시보드에 `KORU 분석` 탭을 추가한다.

### 8.1 KORU 분석 탭

표시 항목:

```text
KORU 현재가/전일 대비 등락률
KORU 거래량/거래량 급증 여부
KORU vs EWY 수익률 비교
KORU vs KOSPI/KOSDAQ 비교
KORU vs 삼성전자/SK하이닉스 비교
외국인/기관/개인 한국시장 수급
USD/KRW 환율 변화
관련 뉴스/텔레그램 속보
KORU 급등락 원인 Top 5
레버리지 ETF 위험 문구
```

### 8.2 차트 요구사항

```text
1. KORU / EWY / SPY / QQQ 누적 수익률 비교 차트
2. KORU / KOSPI / KOSDAQ 비교 차트
3. 삼성전자 / SK하이닉스 / KORU 비교 차트
4. 외국인 순매수 vs KORU 수익률 차트
5. USD/KRW 변화 vs KORU 수익률 차트
```

### 8.3 레버리지 ETF 위험 문구

KORU 상세 화면에는 반드시 아래 문구를 표시한다.

```text
KORU는 일간 3배 레버리지 ETF입니다.
장기 보유 시 기초지수 수익률의 단순 3배와 다르게 움직일 수 있으며,
변동성, 일일 리밸런싱, 복리 효과로 인해 손실이 확대될 수 있습니다.
본 화면은 투자 참고용 분석이며 매수/매도 추천이 아닙니다.
```

---

## 9. Codex 추가 지시 Prompt

아래 내용을 Codex에 그대로 전달한다.

```text
기존 market-impact-ai-robo 프로젝트에 KORU ETF 분석 기능을 추가해라.

목표:
미국 시장에서 거래되는 KORU ETF를 수집하고, EWY, KOSPI, KOSDAQ, 삼성전자, SK하이닉스, USD/KRW, 외국인/기관/개인 수급과 연결하여 KORU 급등락 원인을 분석하는 기능을 구현한다.

요구사항:
1. 미국 시장 ETF ticker `KORU`를 수집 대상에 포함한다.
2. KORU는 Direxion Daily MSCI South Korea Bull 3X ETF로 분류한다.
3. KORU의 기본 가격 데이터는 yfinance로 MVP 구현한다.
4. KORU와 함께 EWY, SPY, QQQ도 비교 수집한다.
5. KORU 분석 시 한국시장 데이터와 연결한다.
   - KOSPI
   - KOSDAQ
   - 삼성전자 005930
   - SK하이닉스 000660
   - USD/KRW
   - 외국인/기관/개인 수급
6. KORU 급등락 분석에서는 다음 원인 후보를 계산한다.
   - EWY 동조화
   - 한국 반도체 대형주 영향
   - 외국인 한국시장 수급
   - 환율 영향
   - 미국 위험자산 선호
   - 뉴스/텔레그램 한국시장 분위기
7. KORU는 3배 레버리지 ETF이므로 장기 투자 분석보다 단기 변동성/시장심리 분석용으로 표시한다.
8. 웹 대시보드에 `KORU 분석` 탭을 추가한다.
9. 웹 대시보드에 `KORU / EWY / KOSPI 비교` 차트를 추가한다.
10. KORU 상세 화면에는 반드시 레버리지 ETF 위험 문구를 표시한다.
11. 자동 매매 기능은 구현하지 않는다.

추가 파일:
src/collectors/collect_koru.py
src/features/koru_features.py
src/analysis/analyze_koru.py
src/web/koru_tab.py
tests/test_koru_features.py
tests/test_koru_analysis.py

DB 추가:
- etf_metadata
- etf_price_daily
- koru_korea_linkage

필수 함수:
1. fetch_koru_price(period="6mo", interval="1d")
2. fetch_us_etf_prices(symbols=["KORU", "EWY", "SPY", "QQQ"])
3. calculate_koru_return_comparison(data)
4. build_koru_korea_linkage(date)
5. calculate_koru_impact_score(features)
6. classify_koru_move_causes(features)
7. render_koru_tab()

완료 기준:
1. python src/collectors/collect_koru.py 실행 시 KORU, EWY, SPY, QQQ 가격 데이터가 저장된다.
2. python src/analysis/analyze_koru.py --sample 실행 시 KORU 원인 분석 결과가 출력된다.
3. streamlit run app.py 실행 시 KORU 분석 탭이 보인다.
4. KORU 분석 탭에 KORU/EWY/KOSPI 비교 차트가 표시된다.
5. KORU 상세 화면에 레버리지 ETF 위험 문구가 표시된다.
6. tests/test_koru_features.py와 tests/test_koru_analysis.py가 통과한다.
```

---

## 10. Harness 추가 지시

기존 Harness에 아래 항목을 추가한다.

```yaml
required_files:
  - src/collectors/collect_koru.py
  - src/features/koru_features.py
  - src/analysis/analyze_koru.py
  - src/web/koru_tab.py
  - tests/test_koru_features.py
  - tests/test_koru_analysis.py

commands:
  validation:
    - python src/collectors/collect_koru.py --sample
    - python src/analysis/analyze_koru.py --sample
    - pytest tests/test_koru_features.py tests/test_koru_analysis.py -q

koru_required_outputs:
  - KORU
  - EWY
  - KOSPI
  - KOSDAQ
  - 005930
  - 000660
  - USD/KRW
  - leverage_warning

forbidden:
  - auto_trade
  - place_order
  - buy_market
  - sell_market
```

---

## 11. 샘플 테스트 데이터

```json
{
  "date": "2026-06-23",
  "koru_return_pct": 6.2,
  "ewy_return_pct": 2.1,
  "spy_return_pct": 0.7,
  "qqq_return_pct": 0.9,
  "kospi_return_pct": 1.4,
  "kosdaq_return_pct": 0.8,
  "samsung_return_pct": 2.7,
  "hynix_return_pct": 3.2,
  "usdkrw_change_pct": -0.4,
  "foreign_net_buy_krw": 180000000000,
  "institution_net_buy_krw": 60000000000,
  "individual_net_buy_krw": -240000000000,
  "news_sentiment_score": 0.72
}
```

이 샘플에서는 다음 원인 후보가 나와야 한다.

```text
1. 삼성전자/SK하이닉스 등 반도체 대형주 강세
2. EWY와 한국시장 ETF 동반 상승
3. 외국인 한국시장 순매수 확대
4. 원화 강세에 따른 한국자산 선호 개선
5. 미국 시장 위험자산 선호 회복
```

---

## 12. 최종 실행 예시

```bash
# KORU 관련 패키지 포함 설치
pip install -r requirements.txt

# DB 초기화
python src/db.py

# KORU 가격 수집
python src/collectors/collect_koru.py

# 한국시장 데이터 수집
python src/collectors/collect_pykrx.py --date 20260623 --tickers 005930 000660

# KORU 분석
python src/analysis/analyze_koru.py --date 20260623

# 웹 대시보드 실행
streamlit run app.py
```

---

## 13. 결론

KORU는 한국시장과 미국시장 사이를 연결하는 유용한 분석 대상이다. 특히 한국 반도체 대형주, 외국인 수급, 원화 흐름, 미국 위험자산 선호를 함께 보면 KORU의 급등락 원인을 더 설득력 있게 설명할 수 있다.

다만 KORU는 일간 3배 레버리지 ETF이므로 장기 투자 적합성 판단보다는 단기 변동성, 시장심리, 한국 관련 레버리지 자금 흐름 분석용으로 사용하는 것이 적절하다.
