from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from roboquant.db import connect_database

ROOT = Path(__file__).resolve().parents[1]

EXPORT_SPEC = importlib.util.spec_from_file_location(
    "export_github_pages_site",
    ROOT / "scripts" / "export_github_pages_site.py",
)
assert EXPORT_SPEC is not None
exporter = importlib.util.module_from_spec(EXPORT_SPEC)
assert EXPORT_SPEC.loader is not None
sys.modules[EXPORT_SPEC.name] = exporter
EXPORT_SPEC.loader.exec_module(exporter)

PUBLISH_SPEC = importlib.util.spec_from_file_location(
    "run_daily_pages_publish",
    ROOT / "scripts" / "run_daily_pages_publish.py",
)
assert PUBLISH_SPEC is not None
publisher = importlib.util.module_from_spec(PUBLISH_SPEC)
assert PUBLISH_SPEC.loader is not None
sys.modules[PUBLISH_SPEC.name] = publisher
PUBLISH_SPEC.loader.exec_module(publisher)

DOCS_PUBLISH_SPEC = importlib.util.spec_from_file_location(
    "publish_github_pages",
    ROOT / "scripts" / "publish_github_pages.py",
)
assert DOCS_PUBLISH_SPEC is not None
docs_publisher = importlib.util.module_from_spec(DOCS_PUBLISH_SPEC)
assert DOCS_PUBLISH_SPEC.loader is not None
sys.modules[DOCS_PUBLISH_SPEC.name] = docs_publisher
DOCS_PUBLISH_SPEC.loader.exec_module(docs_publisher)


def test_export_github_pages_site_writes_static_json_and_relative_assets(tmp_path) -> None:
    db_path = tmp_path / "site.duckdb"
    conn = connect_database(db_path)
    conn.close()
    config_path = _write_config(tmp_path / "top50.yaml", db_path)
    today_config_path = _write_config(tmp_path / "today.yaml", db_path)
    output = tmp_path / "site"

    result = exporter.export_site(
        config_path=config_path,
        today_config_path=today_config_path,
        output_dir=output,
        run_status="partial_ready",
        run_result={"status": "partial_ready", "NAVER_CLIENT_SECRET": "super-secret"},
    )

    assert result["status"] == "exported"
    assert (output / "index.html").exists()
    assert (output / "assets" / "site.css").exists()
    assert (output / "assets" / "site.js").exists()
    assert (output / "data" / "manifest.json").exists()
    assert (output / "data" / "dashboard.json").exists()
    assert (output / "data" / "today.json").exists()
    assert (output / "data" / "tomorrow.json").exists()
    assert (output / "data" / "news_signal_latest.json").exists()
    assert (output / "data" / "official_news_status.json").exists()
    assert (output / "data" / "news_url_crawl.json").exists()
    assert (output / "data" / "x_market_news_status.json").exists()
    assert (output / "data" / "x_news_top20_impact.json").exists()
    assert (output / "data" / "x_market_outlook_impact.json").exists()
    assert (output / "data" / "validation.json").exists()

    manifest = json.loads((output / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "partial_ready"
    assert manifest["run_result"]["NAVER_CLIENT_SECRET"] == "[redacted]"
    js = (output / "assets" / "site.js").read_text(encoding="utf-8")
    html = (output / "index.html").read_text(encoding="utf-8")
    assert 'const DATA = "data/"' in js
    assert "/api/" not in js
    assert "/api/" not in html
    assert 'href="#top20-section"' in html
    assert 'href="#top50-section"' in html
    assert 'href="#long-short-section"' in html
    assert 'href="#tomorrow-section"' in html
    assert 'href="#x-impact-section"' in html
    assert 'href="#official-data-section"' in html
    assert 'load("top50_3M")' in js
    assert 'load("long_short_2M")' in js
    assert 'load("official_news_status")' in js
    assert 'load("news_url_crawl")' in js
    assert 'load("x_news_top20_impact")' in js
    assert 'load("x_market_outlook_impact")' in js
    assert "function renderLongShort" in js
    assert "function renderXImpact" in js
    assert "function renderOfficialData" in js
    assert "function representativesByMarket" in js
    assert "KOSPI LONG" in js
    assert "No data" not in js
    assert "No news collected" not in js
    assert "뉴스 미수집" in js
    validation = json.loads((output / "data" / "validation.json").read_text(encoding="utf-8"))
    assert validation["status"] in {"failed", "partial_ready", "ready"}
    assert "news_signals" in validation["counts"]
    assert "long_short" in validation["counts"]
    assert "x_marketnews_feed" in validation["counts"]
    assert "x_market_news_feed" in validation["counts"]
    assert "marketnewsfeed_site" in validation["counts"]
    assert "marketnewsfeed_headlines" in validation["counts"]
    assert "telegram_marketfeed" in validation["counts"]
    assert "news_url_crawl" in validation["counts"]
    assert "bigkinds_issues" in validation["counts"]
    assert "gdelt_news" in validation["counts"]
    assert "dart_disclosures" in validation["counts"]
    assert "official_price_rows" in validation["counts"]
    assert "x_news_top20_impact" in validation["counts"]
    assert "x_market_outlook_impact" in validation["counts"]
    x_impact = json.loads((output / "data" / "x_news_top20_impact.json").read_text(encoding="utf-8"))
    assert x_impact["status"] == "not_collected"
    assert "X 뉴스 영향도 미생성" in x_impact["messages"][0]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
    assert "super-secret" not in combined
    assert "연구·정보제공용" in combined


def test_export_news_latest_uses_market_news_feed_when_stock_news_empty(tmp_path) -> None:
    db_path = tmp_path / "site.duckdb"
    conn = connect_database(db_path)
    conn.execute(
        """
        INSERT INTO market_news_feed (
          article_id,
          source,
          category,
          title,
          summary,
          link,
          pub_date,
          tickers_json,
          themes_json,
          sentiment_score,
          raw_json,
          collected_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "fed-1",
            "fed_press",
            "macro",
            "Federal Reserve stress test update",
            "Large banks remain resilient under stress.",
            "https://www.federalreserve.gov/example",
            "2026-06-25 15:00:00",
            "[]",
            '["RATE"]',
            0.5,
            "{}",
            "2026-06-28 06:00:00",
        ],
    )
    conn.close()
    config_path = _write_config(tmp_path / "top50.yaml", db_path)
    today_config_path = _write_config(tmp_path / "today.yaml", db_path)
    output = tmp_path / "site"

    exporter.export_site(
        config_path=config_path,
        today_config_path=today_config_path,
        output_dir=output,
        run_status="ready",
    )

    news = json.loads((output / "data" / "news_latest.json").read_text(encoding="utf-8"))
    validation = json.loads((output / "data" / "validation.json").read_text(encoding="utf-8"))

    assert len(news["items"]) == 1
    assert news["items"][0]["source_name"] == "fed_press"
    assert news["items"][0]["description"] == "Large banks remain resilient under stress."
    assert validation["counts"]["news"] == 1
    assert "뉴스 미수집" not in "\n".join(validation["messages"])


def test_export_validation_reports_missing_x_bearer_token(tmp_path) -> None:
    db_path = tmp_path / "site.duckdb"
    conn = connect_database(db_path)
    conn.execute(
        """
        INSERT INTO collection_failures (
          collected_at,
          step,
          source,
          symbol,
          target_date,
          error_message,
          retry_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "2026-06-28 06:00:00",
            "collect_x_market_news",
            "x_marketnews_feed",
            None,
            "2026-06-28",
            "X_BEARER_TOKEN is required for X API v2 collection.",
            0,
        ],
    )
    conn.close()
    config_path = _write_config(tmp_path / "top50.yaml", db_path)
    today_config_path = _write_config(tmp_path / "today.yaml", db_path)
    output = tmp_path / "site"

    exporter.export_site(
        config_path=config_path,
        today_config_path=today_config_path,
        output_dir=output,
        run_status="partial_ready",
    )

    status = json.loads((output / "data" / "x_market_news_status.json").read_text(encoding="utf-8"))
    validation = json.loads((output / "data" / "validation.json").read_text(encoding="utf-8"))

    assert status["status"] == "missing_key"
    assert validation["counts"]["x_marketnews_feed"] == 0
    assert "X 뉴스 미수집: X_BEARER_TOKEN 설정 필요" in validation["messages"]


def test_publish_site_dry_run_does_not_create_or_push_worktree(tmp_path) -> None:
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text("ok", encoding="utf-8")
    (site / "data" / "manifest.json").write_text("{}", encoding="utf-8")
    (site / "data" / "validation.json").write_text('{"status":"failed","can_publish":false}', encoding="utf-8")
    worktree = tmp_path / "gh-pages"

    result = publisher.publish_site(site_dir=site, worktree=worktree, dry_run=True, push=False)

    assert result["status"] == "dry_run"
    assert result["pushed"] is False
    assert result["file_count"] == 3
    assert not worktree.exists()


def test_publish_site_blocks_failed_validation_without_allow_empty(tmp_path) -> None:
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text("ok", encoding="utf-8")
    (site / "data" / "validation.json").write_text('{"status":"failed","can_publish":false}', encoding="utf-8")

    with pytest.raises(ValueError, match="validation failed"):
        publisher.publish_site(site_dir=site, worktree=tmp_path / "gh-pages", dry_run=False, push=False)


def test_publish_github_pages_full_dry_run_order() -> None:
    args = docs_publisher.parse_args(
        [
            "--refresh-mode",
            "full",
            "--dry-run",
            "--config",
            "configs/top50_normal.yaml",
            "--universe-config",
            "configs/universe_top50.yaml",
            "--provider",
            "fdr_poc",
            "--target-date",
            "latest",
        ]
    )

    steps = docs_publisher.build_steps(args, run_status=args.refresh_mode)
    names = [step.name for step in steps]
    commands = [" ".join(step.command) for step in steps]

    assert names == ["stop_web", "full_local_retrain", "export_docs"]
    assert "scripts/run_latest_market_impact_retrain.py" in commands[1]
    assert "--universe-config configs/universe_top50.yaml" in commands[1]
    assert "--provider fdr_poc" in commands[1]
    assert "scripts/export_github_pages_site.py" in commands[2]
    assert "--output docs" in commands[2]


def test_publish_github_pages_export_only_skips_refresh() -> None:
    args = docs_publisher.parse_args(["--refresh-mode", "export-only", "--skip-push"])

    steps = docs_publisher.build_steps(args, run_status=args.refresh_mode)

    assert [step.name for step in steps] == ["stop_web", "export_docs"]
    assert all("run_latest_market_impact_retrain.py" not in " ".join(step.command) for step in steps)


def test_publish_github_pages_skip_refresh_aliases_export_only() -> None:
    args = docs_publisher.parse_args(["--skip-refresh", "--no-stop-web"])
    if args.skip_refresh:
        args.refresh_mode = "export-only"

    steps = docs_publisher.build_steps(args, run_status=args.refresh_mode)

    assert [step.name for step in steps] == ["export_docs"]


def _write_config(path: Path, db_path: Path) -> Path:
    path.write_text(
        f"""
paths:
  database: {db_path}
  report_dir: {path.parent / "reports"}

universe:
  rule: prediction_top_market_cap

horizons:
  2M: 42
  3M: 63
  6M: 126

market_outlook:
  use_pykrx_calendar: false

market_credit_balance:
  enabled: true
""",
        encoding="utf-8",
    )
    return path
