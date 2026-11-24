# FEATURE_CATALOG.md

## 1. Price / Momentum

- return_1d
- return_2d
- return_5d
- return_20d
- overnight_gap
- high_low_range
- close_location_value
- ATR
- realized_vol_5/20/60
- downside_vol
- RSI
- MACD
- moving-average distance
- trend slope
- volume z-score
- turnover z-score

기술지표를 수십 개 무작정 추가하지 않는다.
상관성이 높은 지표는 축소한다.

---

## 2. Foreign Investor Flow — 중요도 VERY HIGH

- foreign_net_buy
- foreign_buy_amount
- foreign_sell_amount
- foreign_net_buy / turnover
- foreign_net_buy / market_cap
- foreign_5d_sum
- foreign_20d_sum
- foreign_flow_zscore
- consecutive_foreign_buy_days
- foreign_flow_acceleration
- foreign_flow_reversal
- foreign_flow_price_divergence
- foreign_cash_vs_futures_divergence

### 매우 중요한 feature
외국인이 현물은 사고 선물은 파는 경우와
현물/선물을 동시에 사는 경우를 구분한다.

---

## 3. Institutional / Retail Flow

- institution_net_buy
- pension_net_buy
- financial_investment_net_buy
- retail_net_buy
- institution_foreign_agreement
- retail_contrarian_signal

세부 투자주체가 확보될 경우 aggregation 전 raw series도 보존한다.

---

## 4. Program Trading

- program_total_net
- arbitrage_program_net
- non_arbitrage_program_net
- program_net / turnover
- program_flow_acceleration

---

## 5. Futures

- futures_return
- futures_volume
- futures_open_interest
- OI_change
- foreign_futures_net
- institution_futures_net
- futures_basis
- basis_zscore
- basis_change
- roll_yield
- expiry_days

해석 예:
가격 상승 + OI 상승은 가격 상승 + OI 하락과 의미가 다를 수 있다.
규칙으로 단정하지 말고 ML feature로 실증 검증한다.

---

## 6. Options

가능한 경우:
- put_call_volume_ratio
- put_call_open_interest_ratio
- ATM_IV
- IV_change
- IV_term_structure
- skew
- tail_put_demand
- expiry_days

VKOSPI 또는 한국 시장 변동성 지표 사용 가능 여부를 확인한다.

---

## 7. Short / Credit / Lending

- short_sell_value
- short_ratio
- short_balance
- securities_lending_balance
- lending_change
- margin_credit_balance
- margin_credit_change

단순히 “공매도 증가=하락”으로 해석하지 않는다.
시장 전체/대형주/코스닥 반응을 별도 학습한다.

---

## 8. Market Breadth — 놓치기 쉬우나 중요

- advance_count
- decline_count
- advance_decline_ratio
- advance_decline_line
- up_volume / down_volume
- new_high_count
- new_low_count
- % above MA20
- % above MA60
- % above MA120
- equal_weight_return
- cap_weight_return
- equal_vs_cap_spread
- cross_sectional_dispersion
- median_stock_return

지수가 삼성전자/대형주 몇 종목 때문에 움직였는지,
시장 전체가 같이 움직였는지 구분한다.

---

## 9. Index Concentration

KOSPI/KOSPI100에서 특히 중요.

- top1 weight contribution
- top5 contribution
- top10 contribution
- semiconductor sector contribution
- breadth excluding top5
- index return excluding top N
- sector concentration

현재 구성종목을 과거에 그대로 적용하지 않는다.
가능하면 point-in-time constituent를 사용한다.

---

## 10. ETF

- index ETF volume
- ETF turnover
- ETF premium_discount
- leveraged ETF activity
- inverse ETF activity
- foreign ETF flow
- ETF AUM change
- creation/redemption data if legally/technically obtainable

leveraged/inverse ETF activity는 retail positioning proxy 후보로 실험한다.

---

## 11. FX

- USDKRW return
- USDKRW gap
- USDKRW vol
- DXY
- JPYKRW
- CNYKRW or USDCNY
- FX risk regime

외국인 수급과 USDKRW interaction feature를 만든다.

예:
`foreign_flow_z * (-usdkrw_return_z)`

---

## 12. Interest Rates / Liquidity

- Korea base rate
- Korea 3Y / 10Y
- US 2Y / 10Y
- KR-US spread
- term spread
- rate change
- rate volatility
- liquidity proxy
- credit spread if available

---

## 13. Overnight Global Market

한국 개장 전 사용 가능:

- S&P500 previous US session
- Nasdaq
- semiconductor index
- VIX
- US futures if timestamp safe
- Nikkei previous session
- China/HK prior data
- ADR-related large Korean company proxies if appropriate

반드시 timezone cut-off를 명시한다.

---

## 14. Commodities

- WTI
- Brent
- Gold
- Copper
- Natural gas
- industrial commodity basket

한국 증시에 대한 sector exposure를 고려한다.

---

## 15. Semiconductor Cycle — KOSPI 중요

연구 후보:
- DRAM price
- NAND price
- semiconductor index
- memory export data
- semiconductor export growth
- semiconductor inventory proxy

데이터 비용/공개시점/라이선스 확인 필수.

---

## 16. News Features

- article_count
- unique_cluster_count
- positive_score
- negative_score
- uncertainty_score
- surprise_score
- policy_topic
- semiconductor_topic
- trade_topic
- geopolitical_topic
- corporate_event_topic
- news_velocity
- after_market_news
- pre_open_news

기사 수 자체도 sentiment와 별개의 feature다.

---

## 17. DART Event Features

- earnings announcement count
- positive earnings surprise proxy
- negative earnings surprise proxy
- buyback
- cancellation of treasury stock
- capital increase
- convertible issuance
- merger/acquisition
- spin-off
- major shareholder change
- large holding report

지수 구성종목별 이벤트를 index weight로 가중한다.

---

## 18. Calendar / Seasonality

- weekday
- month_end
- quarter_end
- year_end
- options_expiry
- futures_expiry
- triple/quadruple witching equivalents
- MSCI rebalance window
- KRX index rebalance window
- ex-dividend
- major macro announcement day
- FOMC/BOK event
- Korean/US holiday adjacency

---

## 19. Surprise / Deviation Features

절대값보다 예상에서 얼마나 벗어났는지가 중요할 수 있다.

- flow z-score
- volume surprise
- volatility surprise
- news volume surprise
- FX shock
- rate shock
- gap surprise

---

## 20. Interaction Features

수동 interaction은 경제적 가설이 있을 때만.

추천 실험:
- foreign_flow × USDKRW
- foreign_flow × futures_basis
- breadth × index_return
- VIX × USDKRW
- semiconductor_return × KOSPI100 concentration
- news_sentiment × foreign_flow
- short_ratio × price_momentum

Tree model은 interaction을 자동 학습하므로 수동 feature와 비교한다.

---

## 21. Alternative Data Discovery Queue

Alternative Data Agent가 매주 검토할 후보:

1. 한국 수출입 high-frequency proxy
2. 반도체 가격
3. 컨테이너/벌크 운임
4. electricity/industrial activity proxy
5. Google/Naver trend
6. IPO/공모 activity
7. corporate bond spread
8. CDS proxy
9. analyst estimate revisions
10. index rebalance expected flow
11. pension allocation events
12. ETF creation/redemption
13. FX swap / funding stress
14. global liquidity
15. news source disagreement
16. market close auction imbalance if legally available
17. broker/member trading concentration if licensed data available
18. order book imbalance for intraday version

모든 후보는 OOS ablation test를 통과해야 한다.
