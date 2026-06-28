# Codex State

## Current Phase

GitHub Pages synced with 8787 X-news dashboard

## Completed

- Cache busting on `docs/index.html` assets (`?v=manifest generated_at`)
- `docs/` regenerated from `export_github_pages_site.py` (15 data files incl. x_*)
- X news pipeline: provider, collector, impact analysis, signals, configs, tests
- Full code + docs pushed to `master` for Pages deploy

## Verification

- `.venv/bin/pytest tests/test_x_market_news_provider.py tests/test_x_news_impact_analysis.py tests/test_news_signal_features.py tests/test_github_pages_export.py -q` -> 19 passed
- `docs/assets/site.js` matches `reports/github_pages_site/assets/site.js`
- Pages URL: https://junhyuk.github.io/stock_pred/

## Known Issues

- X news requires `X_BEARER_TOKEN` in `.env` for live collection (not committed)
- Local 8787 preview: re-export `reports/github_pages_site` after DB updates

## Next Recommended Phase

- Schedule `publish_github_pages.py` after daily retrain
- Enable Korean official RSS URLs in `configs/market_news.yaml`
