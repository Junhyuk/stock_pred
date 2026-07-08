# Codex State

## Current Phase

Official data sources + existing news URL crawl GitHub Pages demo ready

## Completed

- Added official-data DuckDB tables:
  - `news_url_crawl_cache`
  - `bigkinds_issue_daily`
  - `global_news_events`
  - `dart_disclosures`
  - `official_market_prices`
  - `official_data_runs`
- Expanded Naver Search News collection:
  - `collect_naver_news.py` now supports `--display`, `--max-pages`, `--deep`, `--since-hours`
  - Top50 default changed to `display=100`, `max_pages=3`
- Added official provider config:
  - `configs/official_data_sources.yaml`
  - optional env: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `DATA_GO_KR_SERVICE_KEY`, `DART_API_KEY`, `KRX_AUTH_KEY`
- Added official collection CLIs:
  - `scripts/collect_official_news.py`
  - `scripts/collect_bigkinds_issues.py`
  - `scripts/collect_gdelt_news.py`
  - `scripts/collect_dart_disclosures.py`
  - `scripts/collect_official_market_prices.py`
- Added existing URL metadata crawler:
  - `scripts/crawl_existing_news_urls.py`
  - only uses URLs already in DB/config
  - respects robots by default
  - stores metadata and short excerpt, not full article text
- Integrated optional full publish steps before news feature build:
  - BIGKinds, GDELT, existing URL crawl, OpenDART, official market prices
- Extended GitHub Pages export:
  - `docs/data/official_news_status.json`
  - `docs/data/news_url_crawl.json`
  - validation counts for official news/crawl/disclosure/price rows
  - dashboard section “Official Data & URL Crawl”
- README updated with usage and safety notes.
- 2026-07-08 local Pages demo refreshed:
  - `news_url_crawl_cache=30`
  - `docs/data/news_url_crawl.json` status `ready`
  - `docs/data/official_news_status.json` status `partial_ready`
  - `docs/data/validation.json` `can_publish=true`
  - local preview: `http://localhost:8787/`

## Verification

- `.venv/bin/pytest tests/test_schema.py tests/test_naver_news_provider.py tests/test_news_url_crawler.py tests/test_official_data_sources.py tests/test_github_pages_export.py tests/test_latest_market_impact_retrain.py -q`
  - PASS: 24 passed
- `.venv/bin/ruff check ...`
  - PASS
- `.venv/bin/python scripts/publish_github_pages.py --refresh-mode full --dry-run --no-stop-web`
  - PASS: wrapper order confirmed
- `.venv/bin/python scripts/run_latest_market_impact_retrain.py --config configs/top50_normal.yaml --universe-config configs/universe_top50.yaml --provider fdr_poc --target-date 2026-07-08 --dry-run`
  - PASS: new optional official-data steps appear before `build_news_signal_features`
- `.venv/bin/python scripts/export_github_pages_site.py --config configs/top50_normal.yaml --today-config configs/today_update.yaml --output /tmp/... --run-status test`
  - PASS: `official_news_status.json`, `news_url_crawl.json`, `validation.json` generated
- `.venv/bin/python -m compileall -q src scripts tests`
  - PASS
- `.venv/bin/python scripts/crawl_existing_news_urls.py --config configs/top50_normal.yaml --limit 30`
  - PASS: 30 URL metadata rows cached
- `.venv/bin/python scripts/collect_gdelt_news.py --config configs/official_data_sources.yaml --date latest`
  - FAILED twice due network timeout; failure metadata recorded, no fake data
- `.venv/bin/python scripts/publish_github_pages.py --refresh-mode export-only --skip-push --no-stop-web`
  - PASS: `docs/` regenerated
- `curl http://localhost:8787/`
  - PASS: “Official Data & URL Crawl” section present

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` is missing from repo root although `AGENTS.md` references it
- BIGKinds/KRX/Data.go.kr endpoints are intentionally blank until approved endpoint mappings are configured
- OpenDART key will be provided later; current Pages snapshot correctly shows missing/skipped without fake data
- GDELT live collection timed out in this run; retry later before a full market/news update
- URL crawler stores only short excerpts and metadata; no full-text archive is implemented

## Next Recommended Phase

- Push committed code/docs to `origin master` and confirm GitHub Pages deploy
- Configure approved `DATA_GO_KR_SERVICE_KEY`, `DART_API_KEY`, `KRX_AUTH_KEY`, and provider endpoints, then run a live optional collection smoke test
- Continue with close-loop 1Y retrain/gate phase if desired
