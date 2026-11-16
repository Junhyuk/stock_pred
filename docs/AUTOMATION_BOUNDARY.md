# Research forecast automation boundary

GitHub Actions on `ubuntu-latest` is the runner for the next-session Korea and US research forecast. Cloudflare Workers are not. scikit-learn and LightGBM stay on the GitHub-hosted runner.

One workflow starts Monday–Friday at 07:50 Asia/Seoul (`50 22 * * 0-4` UTC), subject to GitHub scheduling delays. It collects and freezes actual inputs before08:00, waits at most ten minutes for the real08:00 cutoff, then restores the same immutable manifest, runs forecasts, verifies publication, commits reviewed payloads and deploys Pages. The whole forecast job is capped at25minutes and Pages at5minutes. The separate smoke workflow is manual-only; its quick checks run inside the daily job.

KST Saturday and Sunday are blocked for all operating modes. Manual validation remains a non-publishing exception. Late starts never collect replacement08:00 data: only a verified same-day pre08:00 preparation may be used, otherwise the previous forecast is retained with a not-ready status. A verified daily or legacy predict publication records the completed KST date; repeat daily runs skip collection, forecasts and publication. Missing preparation and failed runs do not claim completion. Manual prepare/predict/latest remain available on weekdays. See [daily operations](DAILY_AUTOMATION.md).

Public Pages files `reliable_index_forecast.json` and `reliable_validation_report.json` omit `candidate_observations`, `observations`, and `calibration_observations`. The full out-of-fold rows stay in the in-memory report and in `archive/{run_id}.json`. Those archive directories are compressed and uploaded as one Actions artifact kept for 14 days. They are not git-committed. Checksums are SHA-256 of the bytes actually written.

`production_weight` stays 0. News fusion alpha stays 0 unless a separate validated model passes its own gate. This job does not approve production and does not change the Ridge market-outlook model.

Price refresh on a clean runner uses yfinance `^KS11` and `^KQ11` for KOSPI and KOSDAQ only. `published_at` stays null, and the source text says yfinance and that the original publication time is unknown. Existing index dates are not replaced. Missing investor flow stays missing. KOSPI100 remains official code 1034. KOSDAQ100 stays not ready. The US snapshot is a new dated CSV under `data/processed`; a changed existing CSV is not overwritten. When a previous run cached `data/processed/index_forecast.sqlite` and the latest dated US CSV, and that history still reaches the 2019 backfill, the next session appends new closes. A cache miss still requests history from 2019-01-01. The cache is not restored into git. SQLite, DuckDB, `data/raw`, and `data/processed` are not committed. This job does not collect Market News Feed.

The scheduled job does not collect current news or pass an old event file as current context. X is not scraped.
