# Codex State

## Current Phase

GitHub Pages navigation, Long/Short display, and market metrics fallback completed

## Completed

- Added static GitHub Pages navigation buttons:
  - Top20
  - Top50
  - 롱·숏
  - 내일 예측
  - 시장 설명
  - 뉴스
  - 데이터 품질
- Expanded static Pages dashboard sections:
  - Top50 full table from `top50_3M.json`
  - Top50 Long/Short table from `long_short_2M.json`
  - KOSPI/KOSDAQ next-trading-day range cards from `tomorrow.json`
  - existing Top20, market explanations, news, news signals, and quality sections remain visible
- Long/Short counts now validate in `validation.json`.
  - Current preview: Long 10, Short 10
  - KOSPI: LONG 6 / SHORT 6
  - KOSDAQ: LONG 4 / SHORT 4
- Added `market_metrics_daily` fallback:
  - pykrx/KRX bad or empty market response is captured per market.
  - fallback rows use `current_prediction_universe` or latest `prediction_universe_snapshot` market cap.
  - fallback source is `universe_market_cap_fallback`.
- Re-exported static Pages at `reports/github_pages_site/`.
  - `validation.json`: `ready`, `can_publish=true`
  - Counts: Top20 20, Top50 50, Long/Short 20, news 31, news signals 1, market moves 44, tomorrow markets 4
- Local static preview remains available at `http://127.0.0.1:8787/`.

## Modified Files In This Phase

- `.codex/STATE.md`
- `scripts/collect_market_metrics.py`
- `scripts/export_github_pages_site.py`
- `src/roboquant/data/collectors/market_metrics.py`
- `tests/test_github_pages_export.py`
- `tests/test_market_metrics_collector.py`
- Runtime/generated outputs under `data/processed/` and `reports/github_pages_site/`

## Verification

- `.venv/bin/python scripts/collect_market_metrics.py --config configs/top50_normal.yaml --date 2026-06-26` -> `market_metrics_daily rows: 50`
- `.venv/bin/pytest tests/test_github_pages_export.py tests/test_market_metrics_collector.py -q` -> 6 passed
- `.venv/bin/python -m compileall -q app src/roboquant scripts` -> passed
- `.venv/bin/python scripts/run_daily_pages_publish.py --dry-run --skip-retrain` -> exported, pushed=false, validation ready
- `curl -I http://127.0.0.1:8787/` -> HTTP 200
- `curl http://127.0.0.1:8787/data/validation.json` -> validation ready with Long/Short 20

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` and v8 proposal file are absent from the current worktree.
- `KRX_ID/KRX_PW` remain unconfigured; fallback now prevents this from failing the pipeline, but real pykrx market metrics still require credentials/healthy KRX response.
- Naver stock-specific `news_articles` remains empty; Pages shows available official/RSS market news.
- `news_signal_daily` currently has one market-level signal for `2026-06-26`; symbol-level signals need Naver/approved feed coverage.
- A separate pre-existing/untracked docs + GitHub Actions Pages MVP is present in the worktree; this phase did not remove it.
- Real GitHub Pages publish still requires `--publish`, gh-pages branch root configuration, and push access to `origin`.

## Next Recommended Phase

- Review the local static preview, then publish with `scripts/run_daily_pages_publish.py --skip-retrain --publish` once the GitHub Pages `gh-pages` branch source is confirmed.
