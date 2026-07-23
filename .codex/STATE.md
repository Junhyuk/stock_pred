# Codex State

## Current Phase

Nasdaq/SOX-to-KRX linkage and close-loop weighting are implemented as a
strictly separate market-outlook shadow. `market-outlook-v2` Ridge remains the
only production market forecast.

## Completed

- Added cutoff-safe `market_us_linkage_features_daily`, immutable shadow
  forecasts, mature feedback rows, and backtest-run tables in DuckDB.
- Linked only the final premarket U.S. session to the next KRX session:
  Nasdaq/SOX 1D/5D returns, `SOX - Nasdaq` spreads, 20D volatility, shock
  flags, freshness, and cutoff provenance. News and local-LLM inputs are
  explicitly excluded.
- Added an independent Huber/Ridge overlay per KOSPI and KOSDAQ with 504-day
  initial training, one-day purge, 21-day retraining, and 252-residual 80%
  intervals. The candidate is `baseline + dynamic_weight * global_delta`.
- Added a close-loop feedback writer. It records only realised target-session
  outcomes and never rewrites a first-seen shadow forecast; dynamic weight is
  capped at 20% and is zero when the last 60 mature rows lack positive global
  MAE skill.
- Added per-market gates, APIs, `/demo/us-korea-linkage`, static Pages payload
  `docs/data/market_us_linkage_shadow.json`, and daily pipeline hooks after
  final global collection and after the domestic close.

## Latest Linkage Result (2026-07-20 KST)

- 1,074 aligned feature sessions/market and 569 OOS forecasts/market were
  built from 2022-01-05 through 2026-07-20.
- Correlation with the next KRX session: KOSPI SOX `+0.393`, Nasdaq `+0.358`;
  KOSDAQ SOX `+0.338`, Nasdaq `+0.331`.
- KOSPI candidate: MAE `1.356%` vs baseline `1.375%`; direction `55.89%` vs
  `52.02%`; 80% coverage `73.11%`; mean dynamic weight `11.31%`.
- KOSDAQ candidate: MAE `1.396%` vs baseline `1.405%`; direction `49.74%` vs
  `49.38%`; 80% coverage `74.69%`; mean dynamic weight `7.75%`.
- Both markets are `rejected` for coverage and range-width gates, so
  `production_weight=0.0`. This is intentional: no production forecast or
  recommendation changed.
- Domestic KOSPI/KOSDAQ benchmarks and all 50 active-universe closes were
  refreshed through 2026-07-23. The domestic freshness gate now passes.
- A four-row `market-outlook-v2-2026-07-23` forecast targets 2026-07-24. It is
  explicitly `domestic_index_only`: fresh Top50 breadth/KORU and the final
  2026-07-23 U.S. close were not available at the after-close run. The
  production Ridge is unchanged; no Nasdaq/SOX shadow was promoted.
- Exported the matching GitHub Pages snapshot at `2026-07-23T12:24:52Z`.
  `validation.json` is `can_publish=true` / `partial_ready`, retains Top50 50,
  Top20 20, Long/Short 20, and exposes both 2026-07-24 market cards with the
  same `domestic_index_only` state.

## Verification

- `pytest tests/test_market_us_linkage.py tests/test_schema.py
  tests/test_premarket_pipeline.py tests/test_latest_market_impact_retrain.py
  tests/test_github_pages_export.py -q` passed: 21 tests.
- Targeted Ruff and `python -m compileall -q app scripts src/roboquant` pass.
- `pytest tests/test_market_outlook.py -q` passed: 16 tests after repairing
  the forecast freshness/date join helpers; targeted Ruff and compileall pass.
- `pytest tests/test_schema.py tests/test_dashboard_v7.py
  tests/test_market_outlook.py tests/test_market_regime.py
  tests/test_prediction_price_gap.py tests/test_market_outlook_direction_shadow.py
  -q` passed: 42 tests. Targeted Ruff and compileall pass; desktop/mobile
  Chrome smoke tests rendered the dynamic and static market demos.
- FastAPI HTTP smoke passed for the linkage demo and all three APIs. Chrome
  headless rendered the local page; static `docs/` preview and validation pass
  with `can_publish=true`.

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` is absent from the repository root.
- DuckDB supports one writer: stop writable web/jobs before collector or
  backtest commands.
- A full premarket forecast must wait for the final U.S. 2026-07-23 session and
  an 08:00 KST cutoff-safe regime build; the current 2026-07-24 row is not a
  substitute for that run.

## Next Recommended Phase

1. At 07:30-08:00 KST on 2026-07-24, collect the final U.S. close, rebuild the
   cutoff-safe global regime, then refresh Top50 breadth/KORU and rerun the
   full next-session forecast plus shadow candidates.
2. Accumulate at least 60 live final observations. Keep the Nasdaq/SOX overlay
   shadow-only even if it later becomes `accepted_shadow`.
3. Improve interval calibration with a separate residual/conformal candidate;
   compare it to this rejected linkage model without altering production.
