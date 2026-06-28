# Codex State

## Current Phase

Credit-balance based next trading day long/short range implemented

## Completed

- Added official-credit-balance input node:
  - new table: `market_credit_balance_daily`
  - new collector: `scripts/collect_market_credit_balance.py`
  - default source: official KOFIA/data.go.kr config/env endpoint
  - missing `DATA_GO_KR_SERVICE_KEY` skips without fake data and records `collection_failures`
- Added config:
  - `market_credit_balance.enabled=true`
  - `source=data_go_kr_kofia`
  - `endpoint=""`
  - `timeout_seconds=20`
- Added next trading day long/short range:
  - `/api/tomorrow/update-snapshot` now returns `long_short_range`
  - `/demo/tomorrow` now shows “다음 거래일 숏·롱 범위”
  - existing `/api/long-short/latest` and `long_short_recommendations` were not changed
- Range formula:
  - combines next-day market outlook with credit pressure
  - missing credit balance uses neutral `credit_pressure_score=0.5`
  - missing credit quality is exposed as `credit_balance=missing`
  - no auto-trading or order logic added
- Runner:
  - `run_latest_market_impact_retrain.py --dry-run` now includes optional `collect_market_credit_balance`
  - step order: Telegram/news collection -> credit balance -> freshness gate -> market outlook regeneration
- Docs:
  - README documents the credit collector and tomorrow long/short range behavior

## Verification

- Tests:
  - `.venv/bin/pytest tests/test_market_credit_balance.py tests/test_today_market_update.py tests/test_latest_market_impact_retrain.py tests/test_schema.py -q` -> 18 passed
  - `.venv/bin/pytest tests/test_market_outlook.py tests/test_today_market_update.py tests/test_market_news_feed.py tests/test_latest_market_impact_retrain.py tests/test_long_short_portfolio.py tests/test_market_credit_balance.py -q` -> 32 passed, 4 warnings
  - `.venv/bin/pytest tests/test_schema.py tests/test_market_credit_balance.py -q` -> 4 passed
  - `.venv/bin/python -m compileall -q app src/roboquant scripts` -> passed
- Actual optional collection:
  - command: `.venv/bin/python scripts/collect_market_credit_balance.py --config configs/top50_normal.yaml --date latest --allow-missing-key`
  - result: skipped without fake data
  - latest failure: `DATA_GO_KR_SERVICE_KEY is not configured`
  - `market_credit_balance_daily`: max date `None`, row count `0`
- Market outlook refresh:
  - `market_outlook_forecasts` regenerated: 4 rows, asof `2026-06-26`
  - next target: `2026-06-29`
- Tomorrow API result:
  - `/api/tomorrow/update-snapshot` -> `partial_ready`
  - `long_short_range.status=partial_ready`
  - `credit_balance=missing`
  - KOSPI: LONG `13.34%~36.66%`, SHORT `63.34%~86.66%`, credit pressure `0.5`
  - KOSDAQ: LONG `18.50%~41.73%`, SHORT `58.27%~81.50%`, credit pressure `0.5`
- Page/API checks:
  - `/demo/tomorrow` -> HTTP 200
  - `/health` -> HTTP 200
  - `/tmp/tomorrow_credit.html` contains `renderTomorrowLongShortRange`, `LONG 범위`, `SHORT 범위`, `Credit pressure`
- Auto-trading forbidden keyword scan:
  - `rg -n "place_order|send_order|buy_market|sell_market|auto_trade|auto_order|kis_order" src scripts app tests` -> no matches

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` is absent from the current worktree.
- `DATA_GO_KR_SERVICE_KEY` and official KOFIA/data.go.kr endpoint are not configured; credit balance remains missing.
- `NAVER_CLIENT_ID/SECRET`, `TELEGRAM_API_ID/HASH`, `FRED_API_KEY`, and `KRX_ID/PW` are missing.
- KRX market metrics/investor flows remain optional/missing in Today quality.
- Yahoo/yfinance raw price rows are stale; Today/Tomorrow display domestic/global fallback rows.
- Regression tests emit existing feedparser warnings.
- `git status` fails with "not a git repository" in this workspace.

## Next Recommended Phase

- Configure official KOFIA/data.go.kr credit balance endpoint/key, then rerun the latest-data dependency graph so credit pressure uses real KOSPI/KOSDAQ balances instead of neutral fallback.
