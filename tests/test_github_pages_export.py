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
    assert 'load("top50_3M")' in js
    assert 'load("long_short_2M")' in js
    assert "function renderLongShort" in js
    assert "function representativesByMarket" in js
    assert "KOSPI LONG" in js
    assert "No data" not in js
    assert "No news collected" not in js
    assert "뉴스 미수집" in js
    validation = json.loads((output / "data" / "validation.json").read_text(encoding="utf-8"))
    assert validation["status"] in {"failed", "partial_ready", "ready"}
    assert "news_signals" in validation["counts"]
    assert "long_short" in validation["counts"]
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
