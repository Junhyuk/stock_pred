# DATA_SOURCES.md

## A. Official / Preferred Sources

### KRX Data Marketplace / KRX Open API
우선순위: HIGH

활용:
- KOSPI/KOSDAQ 계열 지수 일별시세
- 주식 일별매매
- ETF/ETN
- 선물
- 옵션
- 기본정보

KRX Open API는 지수 및 시장의 일별 데이터를 제공하며,
공식 서비스 목록상 2010년 이후 데이터를 제공하는 항목들이 있다.

주의:
- 실시간/재배포/상업적 사용은 별도 라이선스 조건을 확인한다.
- Open API 제공 항목과 유료 Data Feed 제공 항목을 구분한다.

### KRX Data Marketplace Web Statistics
추가 조사:
- 투자자별 거래실적
- 개별종목 투자자별 거래
- 공매도
- 공매도 순보유잔고
- 지수 구성종목
- 선물 최근월물
- PER/PBR/배당수익률

### OpenDART
우선순위: HIGH

활용:
- 공시 검색
- 기업개황
- 공시 원문
- 재무제표
- 주요 재무지표
- 대량보유 상황
- 주요사항보고서

공시 timestamp 기반 event feature를 만든다.

### Korea Investment & Securities Open API
우선순위: HIGH for live/broker layer

활용 후보:
- 국내 주식 REST
- WebSocket real-time
- 선물/옵션
- 시세/매매 자동화

실제 endpoint 가능 여부와 계정 권한을 API 문서 기준으로 확인한다.

### Bank of Korea ECOS
우선순위: HIGH

활용:
- 정책금리
- 시장금리
- 환율
- 통화/유동성
- 경기지표

중요:
macro series는 발표일과 기준일이 다르므로 `published_at`를 보존한다.

---

## B. News

### Naver Search News API
한국 뉴스 검색/수집 후보.

저장할 항목:
- title
- description
- original link
- published_at
- query
- entity
- source domain

중복 기사 clustering이 필요하다.

### BIGKinds
한국 언론 뉴스 분석/검증에 활용 가능.
이용정책과 자동수집 가능범위를 확인한다.

---

## C. Global / Cross Asset

가능하면 공식 또는 신뢰할 수 있는 market-data provider를 사용한다.

- US indices
- Nasdaq
- Philadelphia Semiconductor Index proxy/data
- US Treasury yields
- DXY
- VIX
- commodities
- Japan/China/HK indices

무료 데이터는 과거 수정/결측/라이선스를 점검한다.

---

## D. Data Acquisition Priority

### Tier 0 — 반드시 확보
1. Index OHLCV
2. foreign/institution/individual flow
3. USDKRW
4. US overnight equity
5. rates
6. calendar
7. market breadth

### Tier 1 — 매우 추천
1. foreign futures
2. program trading
3. short selling
4. open interest
5. basis
6. ETF
7. DART
8. news

### Tier 2 — 성능 개선 실험
1. options IV/skew
2. credit / securities lending
3. margin balance
4. sector breadth
5. earnings revision
6. index rebalancing events

### Tier 3 — 대체데이터
1. semiconductor memory pricing
2. shipping/freight
3. export indicators
4. search trend
5. policy/event calendar
6. social sentiment

Tier 3는 비용과 데이터 라이선스를 먼저 확인한다.

현재 로컬 DB에 없는 항목과 수집 가드는
`MISSING_DATA_FOR_INDEX_FORECAST.md`에 있다.


## Index constituent snapshots

`docs/data/index_constituents.json` contains code and name pairs for KOSPI 200
and KOSDAQ 150. The daily forecast workflow refreshes them with
`python scripts/collect_index_constituents.py`. The source is Korea Investment &
Securities' public current [KOSPI and KOSDAQ security masters and documented
field layouts](https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info).
The KOSPI200 sector field identifies membership; the KOSDAQ150 field uses Y/N.
These are index membership fields, not ETF holdings. No prices are published.

Each `as_of` is the calendar date stored in that market's upstream ZIP member
metadata: the date of the master snapshot, not a certified exchange session or
a historical query date. The two dates can differ. Invalid, stale, incomplete,
future-dated or failed downloads leave that market's last validated snapshot
intact; no JSON is created until both markets have valid snapshots. Counts are
included as integrity checks and may differ from the index's nominal name.
