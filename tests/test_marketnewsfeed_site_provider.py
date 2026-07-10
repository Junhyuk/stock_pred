from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from roboquant.data.providers.marketnewsfeed_site import (
    MarketNewsFeedSiteProvider,
    MarketNewsFeedSiteSettings,
    parse_marketnewsfeed_html,
  rendered_headlines_to_frame,
)
from roboquant.db import connect_database

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "collect_marketnewsfeed_site.py"
SPEC = importlib.util.spec_from_file_location("collect_marketnewsfeed_site", SCRIPT_PATH)
assert SPEC is not None
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


HTML = """
<!doctype html>
<html>
<head>
  <title>Market News Feed — live headlines with live charts</title>
  <meta name="description" content="Live market headlines and charts">
</head>
<body>
  <h1>MARKET NEWS FEED</h1>
  <p>loading…</p>
  <p>headlines:
    <a href="https://t.me/marketfeed">t.me/marketfeed</a>
    <a href="https://t.me/CryptoNewsFeed1">crypto</a>
    <a href="https://x.com/MarketNews_Feed">X</a>
  </p>
</body>
</html>
"""

RENDERED_HTML = """
<main>
  <article class="card" id="card-h11301" data-hid="11301" data-ts="1783689559">
    <div class="card-main">
      <div class="meta">
        <time title="2026-07-10T13:19:19.000Z">13:19:19 · 8m ago</time>
        <span class="chan">MF</span>
        <span class="cat">STOCKS</span>
        <span class="imp" title="importance 3/5">●●●</span>
      </div>
      <h2 class="hl">US PRES TRUMP: I WILL NOT SIGN THE HOUSING BILL</h2>
      <div class="assets">
        <button class="chip-sym" data-sym="XHB">XHB</button>
        <button class="chip-sym" data-sym="DHI">DHI</button>
      </div>
      <div class="srcs"><a href="https://x.com/LiveSquawk/status/1">@LiveSquawk ↗</a></div>
    </div>
  </article>
</main>
"""


def test_parse_marketnewsfeed_html_extracts_public_metadata() -> None:
    parsed = parse_marketnewsfeed_html(HTML, base_url="https://marketnewsfeed.com/")

    assert parsed["title"] == "Market News Feed — live headlines with live charts"
    assert parsed["meta_description"] == "Live market headlines and charts"
    assert parsed["contains_loading"] is True
    assert [item["href"] for item in parsed["telegram_links"]] == [
        "https://t.me/marketfeed",
        "https://t.me/CryptoNewsFeed1",
    ]
    assert parsed["x_links"][0]["href"] == "https://x.com/MarketNews_Feed"


def test_provider_requests_only_configured_site_url_and_stores_metadata_row() -> None:
    requested: list[str] = []

    def opener(request):
        requested.append(request.full_url)
        return HTML.encode("utf-8")

    settings = MarketNewsFeedSiteSettings(site_url="https://marketnewsfeed.com/")
    provider = MarketNewsFeedSiteProvider(opener=opener)

    frame = provider.fetch_metadata(settings=settings)

    assert requested == ["https://marketnewsfeed.com/"]
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["source"] == "marketnewsfeed_site"
    assert row["sentiment_score"] == 0.5
    assert "No hidden JavaScript" in row["summary"]
    raw = json.loads(row["raw_json"])
    assert raw["private_or_js_endpoints_inferred"] is False
    assert raw["telegram_links"][0]["href"] == "https://t.me/marketfeed"


def test_rendered_headlines_are_normalized_as_shadow_rows() -> None:
    frame = rendered_headlines_to_frame(
        RENDERED_HTML,
        base_url="https://marketnewsfeed.com/",
    )

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["source"] == "marketnewsfeed_site_shadow"
    assert row["site_category"] == "STOCKS"
    assert row["title"] == "US PRES TRUMP: I WILL NOT SIGN THE HOUSING BILL"
    assert row["source_url"] == "https://x.com/LiveSquawk/status/1"
    assert json.loads(row["tickers_json"]) == ["XHB", "DHI"]
    assert row["importance"] == 3
    assert str(row["published_at"]) == "2026-07-10 13:19:19"
    raw = json.loads(row["raw_json"])
    assert raw["shadow_only"] is True
    assert raw["impact_returns_collected"] is False


def test_collect_marketnewsfeed_site_dry_run_uses_config(tmp_path) -> None:
    db_path = tmp_path / "site.duckdb"
    conn = connect_database(db_path)
    conn.close()
    config_path = tmp_path / "marketnewsfeed.yaml"
    config_path.write_text(
        f"""
paths:
  database: {db_path}
marketnewsfeed:
  site_url: https://marketnewsfeed.com/
  telegram_channel: marketfeed
  x_username: MarketNews_Feed
""",
        encoding="utf-8",
    )

    result = collector.run_collection(config_path, dry_run=True)

    assert "source: marketnewsfeed_site" in result
    assert "mode: public_dom_shadow" in result


def test_collect_marketnewsfeed_site_persists_rendered_headlines_as_shadow(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "site.duckdb"
    conn = connect_database(db_path)
    conn.close()
    config_path = tmp_path / "marketnewsfeed.yaml"
    config_path.write_text(
        f"""
paths:
  database: {db_path}
marketnewsfeed:
  site_url: https://marketnewsfeed.com/
  crawl_site_metadata: false
  crawl_rendered_headlines: true
  max_headlines: 10
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(collector, "render_marketnewsfeed_dom", lambda *args, **kwargs: RENDERED_HTML)

    result = json.loads(collector.run_collection(config_path))

    conn = connect_database(db_path, read_only=True, initialize_schema=False)
    rows = conn.execute("SELECT source, title FROM marketnewsfeed_headlines").fetchall()
    feed_rows = conn.execute("SELECT COUNT(*) FROM market_news_feed").fetchone()[0]
    conn.close()
    assert result["status"] == "ready"
    assert result["headline_rows"] == 1
    assert rows == [
        (
            "marketnewsfeed_site_shadow",
            "US PRES TRUMP: I WILL NOT SIGN THE HOUSING BILL",
        )
    ]
    assert feed_rows == 0
