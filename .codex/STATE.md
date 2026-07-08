# Codex State

## Current Phase

Learned-results static Pages snapshot exported and ready for GitHub Pages push

## Completed

- Added `configs/marketnewsfeed.yaml`.
- Added `src/roboquant/data/providers/marketnewsfeed_site.py`.
  - Fetches only configured `https://marketnewsfeed.com/`.
  - Extracts title, meta description, public links, Telegram links, X links, loading status.
  - Does not infer hidden JavaScript/private endpoints.
  - Writes metadata row to `market_news_feed` with `source=marketnewsfeed_site`.
- Added CLI:
  - `scripts/collect_marketnewsfeed_site.py --config configs/marketnewsfeed.yaml`
- Integrated full retrain order:
  - `collect_telegram_signals`
  - `collect_x_market_news`
  - `collect_marketnewsfeed_site`
  - `build_news_signal_features`
- Existing learning path remains:
  - Telegram `marketfeed` -> `telegram_market_signal_daily` -> feature matrix.
  - X `MarketNews_Feed` -> `market_news_feed` / `news_signal_daily` -> feature matrix.
  - Site metadata row is neutral metadata unless headline text is available from Telegram/X.
- GitHub Pages export now reports:
  - `marketnewsfeed_site`
  - `telegram_marketfeed`
  - `x_market_news_feed`
- README updated with safe MarketNewsFeed operating notes.
- Re-exported current learned results to `docs/` with `run-status=export-only`.
- Started local static preview on `http://localhost:8787/`.

## Verification

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
- OpenDART key will be provided later; keep missing/skipped without fake data.
- DB writer jobs can conflict with running FastAPI/Streamlit/preview processes; stop web before write-heavy pipeline runs.

## Next Recommended Phase

- Push staged code + `docs/` snapshot to `origin master` and confirm GitHub Pages deploy.
- Run full local retrain later if needed:
  `.venv/bin/python scripts/publish_github_pages.py --refresh-mode full --skip-push --no-stop-web`
