# Codex State

## Current Phase

MarketNewsFeed public headline shadow collection and Pages snapshot completed

## Completed

- Diagnosed the reported post-OpenDART accuracy drop.
  - DART has 18 rows on 2026-07-08..09 and 90 nonzero feature rows.
  - Labeled 3M training rows with nonzero DART values: 0/415025.
  - Saved 3M classifier/regressor DART importance: all 0; DART did not cause the drop.
- Found the actual next-day forecast defect in `src/roboquant/market_outlook.py`.
  - Sparse global/news features used defaults near 50 historically but live values near 0.
  - Live values reached -16..-22 standard deviations and distorted TODAY forecasts.
- Added chronological 20% validation metrics to each market/horizon model spec.
- Added a minimum history gate: feature variation needs at least 60 rows and 5% of training rows.
- Added standardized-feature clipping at +/-5 for remaining OOD values.
- Bumped guarded forecasts to `market-outlook-v2` for traceability.
- Updated `scripts/train_market_outlook.py` to report validation accuracy/RMSE.
- Refreshed approved `fdr_poc` prices through 2026-07-10 and persisted guarded 2026-07-13 forecasts.
- Existing DART collection, leakage-safe rolling features, and 6M+ exclusion policy remain intact.

- Upgraded `src/roboquant/data/providers/marketnewsfeed_site.py` to collect public rendered cards.
  - Uses bounded local Chrome DevTools rendering; no hidden/private endpoint inference.
  - Stores title, UTC time, category, source URL, tickers and importance only.
  - Excludes article bodies and displayed post-news return/chart values.
- Added isolated `marketnewsfeed_headlines` shadow table and deterministic deduplication.
- Collector falls back to the existing neutral metadata row if Chrome rendering fails.
- Headline rows are not connected to `news_signal_daily`, feature matrix, or recommendation scores.
- Pages Official Data now displays shadow count and latest 8 public headline links.

## Verification

- `.venv/bin/pytest tests/test_market_outlook.py -q`
  - PASS: 10 passed
- `.venv/bin/ruff check src/roboquant/market_outlook.py scripts/train_market_outlook.py tests/test_market_outlook.py`
  - PASS
- `.venv/bin/python scripts/train_market_outlook.py --config configs/top50_normal.yaml --date latest`
  - PASS after 2026-07-10 refresh: KOSPI TODAY holdout accuracy 0.5941; KOSDAQ 0.5188.
- `.venv/bin/python scripts/generate_market_outlook.py --config configs/top50_normal.yaml --date latest`
  - PASS: 4 rows persisted for asof 2026-07-10; TODAY target is 2026-07-13.
- Guarded TODAY forecast comparison:
  - KOSPI: -3.0250% / up 0.0209 -> +0.5850% / up 0.6502.
  - KOSDAQ: -2.2693% / up 0.0932 -> +0.1182% / up 0.5270.
- Prior DART focused suite remains PASS: 13 tests; feature build and 3M training completed.
- Full latest-market pipeline for target 2026-07-10: PASS with freshness `ready`.
  - 3M walk-forward hit ratio: 0.5769; precision@20: 0.5878; gate: production.
  - 3M stored precision@k: 0.2504; stored IC: 0.0615.
  - Final TODAY: KOSPI -0.0082%, KOSDAQ -0.0756%, both neutral.
- `.venv/bin/pytest tests/test_marketnewsfeed_site_provider.py tests/test_latest_market_impact_retrain.py tests/test_github_pages_export.py tests/test_schema.py tests/test_market_outlook.py -q`
  - PASS: 28 passed
- MarketNewsFeed live collection: PASS, 50 rows / 50 distinct IDs, all with public source URLs.
- MarketNewsFeed integration Ruff check: PASS.
- Pages export: PASS, `can_publish=true`, `marketnewsfeed_headlines=50`.
- Local browser: PASS at `http://localhost:8787/`; desktop/mobile overflow checks passed.

- `.venv/bin/pytest tests/test_marketnewsfeed_site_provider.py tests/test_latest_market_impact_retrain.py tests/test_github_pages_export.py tests/test_telegram_config.py tests/test_telegram_signal_features.py -q`
  - PASS: 22 passed
- `.venv/bin/ruff check src/roboquant/data/providers/marketnewsfeed_site.py scripts/collect_marketnewsfeed_site.py scripts/run_latest_market_impact_retrain.py scripts/export_github_pages_site.py tests/test_marketnewsfeed_site_provider.py tests/test_latest_market_impact_retrain.py tests/test_github_pages_export.py README.md`
  - PASS
- `.venv/bin/python -m compileall -q src scripts tests`
  - PASS
- `.venv/bin/python scripts/run_latest_market_impact_retrain.py --config configs/top50_normal.yaml --universe-config configs/universe_top50.yaml --provider fdr_poc --target-date 2026-07-08 --dry-run`
  - PASS: MarketNewsFeed step appears before `build_news_signal_features`.
- `.venv/bin/python scripts/collect_marketnewsfeed_site.py --config configs/marketnewsfeed.yaml`
  - PASS: `marketnewsfeed_site` row upserted.
- `.venv/bin/python scripts/collect_x_market_news.py --config configs/x_market_news.yaml --allow-missing-key`
  - PASS: skipped without fake data because `X_BEARER_TOKEN` is not configured.
- `.venv/bin/python scripts/collect_telegram_signals.py --config configs/telegram_signals.yaml`
  - PASS: skipped without fake data because Telegram credentials are not configured.
- Temp Pages export:
  - `official_news_status.counts.marketnewsfeed_site=1`
  - `telegram_marketfeed=0`
  - `x_market_news_feed=0`
- `.venv/bin/python scripts/publish_github_pages.py --refresh-mode export-only --skip-push --no-stop-web`
  - PASS: `docs/` generated with 17 data files
- `docs/data/validation.json`
  - PASS: `can_publish=true`, `status=partial_ready`
  - counts: `top50=50`, `top20=20`, `long_short=20`, `marketnewsfeed_site=1`
- `curl http://localhost:8787/`
  - PASS: Top20, Top50, Long/Short, Official Data & URL Crawl sections present

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` is missing from repo root although `AGENTS.md` references it.
- Telegram live collection requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and a session.
- X live collection requires `X_BEARER_TOKEN`.
- DART/global/news history is too short for training; sparse inputs are now blocked until support gates pass.
- MarketNewsFeed headlines are shadow-only until sufficient history and A/B validation exist.
- GDELT collection failed as an optional source; no synthetic replacement was used.
- DB writer jobs can conflict with running FastAPI/Streamlit/preview processes; stop web before write-heavy pipeline runs.

## Next Recommended Phase

- Backfill approved global-market history through existing provider adapters, then run a walk-forward
  baseline-vs-global ablation. Keep gated features disabled unless direction accuracy and RMSE pass.
- Accumulate at least 60 trading days of MarketNewsFeed shadow history, then evaluate a Korea-relevance
  filtered baseline-vs-news ablation; do not promote raw headline counts directly.
- Accumulate DART disclosures and labels; do not enable DART model weight until coverage and A/B gates pass.
- Export Pages only after `docs/data/validation.json` reports `can_publish=true`.
