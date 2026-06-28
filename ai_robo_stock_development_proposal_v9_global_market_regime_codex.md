# AI 로보 주식 예측 서비스 v9
## 글로벌 시장 Regime Filter + 해외지수 Feature + Backtest 개선 Codex 개발 지시서

> 목적  
> 국내 종목 예측 모델이 전일 미국 시장 급락과 같은 글로벌 충격을 놓치지 않도록 개선한다.  
> 기존 국내 가격·수급·뉴스·공시·애널리스트 데이터에 해외 지수, 변동성, 금리, 환율, 원자재, 반도체 지수를 결합한다.  
> 로컬 Ubuntu에서 수집 → feature 생성 → 예측 → backtest A/B 비교 → 채택 또는 제거까지 구현한다.  
> Cafe24에는 최종 snapshot, API, UI만 배포한다.

---

# 0. 문제 정의

## 0.1 실패 사례

2026-06-05 미국 장 마감 기준:

| 지수 | 일간 변동률 |
|---|---:|
| Nasdaq Composite | -4.18% |
| S&P 500 | -2.64% |
| Dow Jones Industrial Average | -1.35% |

기술주와 반도체 종목의 하락 폭이 컸고, 강한 고용지표 이후 금리 인상 우려가 커지면서 성장주 중심 매도가 확대됐다.

현재 프로젝트가 이 상황에서도 한국 종목 상승을 예측했다면 다음 문제가 있을 수 있다.

```text
1. 전일 미국 장 마감 데이터를 한국 예측 전에 반영하지 않음
2. Nasdaq, S&P500, Dow, SOX feature가 없음
3. VIX, 미국채 금리, USD/KRW feature가 없음
4. 급락장에서 추천 점수를 낮추는 risk gate가 없음
5. 시차 정렬 오류로 최신 데이터가 빠지거나 미래 정보가 섞임
6. 전체 기간 평균 정확도만 보고 shock 구간 정확도를 따로 검증하지 않음
```

---

# 1. 핵심 구조

```text
[기존 국내 종목 모델]
가격 / 거래량 / 외국인 / 기관 / 개인 / 뉴스 / 공시 / 재무 / 애널리스트

                +

[Global Market Regime Layer]
미국 지수 / SOX / 일본 / 대만 / VIX / 미국채 / 환율 / 유가 / 미국 선물

                ↓

[Risk Gate]
risk_on / neutral / risk_off / panic

                ↓

[Recommendation Adjustment]
추천 점수 조정
종목별 weight 조정
현금 비중 조정
대시보드 경고 표시
```

원칙:

```text
- 글로벌 지표는 개별 종목 예측을 무조건 뒤집는 용도가 아니다.
- 급락 위험이 커지면 추천 점수, weight, 신뢰도를 낮춘다.
- 신규 feature는 backtest A/B 검증 후 좋아질 때만 production 반영한다.
- 정확도가 하락하면 feature 또는 모델을 제거하고 production_weight = 0으로 되돌린다.
```

---

# 2. 수집할 글로벌 데이터

## 2.1 반드시 추가

| 그룹 | 지표 | PoC symbol 예시 | 활용 |
|---|---|---|---|
| 미국 대표지수 | S&P 500 | `^GSPC` | 글로벌 risk-on/off |
| 미국 기술주 | Nasdaq Composite | `^IXIC` | 성장주·반도체 민감도 |
| 미국 가치주 | Dow Jones | `^DJI` | 기술주 편중 여부 비교 |
| 미국 반도체 | Philadelphia Semiconductor Index | `^SOX` | 삼성전자·SK하이닉스·반도체 장비주 |
| 미국 변동성 | VIX | `^VIX`, FRED `VIXCLS` | panic 감지 |
| 미국 장기금리 | 미국채 10년 | FRED `DGS10` | 성장주 할인율, 금리 충격 |
| 환율 | USD/KRW | provider별 코드 | 외국인 수급·수출주 영향 |
| 한국 벤치마크 | KOSPI | provider별 코드 | 국내 시장 regime |
| 한국 성장주 | KOSDAQ | provider별 코드 | 중소형 성장주 regime |

## 2.2 아시아 연동성

| 그룹 | 지표 | PoC symbol 예시 | 활용 |
|---|---|---|---|
| 일본 | Nikkei 225 | `^N225` | 아시아 위험선호, 엔화 영향 |
| 대만 | Taiwan Weighted | `^TWII` | 반도체 공급망, TSMC 영향 |
| 홍콩 | Hang Seng | `^HSI` | 중국·아시아 위험선호 |
| 중국 | Shanghai Composite | `000001.SS` | 중국 경기·산업재·소비재 |
| 대만 반도체 | TSMC ADR | `TSM` | 반도체 업황 보조 |

## 2.3 고도화

| 그룹 | 지표 | PoC symbol 예시 | 활용 |
|---|---|---|---|
| 미국 선물 | S&P 500 E-mini | `ES=F` | 한국 장 시작 전 선행 신호 |
| 미국 선물 | Nasdaq 100 futures | `NQ=F` | 한국 기술주 선행 신호 |
| 원자재 | WTI | FRED `DCOILWTICO` | 물가·정유·화학 |
| 원자재 | Brent | FRED `DCOILBRENTEU` | 에너지·인플레이션 |
| 금리 | 미국채 2년 | FRED `DGS2` | 정책금리 민감도 |
| 금리 스프레드 | 10Y-3M | FRED `T10Y3M` | 경기 둔화 보조 |
| 반도체 종목 | NVDA, AVGO, AMD, MU | 티커 그대로 | 반도체 cluster 보조 |

---

# 3. 데이터 소스 전략

## 3.1 로컬 PoC

```text
PoC:
- yfinance 또는 FinanceDataReader
- FRED API
- 기존 국내 provider

운영:
- 한국투자증권 KIS Developers API
- 승인받은 KRX Open API
- FRED API
- 계약한 글로벌 시세 공급자
```

주의:

```text
- yfinance는 PoC 전용이다.
- 웹서비스 운영에서 저장·가공·재배포 범위를 확인한다.
- provider adapter로 분리하고 비공식 크롤러를 핵심 코드에 직접 결합하지 않는다.
```

## 3.2 KIS 활용 대상

```text
- 해외지수 분봉조회
- 해외주식 종목/지수/환율 기간별시세
- 국내주식 업종기간별시세
- 국내기관·외국인 매매종목 가집계
```

## 3.3 FRED daily series

```text
VIXCLS       CBOE VIX
DGS10        미국채 10년
DGS2         미국채 2년
T10Y3M       미국채 10년 - 3개월 spread
DCOILWTICO   WTI
DCOILBRENTEU Brent
```

---

# 4. 가장 중요한 시차 정렬

## 4.1 한국 장 시작 전 예측

예측 생성 시각:

```text
07:30 ~ 08:30 KST
```

사용 가능:

```text
- 전일 미국 장 마감
- 미국 선물 현재 snapshot
- 전일 일본·대만·홍콩 장 마감
- 전일 KOSPI/KOSDAQ 장 마감
- 최신 공개 환율·금리·VIX
```

금지:

```text
- 당일 일본·대만 장 마감
- 당일 한국 장 마감
- 아직 공개되지 않은 미국 장 마감
```

## 4.2 한국 장중 모델

```text
09:30 / 11:30 / 14:30 KST
```

사용 가능:

```text
- 당일 KOSPI/KOSDAQ 장중 수익률
- Nikkei/Taiwan 장중 수익률
- 미국 선물 snapshot
- USD/KRW snapshot
```

장전 모델과 장중 모델은 분리한다.

## 4.3 Leakage 방지 예시

```text
예측 대상:
2026-06-08 한국 장 시작 전 추천

사용:
2026-06-05 미국 장 마감 S&P500/Nasdaq/Dow/SOX/VIX
2026-06-05 일본·대만 장 마감
2026-06-05 한국 장 마감
2026-06-08 08:00 KST 미국 선물 snapshot

금지:
2026-06-08 일본·대만 장 마감
2026-06-08 한국 장 마감
```

---

# 5. DB 스키마

```sql
CREATE TABLE IF NOT EXISTS global_market_daily (
    trade_date DATE NOT NULL,
    symbol VARCHAR(40) NOT NULL,
    market_group VARCHAR(30) NOT NULL,
    display_name TEXT NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume NUMERIC,
    return_1d NUMERIC,
    return_5d NUMERIC,
    return_20d NUMERIC,
    volatility_20d NUMERIC,
    source_name VARCHAR(50) NOT NULL,
    source_timestamp TIMESTAMP,
    ingested_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (trade_date, symbol, source_name)
);

CREATE TABLE IF NOT EXISTS global_market_intraday_snapshot (
    snapshot_at TIMESTAMP NOT NULL,
    symbol VARCHAR(40) NOT NULL,
    market_group VARCHAR(30) NOT NULL,
    price NUMERIC,
    change_rate NUMERIC,
    source_name VARCHAR(50) NOT NULL,
    source_timestamp TIMESTAMP,
    freshness_seconds INT,
    ingested_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (snapshot_at, symbol, source_name)
);

CREATE TABLE IF NOT EXISTS market_regime_daily (
    prediction_date DATE NOT NULL,
    prediction_cutoff TIMESTAMP NOT NULL,
    us_equity_score NUMERIC,
    semiconductor_score NUMERIC,
    asia_score NUMERIC,
    volatility_score NUMERIC,
    rate_score NUMERIC,
    fx_score NUMERIC,
    commodity_score NUMERIC,
    global_risk_score NUMERIC NOT NULL,
    regime VARCHAR(20) NOT NULL,
    recommended_cash_ratio NUMERIC NOT NULL,
    feature_version VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (prediction_date, prediction_cutoff, feature_version)
);

CREATE TABLE IF NOT EXISTS stock_global_exposure (
    ticker VARCHAR(20) NOT NULL,
    feature_version VARCHAR(50) NOT NULL,
    beta_sp500 NUMERIC,
    beta_nasdaq NUMERIC,
    beta_sox NUMERIC,
    beta_nikkei NUMERIC,
    beta_taiwan NUMERIC,
    beta_usdkrw NUMERIC,
    beta_wti NUMERIC,
    sector TEXT,
    estimated_from DATE,
    estimated_to DATE,
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (ticker, feature_version)
);
```

---

# 6. Provider adapter

```text
backend/app/
  providers/global_market/
    base.py
    fred_provider.py
    yfinance_poc_provider.py
    kis_overseas_provider.py
  services/
    global_market_service.py
    market_regime_service.py
  jobs/
    collect_global_market_daily.py
    collect_premarket_snapshot.py
    build_market_regime.py
```

`backend/app/providers/global_market/base.py`

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

@dataclass(frozen=True)
class DailyMarketBar:
    trade_date: date
    symbol: str
    market_group: str
    display_name: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: Decimal | None
    source_name: str
    source_timestamp: datetime | None

@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_at: datetime
    symbol: str
    market_group: str
    price: Decimal
    change_rate: Decimal | None
    source_name: str
    source_timestamp: datetime | None

class GlobalMarketProvider(ABC):
    @abstractmethod
    def get_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: date,
        end_date: date,
    ) -> list[DailyMarketBar]:
        raise NotImplementedError

    @abstractmethod
    def get_snapshots(
        self,
        symbols: Iterable[str],
        snapshot_at: datetime,
    ) -> list[MarketSnapshot]:
        raise NotImplementedError
```

---

# 7. PoC symbol registry

`backend/app/config/global_market_symbols.py`

```python
GLOBAL_DAILY_SYMBOLS = {
    "sp500": ("^GSPC", "S&P 500", "US_INDEX"),
    "nasdaq": ("^IXIC", "Nasdaq Composite", "US_INDEX"),
    "dow": ("^DJI", "Dow Jones", "US_INDEX"),
    "sox": ("^SOX", "Philadelphia Semiconductor Index", "SEMICONDUCTOR"),
    "nikkei225": ("^N225", "Nikkei 225", "ASIA_INDEX"),
    "taiwan_weighted": ("^TWII", "Taiwan Weighted", "ASIA_INDEX"),
    "hang_seng": ("^HSI", "Hang Seng", "ASIA_INDEX"),
    "shanghai": ("000001.SS", "Shanghai Composite", "ASIA_INDEX"),
    "tsmc_adr": ("TSM", "TSMC ADR", "SEMICONDUCTOR"),
    "sp500_futures": ("ES=F", "S&P 500 E-mini Futures", "US_FUTURES"),
    "nasdaq_futures": ("NQ=F", "Nasdaq 100 Futures", "US_FUTURES"),
}
```

주의:

```text
- symbol은 PoC provider 예시다.
- KIS provider용 mapping은 별도 파일로 만든다.
- 공급자 코드를 서비스 코드에 분산 하드코딩하지 않는다.
```

---

# 8. Feature Engineering

## 8.1 기본 feature

```text
sp500_return_1d
sp500_return_5d
nasdaq_return_1d
nasdaq_return_5d
dow_return_1d
sox_return_1d
sox_return_5d
nikkei_return_1d
taiwan_return_1d
hangseng_return_1d
vix_level
vix_change_1d
us10y_level
us10y_change_bp_1d
us2y_change_bp_1d
yield_curve_10y3m
usdkrw_return_1d
wti_return_1d
brent_return_1d
sp500_futures_return_snapshot
nasdaq_futures_return_snapshot
```

## 8.2 shock feature

```text
us_equity_shock
us_tech_shock
semiconductor_shock
asia_risk_off
vix_spike
rate_spike
fx_spike
oil_spike
global_panic
```

## 8.3 Regime baseline

`backend/app/services/market_regime_service.py`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class GlobalSignals:
    sp500_return_1d: float
    nasdaq_return_1d: float
    dow_return_1d: float
    sox_return_1d: float
    vix_level: float | None
    vix_change_1d: float | None
    us10y_change_bp_1d: float | None
    usdkrw_return_1d: float | None
    nasdaq_futures_return_snapshot: float | None

def build_global_regime(signals: GlobalSignals) -> dict:
    risk = 0.0
    reasons: list[str] = []

    if signals.sp500_return_1d <= -0.02:
        risk += 20
        reasons.append("S&P500 daily shock")
    if signals.nasdaq_return_1d <= -0.03:
        risk += 25
        reasons.append("Nasdaq daily shock")
    if signals.sox_return_1d <= -0.04:
        risk += 25
        reasons.append("Semiconductor index shock")
    if signals.vix_level is not None and signals.vix_level >= 25:
        risk += 15
        reasons.append("VIX elevated")
    if signals.vix_change_1d is not None and signals.vix_change_1d >= 0.20:
        risk += 10
        reasons.append("VIX spike")
    if signals.us10y_change_bp_1d is not None and signals.us10y_change_bp_1d >= 10:
        risk += 10
        reasons.append("US 10Y yield spike")
    if signals.usdkrw_return_1d is not None and signals.usdkrw_return_1d >= 0.01:
        risk += 10
        reasons.append("USD/KRW risk-off move")
    if (
        signals.nasdaq_futures_return_snapshot is not None
        and signals.nasdaq_futures_return_snapshot <= -0.01
    ):
        risk += 10
        reasons.append("Nasdaq futures weak before KRX open")

    risk = max(0.0, min(risk, 100.0))

    if risk >= 70:
        return {"risk": risk, "regime": "panic", "cash_ratio": 0.50, "reasons": reasons}
    if risk >= 45:
        return {"risk": risk, "regime": "risk_off", "cash_ratio": 0.30, "reasons": reasons}
    if risk >= 20:
        return {"risk": risk, "regime": "neutral", "cash_ratio": 0.15, "reasons": reasons}
    return {"risk": risk, "regime": "risk_on", "cash_ratio": 0.05, "reasons": reasons}
```

threshold는 초기 baseline이다. walk-forward backtest로 조정한다.

---

# 9. 종목별 민감도와 weight

모든 종목에 동일 penalty를 적용하지 않는다.

```text
반도체:
SOX + Nasdaq + TSMC ADR + USD/KRW

자동차:
S&P500 + Dow + USD/KRW + Nikkei

바이오:
Nasdaq + 금리 + VIX

정유·화학:
WTI + Brent + USD/KRW

금융:
Dow + 미국채 금리 + yield curve
```

```python
def apply_regime_weight_cap(regime: str, stock_weight: float, sector_weight: float):
    if regime == "panic":
        return min(stock_weight, 0.05), min(sector_weight, 0.15)
    if regime == "risk_off":
        return min(stock_weight, 0.08), min(sector_weight, 0.20)
    if regime == "neutral":
        return min(stock_weight, 0.12), min(sector_weight, 0.30)
    return min(stock_weight, 0.15), min(sector_weight, 0.40)
```

---

# 10. ML 반영

## 10.1 LightGBM baseline

기존 LightGBM feature에 추가:

```text
global daily feature
shock feature
market regime category
종목별 global beta
```

## 10.2 PatchTST/TFT 2차 모델

RTX 3090 로컬 GPU에서 sequence 모델을 실험한다.

```text
lookback: 252 거래일

channels:
종목 OHLCV
외국인/기관/개인 수급
뉴스 감성
애널리스트 목표가 변화
KOSPI/KOSDAQ
S&P500/Nasdaq/Dow/SOX
Nikkei/Taiwan
VIX
US10Y
USD/KRW
WTI
```

출력:

```text
20D 상승확률
60D 상승확률
120D 초과수익 확률
240D 리스크 확률
```

## 10.3 제거 원칙

```text
- 신규 feature 또는 모델은 바로 production 반영 금지
- 기존 모델보다 backtest가 나빠지면 gate_status = rejected
- production_weight = 0
- UI에 rejected 사유 표시
```

---

# 11. Backtest 개선

## 11.1 A/B 모델

```text
A: domestic_only_v1
B: domestic_plus_global_regime_v1
C: domestic_plus_global_regime_patchtst_v1
```

## 11.2 Shock 구간 별도 검증

```text
normal days
risk_off days
panic days
Nasdaq <= -3%
S&P500 <= -2%
SOX <= -4%
VIX >= 25
US10Y daily change >= +10bp
USD/KRW >= +1%
```

## 11.3 추가 지표

| 지표 | 의미 |
|---|---|
| `shock_day_hit_ratio` | 글로벌 급락 다음 한국 장 방향 정확도 |
| `panic_precision_top20` | panic 구간 Top20 성과 |
| `risk_off_mdd` | 위험회피 구간 MDD |
| `false_positive_up_rate` | 급락 위험에도 상승 오판한 비율 |
| `cash_gate_avoided_loss` | 현금 gate로 줄인 손실 |
| `sector_shock_accuracy` | 민감 섹터별 정확도 |

Gatekeeper 추가 조건:

```text
- shock_day_hit_ratio가 기존 모델보다 낮으면 rejected
- false_positive_up_rate가 증가하면 rejected
- risk_off_mdd가 악화되면 rejected
- 전체 precision_top20과 avg_excess_return도 기존 기준 이상이어야 함
```

---

# 12. 배치 스케줄

## 12.1 장 마감 후

```text
18:00 KST
1. 국내 가격·수급 수집
2. 일본·대만·홍콩 일봉 수집
3. Universe 갱신
4. 국내 feature 생성
```

## 12.2 미국 장 마감 후, 한국 장 시작 전

```text
07:30 KST
1. S&P500/Nasdaq/Dow/SOX 일봉
2. VIX
3. 미국채 2Y/10Y
4. USD/KRW
5. WTI/Brent
6. 미국 선물 snapshot
7. global feature
8. market regime
9. 추천 점수 보정
10. dashboard snapshot
```

## 12.3 장중 optional

```text
09:30 / 11:30 / 14:30 KST
KOSPI/KOSDAQ
Nikkei/Taiwan
미국 선물
USD/KRW
intraday risk warning
```

---

# 13. 실행 명령

```bash
python -m app.jobs.collect_global_market_daily \
  --from-date 2022-01-01 \
  --to-date 2026-06-05

python -m app.jobs.collect_premarket_snapshot

python -m app.jobs.build_market_regime \
  --prediction-date 2026-06-08 \
  --cutoff "2026-06-08T08:00:00+09:00"

python -m app.jobs.run_backtest \
  --model domestic_only_v1 \
  --all-horizons

python -m app.jobs.run_backtest \
  --model domestic_plus_global_regime_v1 \
  --all-horizons

python -m app.jobs.compare_backtest_models \
  --baseline domestic_only_v1 \
  --candidate domestic_plus_global_regime_v1
```

---

# 14. FastAPI

```text
GET /api/market-regime/current
GET /api/market-regime/history?from=2026-05-01&to=2026-06-08
GET /api/global-markets/latest
GET /api/global-markets/history?symbol=^IXIC&days=60
GET /api/backtest/shock-summary
GET /api/backtest/compare?baseline=domestic_only_v1&candidate=domestic_plus_global_regime_v1
```

응답 예:

```json
{
  "prediction_date": "2026-06-08",
  "global_risk_score": 78,
  "regime": "panic",
  "recommended_cash_ratio": 0.50,
  "reasons": [
    "S&P500 daily shock",
    "Nasdaq daily shock",
    "Semiconductor index shock",
    "VIX spike"
  ]
}
```

---

# 15. Dashboard 추가 UI

```text
[글로벌 시장 위험도]
risk_on / neutral / risk_off / panic
Global Risk Score
권장 현금비중
주요 원인 tag

[전일 해외시장]
S&P500
Nasdaq
Dow
SOX
Nikkei
Taiwan Weighted
VIX
US10Y
USD/KRW

[예측 보정]
기존 추천 점수
글로벌 위험 보정값
최종 추천 점수
weight 감소 이유

[Shock Backtest]
급락 다음 날 정확도
상승 오판 비율
현금 gate 전·후 MDD
국내 전용 vs 글로벌 보정 모델
```

UI badge:

```text
risk_on     green
neutral     yellow
risk_off    orange
panic       red
```

사용자 문구:

```text
글로벌 시장 위험도는 전일 미국 장 마감, 아시아 시장, 변동성, 금리, 환율을 종합한 참고 지표입니다.
과거 성과와 백테스트 결과는 미래 수익을 보장하지 않습니다.
```

---

# 16. 테스트

```python
def test_premarket_features_do_not_include_future_asia_close():
    cutoff = "2026-06-08T08:00:00+09:00"
    features = build_global_features(prediction_cutoff=cutoff)
    assert features.max_source_timestamp <= cutoff

def test_nasdaq_crash_triggers_risk_off_or_panic():
    signals = GlobalSignals(
        sp500_return_1d=-0.0264,
        nasdaq_return_1d=-0.0418,
        dow_return_1d=-0.0135,
        sox_return_1d=-0.08,
        vix_level=28.0,
        vix_change_1d=0.30,
        us10y_change_bp_1d=12.0,
        usdkrw_return_1d=0.012,
        nasdaq_futures_return_snapshot=-0.012,
    )
    result = build_global_regime(signals)
    assert result["regime"] in {"risk_off", "panic"}
    assert result["cash_ratio"] >= 0.30
```

추가 unit test:

```text
- SOX <= -4%이면 반도체 weight 감소
- VIX >= 25이면 위험 점수 증가
- US10Y +10bp이면 rate shock 반영
- 공급자 실패 시 마지막 정상 snapshot 유지
- 미래 timestamp가 feature에 포함되지 않음
```

---

# 17. Acceptance Criteria

```text
[Data]
- S&P500/Nasdaq/Dow/SOX/Nikkei/Taiwan/VIX/US10Y/USDKRW 수집
- source_timestamp 저장
- 장전 snapshot 저장
- freshness 검사

[Feature]
- global daily feature
- shock feature
- market regime
- 종목별 beta

[Prediction]
- 기존 추천 점수와 보정 점수 분리 저장
- panic이면 현금 비중 증가
- 반도체 shock이면 관련 weight 감소

[Backtest]
- A/B 비교
- shock 구간 별도 검증
- false_positive_up_rate
- risk_off_mdd
- 하락 시 rejected + production_weight = 0

[Web]
- 글로벌 위험 카드
- 전일 해외시장 패널
- 예측 보정 패널
- Shock Backtest 패널
```

---

# 18. Codex 단계

```text
P1. DB migration
P2. GlobalMarketProvider interface
P3. FRED provider
P4. yfinance PoC provider
P5. daily collector
P6. premarket snapshot
P7. market regime baseline
P8. 추천 점수 보정
P9. A/B backtest + shock metric
P10. FastAPI
P11. Dashboard
P12. KIS provider 교체
```

---

# 19. Codex 첫 요청문

```text
HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md와
ai_robo_stock_development_proposal_v9_global_market_regime_codex.md를 먼저 읽어줘.

이번에는 P1~P3만 구현해줘.

범위:
1. global_market_daily migration
2. global_market_intraday_snapshot migration
3. market_regime_daily migration
4. stock_global_exposure migration
5. GlobalMarketProvider interface
6. FredProvider skeleton
7. VIXCLS, DGS10, DGS2, T10Y3M, DCOILWTICO, DCOILBRENTEU registry
8. 최소 unit test
9. .codex/STATE.md 갱신

주의:
- 관련 없는 파일은 수정하지 마.
- 운영 endpoint를 추측해 하드코딩하지 마.
- source_timestamp를 반드시 보존해.
- 시차 leakage test TODO를 남겨.
- 수정 파일, 실행 명령, 테스트 결과만 간단히 보고해.
```

---

# 20. 후속 요청문

## P4~P5

```text
HARNESS와 .codex/STATE.md를 읽어줘.
P4~P5만 수행해줘.

- yfinance_poc_provider.py
- collect_global_market_daily.py
- symbol registry
- upsert
- 결측치와 freshness 검사
- unit test

PoC provider임을 명확히 표시하고 운영용으로 사용하지 마.
```

## P6~P8

```text
HARNESS와 .codex/STATE.md를 읽어줘.
P6~P8만 수행해줘.

- collect_premarket_snapshot.py
- market_regime_service.py
- build_market_regime.py
- 추천 점수 보정
- panic/risk_off 현금 gate
- 반도체 shock weight cap
- 시차 leakage test
- shock scenario test
```

## P9~P11

```text
HARNESS와 .codex/STATE.md를 읽어줘.
P9~P11만 수행해줘.

- domestic_only_v1 vs domestic_plus_global_regime_v1 A/B backtest
- shock_day_hit_ratio
- false_positive_up_rate
- risk_off_mdd
- cash_gate_avoided_loss
- FastAPI endpoint
- Global Risk Card
- Shock Backtest Panel
- 기존 dashboard 연결

정확도가 낮아지면 신규 모델은 gate_status=rejected,
production_weight=0이 되도록 구현해줘.
```

---

# 21. 운영 전 체크리스트

```text
- 데이터 라이선스 확인
- 미국 장 마감 후 한국 장전 batch 실행
- source_timestamp leakage test
- 휴장일 처리
- DST 처리
- FRED 지연 처리
- 미국 선물 freshness 검사
- provider 실패 시 마지막 정상 snapshot
- panic badge 및 안내 문구
- A/B backtest 통과 전 production 반영 금지
```

---

# 22. 최종 요약

```text
국내 종목만 보고 상승을 예측하지 않는다.

전일 미국:
S&P500 + Nasdaq + Dow + SOX + VIX + 미국채

아시아:
Nikkei + Taiwan Weighted + Hang Seng

시장 변수:
USD/KRW + WTI + Brent + 미국 선물

를 한국 장전 예측에 반영한다.

급락 구간:
추천 신뢰도 감소
현금 비중 증가
반도체·성장주 weight 감소
panic 경고

신규 feature가 backtest 정확도를 낮추면:
즉시 제거
gate_status = rejected
production_weight = 0
```

---

# 23. 참고자료

- Reuters, 2026-06-05: 미국 고용지표와 기술주·반도체 매도에 따른 미국 증시 급락 보도
- AP, 2026-06-05: S&P500, Dow, Nasdaq 일간 변동 폭 보도
- 한국투자증권 KIS Developers API 문서
- FRED `VIXCLS`
- FRED `DGS10`
- FRED `DGS2`
- FRED `T10Y3M`
- FRED `DCOILWTICO`
