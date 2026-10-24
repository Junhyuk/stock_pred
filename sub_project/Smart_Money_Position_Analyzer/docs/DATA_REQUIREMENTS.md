# 필수 데이터 및 표준 컬럼 설계

## 공통 기준
모든 데이터 레코드는 `market`, `symbol`, `event_date`(거래/보고 기준일), `available_at`(대외 공개시점), `ingested_at`, `source_name`, `source_url`, `revision_id`를 포함한다. 가능하면 `unit`, `currency`도 추가한다. `NULL`을 0으로 치환하지 않는다.

| 우선순위 | 지역 | 데이터 | 대표 컬럼 | 주기 | 활용 |
|---|---|---|---|---|---|
| P0 | KR | 종목 가격/거래량 | date, symbol, close, adjusted_close, volume, traded_value, shares_out | 일 | 가격/거래량, 분할 보정 |
| P0 | KR | 투자자별 매매 | date, symbol, investor_type, buy_qty, sell_qty, net_qty, buy_value, sell_value, net_value | 일 | 외국인/기관 누적 매수 |
| P0 | KR | 외국인 보유량 | date, symbol, foreign_held_shares, foreign_ownership_pct | 일/공개 단위 | 순매수 추정 교차검증 |
| P1 | KR | 공매도 | date, symbol, short_sale_value, short_sale_volume, short_net_balance_shares | 일/발표 단위 | 숏 잔고의 추세 |
| P1 | KR | 파생상품 | date, instrument, expiry, investor_type, net_traded_contracts, open_interest, futures_basis | 일 | 현물·선물의 교차 신호 |
| P1 | KR | DART 대량보유 공시 | receipt_date, corp_code, reporter, reported_shares, share_delta, report_reason | 수시 | 공개된 대주주 변화 |
| P0 | US | 일별 가격/기업행동 | date, symbol, adjusted_close, volume, split_factor, outstanding | 일 | 13F 수량 비교 보정 |
| P0 | US | SEC 13F | report_period_end, filing_date, manager_cik, accession, issuer_cusip, put_call, reported_shares, reported_value_usd | 분기 | 보고대상 기관 롱 보유 변화 |
| P1 | US | SEC 13D/13G | report_date, filing_date, holder, beneficial_ownership_pct, ownership_change | 수시/공시 규칙 | 5% 이상 대량보유 신고 |
| P1 | US | 공매도 잔고(SI) | settlement_date, publish_date, symbol, short_interest_shares, days_to_cover | 대개 월 2회 | 숏 포지션 잔고 |
| P2 | US | 옵션(OI/IV/skew) | date, symbol, expiry, strike, option_type, open_interest, implied_vol, volume | 일/공급사 | 헤지/투기 수요 간접 관찰 |
| P2 | US | CFTC COT(TFF) | position_asof, published_at, contract, asset_manager_long, asset_manager_short, leveraged_funds_long, leveraged_funds_short, total_oi | 주 | 지수·선물 시장의 큰손 성향 |
| P2 | 공통 | 거시정보 | date, usdk_rw, fed_rate, volatility_index, benchmark_return | 일/주 | 글로벌 자금위험 선호 맥락 |

## 중요한 데이터 구별
- 외국인 `net_qty`를 누적한 값은 해당 기간의 **순거래량**일 뿐, 이전 보유량이나 최종 실보유량을 직접 알려주지 않는다.
- KRX의 외국인 보유수량 발표 기준 시점이 거래일과 어긋날 수 있으므로 해당 출처의 주석 및 확정치를 함께 보관한다.
- KRX 홈페이지에서 다운로드 가능한 '투자자별 거래실적'과 **KRX OPEN API에서 제공되는 엔드포인트 목록은 동일하지 않다**. 공개 API는 일별 매매정보/시세 등이 중심이다. 기관별 수급 자동 수집 가능 여부와 계약·라이선스는 개별 확인한다.
- 미국 기관 13F 자료는 `as_reported`와 `adjusted_shares`를 둘 다 남겨 수정신고/주식분할을 추적한다.
- 미국 내 투자자를 '외국인'으로 별도 구분한 종목별 일일 순매수 공개 통계는 KRX와 동일한 형태로 제공되지 않는다. 13F는 외국인 전용 자료가 아니다.
- 공매도 매매량(short-sale volume)은 미청산 잔고(short interest)와 다르다.

## 우선 확보 순서
1. KR 관심 종목 10개 × 과거 2년: 가격, 외국인 순매수 수량·금액, 외국인 보유량.
2. KR 동일 종목: 기관 수급, 공매도 잔고 및 기업행동.
3. US 관심 종목 10개 × 과거 8개 분기: 13F 기관별 보유주수, 보유종목 CUSIP→티커 매핑.
4. US 공매도 잔고와 일부 운용사의 13D/G.
5. 지수선물 COT 및 옵션 OI/IV는 2차 확장.
