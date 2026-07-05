# Codex State

## Current Phase

Local Full Retrain GitHub Pages Publish 검증 완료

## Completed

- GitHub Pages 정적 dashboard는 `docs/`를 source로 유지
- `.github/workflows/github-pages.yml`은 `docs/**` push 시 Pages deploy만 담당
- `scripts/publish_github_pages.py` 운영 경로 정리
  - `--refresh-mode full`: 로컬 Top50 전체 retrain 후 `docs/` export/commit/push
  - `--refresh-mode light`: 경량 뉴스/오늘/market-up-down 갱신 후 export
  - `--refresh-mode export-only`: 현재 DuckDB snapshot으로 `docs/`만 재생성
  - `--skip-push`: export + validation까지만 수행하고 git stage/commit/push 생략
  - `--restart-web`: 완료 후 FastAPI/Streamlit 재시작
- `docs/data/validation.json`의 `can_publish`가 false면 push 차단
- `.env`, `data/`, `models/`, `reports/` staged path 차단 강화
- README Daily GitHub Pages Publish 문서 갱신
- 2026-07-05 local full retrain 실행 후 `docs/` export 완료
- `http://localhost:8787/` 정적 Pages 데모 실행 및 Chrome screenshot 확인

## Latest Full Retrain Snapshot

- Command:
  - `.venv/bin/python scripts/publish_github_pages.py --refresh-mode full --skip-push --no-stop-web`
- Target trade date: `2026-07-03`
- Manifest:
  - `generated_at`: `2026-07-05T13:04:13Z`
  - `status`: `ready`
- Validation:
  - `can_publish=true`
  - `status=partial_ready`
  - `top50=50`, `top20=20`, `long_short=20`
  - `news=35`, `market_moves=35`, `tomorrow_markets=2`
- Known warning:
  - `X_BEARER_TOKEN` 미설정으로 X news 미수집

## Verification

- `.venv/bin/python scripts/publish_github_pages.py --refresh-mode full --dry-run`
  - PASS: stop_web -> full_local_retrain -> export_docs 순서 확인
- `.venv/bin/python scripts/publish_github_pages.py --refresh-mode full --skip-push --no-stop-web`
  - PASS: full retrain + `docs/` export, validation `can_publish=true`
- `curl http://localhost:8787/data/manifest.json`
  - PASS: `generated_at=2026-07-05T13:04:13Z`, `status=ready`
- `curl http://localhost:8787/data/validation.json`
  - PASS: `can_publish=true`
- Chrome headless screenshot:
  - PASS: `reports/web_smoke/github_pages_static_demo.png`
- `.venv/bin/pytest tests/test_github_pages_export.py tests/test_latest_market_impact_retrain.py -q`
  - PASS: 12 passed
- `.venv/bin/ruff check scripts/publish_github_pages.py scripts/export_github_pages_site.py tests/test_github_pages_export.py README.md`
  - PASS
- Previous full suite:
  - `.venv/bin/pytest -q`: 176 passed, 1 skipped

## Operational Command

```bash
.venv/bin/python scripts/publish_github_pages.py --refresh-mode full
```

Local validation only:

```bash
.venv/bin/python scripts/publish_github_pages.py --refresh-mode export-only --skip-push
```

Cron:

```cron
35 18 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/publish_github_pages.py --refresh-mode full >> logs/github_pages_full_update.log 2>&1
30 20 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/publish_github_pages.py --refresh-mode full >> logs/github_pages_full_update_retry.log 2>&1
```

## Known Issues

- `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md` is missing from repo root although `AGENTS.md` references it
- X news requires `X_BEARER_TOKEN` in `.env` for live collection
- FRED/Naver/KRX credentials are optional; missing keys produce partial/missing status without fake data

## Next Recommended Phase

- Push verified `docs/` snapshot to `origin master` and confirm GitHub Pages Actions deploy
- Optionally add Korean official RSS URLs in `configs/market_news.yaml`
