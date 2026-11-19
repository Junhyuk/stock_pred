# Robo Stock next-session accuracy research

The KOSPI and KOSDAQ endpoint is each index's next regular-session close. The
primary metric is mean absolute error in index points on the same dates for
every model in a comparison. Return error, probability quality and interval
quality are reported separately. A historical replay is exploratory evidence.
Actual forecasts and their outcomes are matched in the forward ledger by
market, target, protocol and role.

Run this command from the repository root with the existing forecast Python
environment and an explicit audit time with a timezone.

```bash
python scripts/run_robo_stock_accuracy.py \
  --sqlite /path/to/index_forecast.sqlite \
  --closes-csv /path/to/close_crosscheck.csv \
  --closes-observed-at 2026-11-19T20:34:02+09:00 \
  --archive /path/to/full_forecast_archive.json \
  --publication /path/to/the_matching/reliable_publication.json \
  --ledger /path/to/postclose_forward_history.json \
  --legacy-db /path/to/roboquant.duckdb \
  --as-of 2026-11-19T20:35:00+09:00 \
  --output-dir logs/robo-accuracy/example
```

- The SQLite source is opened read only. The close CSV must have
  `index_code,session_date,close` columns with ISO session dates and one row
  per market and session. Supply the snapshot retrieval time and preserve
  known `published_at` or `observed_at` columns in the input CSV. Both
  the snapshot and row timestamps must be available by the audit cutoff.
  Differences between overlapping close sources are
  excluded. A date-only publication field is historical provenance, without
  a verified release time.
- Use the full archive JSON from the `reliable-forecast-archives` Actions
  artifact. Obtain the receipt and ledger from the commit created by that
  forecast run. The CLI checks the archive SHA-256 against the receipt and
  audits the ledger against its history hash. Compact Pages forecasts omit
  the training evaluation records required for replay.
- The output directory must be new. `accuracy.json` contains model,
  window and cohort comparisons. `accuracy_daily.csv` contains the price
  comparison rows, point errors and any exclusions. Use `--audit-only` to
  regenerate the latest 30-session accuracy audit without fitting candidates.
  `--legacy-db` counts available existing 1D market-outlook rows separately.
- The audit applies sourced CSAT session overrides on November 14, 2024,
  November 13, 2025, and November 19, 2026. The original ledger records what
  was issued.
  Any calendar discrepancy is recorded alongside the corrected audit
  maturity time. [KOSPI notice](https://kind.krx.co.kr/external/2026/11/05/000141/20261105000216/99303.htm)
  and [KOSDAQ notice](https://kind.krx.co.kr/external/2026/11/05/000145/20261104001218/70780.htm).

## Candidate assessment

The point candidates are a rolling 504-session Ridge and a rolling
504-session Huber model with volatility-normalized returns. Both reuse
the existing price features and fit at fixed 21-session anchors with a
one-session purge. The flat previous-close, expanding-mean, and archived
price-only model are the point baselines. The archived calibrated
probability is compared with a recent 252-outcome Laplace-smoothed frequency.
Interval comparisons include coverage, width, lower breaches and interval
score. Candidate selection is restricted to the pre-existing development
dates. The older published holdout and recent 30 completed sessions are
descriptive checks.

The research command assigns zero production weight. A candidate requires
paired evidence on 252 new, outcome-mature actual forecasts per market and
independent review before a change to the Robo Stock serving path. Use the
manual `Premarket smoke` workflow for the focused runner checks. Historical
fits are run separately from the scheduled weekday forecast.

## Rates, prior-session flows and margin credit

Use a dedicated SQLite file for source observations. The importer supports
canonical CSV snapshots and the daily **신용공여 잔고 추이** XLSX export from
FreeSIS. The XLSX path requires local `openpyxl`. Enter the export's actual
receipt time with a timezone. The importer checks the headers, settlement
dates, KRW units and market totals before appending.

```bash
python scripts/collect_robo_yields.py --start 2019-01-02 --output /path/to/yields.csv
python scripts/import_robo_session_inputs.py \
  --db data/processed/robo_session_inputs.sqlite /path/to/yields.csv
python scripts/import_robo_session_inputs.py \
  --db data/processed/robo_session_inputs.sqlite \
  --kofia-credit-xlsx /path/to/freesis-credit.xlsx \
  --received-at 2026-11-19T21:17:37+09:00
```

Add `--include-korea` to the yield command when an authorized `ECOS_API_KEY`
or `BOK_API_KEY` is configured. The U.S. collector reads Treasury's public
daily par yield curve. The Korean API path reads daily 1Y, 3Y and 10Y
observations from ECOS table 817Y002.
[U.S. Treasury](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml),
[ECOS](https://ecos.bok.or.kr/api/).

CSV fields are `market,series,observation_date,value,unit,source,received_at,`
`published_at,is_final,availability_policy`. Percent rates use `GLOBAL` and
the `KR_YIELD_1Y` through `KR_YIELD_10Y` or corresponding `US_` series for
the three supported tenors. Cash flow uses market-specific `FOREIGN_NET`,
`INSTITUTION_NET`, `RETAIL_NET` and `MARKET_TURNOVER` in KRW. Use source
`krx_market_investor_value` for the official market-level data. Store a blank
publication time or finality field when the source has no verified value.
Retrying the same import adds zero rows. A revision or newly verified earlier
receipt adds a row while preserving the existing version history.
[KRX market investor trades](https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020301).

Add `--session-input-db data/processed/robo_session_inputs.sqlite` to the full
accuracy command. In `accuracy.json`, `additional_inputs` compares each
feature group to a Ridge price-only model trained on identical sessions, the
archived baseline and naive baselines. It reports matched coverage, per-regime
errors, and paired performance with the feature group removed. Cash flow uses
the session immediately preceding the target with verified finality and receipt.
Historical dated yields and FreeSIS credit balances use a two-KRX-session
lag for retrospective reconstruction. Actual historical publication and
revision times are unknown for those snapshots. The research forecast also
checks actual receipt against the invocation cutoff. Credit uses the balance
and its log change from the two latest eligible observations. Use markets'
own balances when both are available on the same source.
[FreeSIS](https://freesis.kofia.or.kr/stat/main.do),
[FSC credit API](https://www.data.go.kr/data/15094809/openapi.do).

The 2026-11-19 snapshots arrived after the scheduled forecast's cutoff.
Those snapshots support a later research comparison for 2026-11-20. The
standalone test retains zero production weight and runs outside the scheduled
workflow. The raw data and append-only SQLite stay local.

## Additional input priorities

Test previous-session open gaps, intraday ranges and closing location first.
The local SQLite check found 1,927 of 1,929 daily rows with valid OHLC per
market through 2026-11-11. These features reuse the existing price pipeline.
Compare each addition on the same evaluation sessions to measure its impact.

| Priority | Input and hypothesis | Source, availability and readiness |
|---|---|---|
| 1 | KOSPI/KOSDAQ advancing share and up/down turnover to distinguish broad participation from concentrated moves | KRX lists equity daily trading data from 2010-01-04. Historical listed and suspended membership, publication times, price-access rights and missingness require verification. [KRX service catalog](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd) |
| 2 | Final foreign and institutional cash net flow scaled by turnover, then futures flows, open interest and expiry to test continuation and hedging | KRX lists cash and derivatives trading statistics. Product coverage, investor breakdown access, cost and earliest final release times require confirmation. Regular and night trading sessions need separate attribution. [KRX statistics](https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd) |
| 3 | USD/KRW and US two-year yield changes to test currency and discount-rate shocks beyond existing US equity features | ECOS currency item 731Y001/0000001 reports the reference rate. Fed H.15 is released on US working days at 16:15 Eastern time. Track observations and revisions, and verify the first receipt of each value. [ECOS](https://ecos.bok.or.kr/api/) and [H.15](https://www.federalreserve.gov/releases/h15/about.htm). |

Use authorized sources and retain the actual availability time and vintage.
Before feature engineering, measure coverage and missingness over at least
three years if available, and record source terms and cost. Evaluate each
feature block independently against the same point and probability baselines,
then remove it from the combined model to measure incremental skill by market
and volatility regime. Incremental skill and ablation results are pending
eligible observations.
