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
- Latest domestic benchmarks end on 2026-07-16 while global data ends on
  2026-07-17. The 2026-07-20 live candidate stays `blocked_stale_input`; no
  substitute forecast was created. There are no mature live linkage rows yet.

## Verification

- `pytest tests/test_market_us_linkage.py tests/test_schema.py
  tests/test_premarket_pipeline.py tests/test_latest_market_impact_retrain.py
  tests/test_github_pages_export.py -q` passed: 21 tests.
- Targeted Ruff and `python -m compileall -q app scripts src/roboquant` pass.
- FastAPI HTTP smoke passed for the linkage demo and all three APIs. Chrome
  headless rendered the local page; static `docs/` preview and validation pass
  with `can_publish=true`.

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` is absent from the repository root.
- DuckDB supports one writer: stop writable web/jobs before collector or
  backtest commands.
- The local domestic close freshness guard correctly blocks a new Monday
  forecast until the 2026-07-17 KOSPI/KOSDAQ and Top50 closes are available.

## Next Recommended Phase

1. Repair the approved domestic price source and rerun the freshness-gated
   daily pipeline before attempting a confirmed next-session forecast.
2. Accumulate at least 60 live final observations. Keep the Nasdaq/SOX overlay
   shadow-only even if it later becomes `accepted_shadow`.
3. Improve interval calibration with a separate residual/conformal candidate;
   compare it to this rejected linkage model without altering production.
