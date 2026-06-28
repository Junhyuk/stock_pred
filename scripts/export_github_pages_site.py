#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.dashboard.dashboard_service import (
    get_latest_dashboard_snapshot,
    get_latest_news,
    get_market_move_explanations,
    get_today_market_snapshot,
    get_tomorrow_market_snapshot,
    get_top20_upside_recommendations,
    get_top50_universe,
)
from roboquant.dashboard.long_short_service import get_latest_long_short
from roboquant.dashboard.price_forecast_service import get_top20_price_forecast
from roboquant.db import connect_database, get_table_columns, table_exists

DEFAULT_OUTPUT = ROOT / "reports" / "github_pages_site"
DISCLAIMER = "연구·정보제공용 대시보드입니다. 투자 판단과 책임은 사용자 본인에게 있으며 수익을 보장하지 않습니다."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a static GitHub Pages dashboard.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--today-config", default="configs/today_update.yaml")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--run-status", default="manual")
    parser.add_argument("--run-result-file", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_result = _read_json_file(args.run_result_file) if args.run_result_file else {}
    result = export_site(
        config_path=args.config,
        today_config_path=args.today_config,
        output_dir=args.output,
        run_status=args.run_status,
        run_result=run_result,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))


def export_site(
    *,
    config_path: str | Path = "configs/top50_normal.yaml",
    today_config_path: str | Path = "configs/today_update.yaml",
    output_dir: str | Path = DEFAULT_OUTPUT,
    run_status: str = "manual",
    run_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    today_config = load_config(today_config_path)
    output = Path(output_dir)
    data_dir = output / "data"
    assets_dir = output / "assets"
    if output.exists():
        shutil.rmtree(output)
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    database = get_database_path(config)
    conn = connect_database(database, read_only=database.exists(), initialize_schema=not database.exists())
    try:
        payloads = _build_payloads(conn, today_config)
    finally:
        conn.close()

    manifest = {
        "generated_at": _utcnow().isoformat(timespec="seconds") + "Z",
        "status": run_status,
        "disclaimer": DISCLAIMER,
        "source": {
            "config": str(config_path),
            "universe_rule": config.get("universe", {}).get("rule", "prediction_top_market_cap"),
            "publish_surface": "github_pages_static_summary",
        },
        "run_result": _sanitize(run_result or {}),
        "data_files": sorted(f"{name}.json" for name in payloads),
    }
    payloads["manifest"] = manifest

    for name, payload in payloads.items():
        _write_json(data_dir / f"{name}.json", _sanitize(payload))
    asset_version = _asset_version(str(manifest["generated_at"]))
    (output / "index.html").write_text(_index_html(asset_version), encoding="utf-8")
    (assets_dir / "site.css").write_text(_site_css(), encoding="utf-8")
    (assets_dir / "site.js").write_text(_site_js(), encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return {
        "status": "exported",
        "output": str(output),
        "files": len(list(output.rglob("*"))),
        "data_files": len(payloads),
    }


def _build_payloads(conn, today_config: dict[str, Any]) -> dict[str, Any]:
    payloads = {
        "dashboard": _safe_payload(lambda: get_latest_dashboard_snapshot(conn)),
        "today": _safe_payload(lambda: get_today_market_snapshot(conn, today_config)),
        "tomorrow": _safe_payload(lambda: get_tomorrow_market_snapshot(conn, today_config)),
        "top20_upside_3M": _safe_payload(lambda: get_top20_upside_recommendations(conn, horizon="3M", limit=20)),
        "top20_price_forecast": _safe_payload(
            lambda: get_top20_price_forecast(conn, horizons="3M,6M,9M,1Y", limit=20, base_horizon="3M")
        ),
        "top50_3M": _safe_payload(lambda: get_top50_universe(conn, horizon="3M", universe_rule="prediction_top_market_cap")),
        "long_short_2M": _safe_payload(lambda: get_latest_long_short(conn, horizon="2M")),
        "market_move_explanations": _safe_payload(
            lambda: get_market_move_explanations(conn, date="latest", scope="top50", limit=100, config=today_config)
        ),
        "news_latest": {"items": _safe_payload(lambda: _latest_news_items(conn, limit=50), fallback=[])},
    }
    payloads["x_market_news_status"] = _safe_payload(lambda: _x_market_news_status(conn))
    payloads["news_signal_latest"] = _safe_payload(lambda: _latest_news_signals(conn))
    payloads["x_news_top20_impact"] = _safe_payload(lambda: _x_news_top20_impact(conn))
    payloads["x_market_outlook_impact"] = _safe_payload(lambda: _x_market_outlook_impact(conn))
    payloads["validation"] = _validate_payloads(payloads)
    return payloads


def _latest_news_items(conn, *, limit: int = 50) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in get_latest_news(conn, limit=limit):
        normalized = dict(item)
        normalized.setdefault("kind", "stock_news")
        normalized.setdefault("source_name", normalized.get("source_name") or "news_articles")
        items.append(normalized)
    items.extend(_latest_market_news_items(conn, limit=limit))
    return _dedupe_sort_news(items, limit=limit)


def _latest_market_news_items(conn, *, limit: int = 50) -> list[dict[str, Any]]:
    if not table_exists(conn, "market_news_feed"):
        return []
    frame = conn.execute(
        """
        SELECT
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
          collected_at
        FROM market_news_feed
        ORDER BY pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        LIMIT ?
        """,
        [int(limit)],
    ).fetchdf()
    items: list[dict[str, Any]] = []
    for item in frame.to_dict(orient="records"):
        item["kind"] = "market_news"
        item["source_name"] = item.get("source") or "market_news_feed"
        item["description"] = item.get("summary") or ""
        items.append(item)
    return items


def _dedupe_sort_news(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("article_id") or item.get("link") or f"{item.get('title')}|{item.get('pub_date')}")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped.sort(key=_news_sort_key, reverse=True)
    return deduped[: int(limit)]


def _news_sort_key(item: dict[str, Any]) -> str:
    value = item.get("pub_date") or item.get("query_date") or item.get("collected_at") or ""
    return str(value)


def _latest_news_signals(conn) -> dict[str, Any]:
    if not table_exists(conn, "news_signal_daily"):
        return {
            "status": "not_collected",
            "asof_date": None,
            "summary": {"count": 0, "negative_max": 0.0, "source_diversity_max": 0.0},
            "items": [],
            "messages": ["뉴스 신호 테이블이 없습니다."],
        }
    row = conn.execute("SELECT MAX(signal_date) FROM news_signal_daily").fetchone()
    asof = row[0] if row else None
    if asof is None:
        return {
            "status": "not_collected",
            "asof_date": None,
            "summary": {"count": 0, "negative_max": 0.0, "source_diversity_max": 0.0},
            "items": [],
            "messages": ["뉴스 신호가 아직 생성되지 않았습니다."],
        }
    columns = set(get_table_columns(conn, "news_signal_daily"))
    order_column = "news_negative_attention_score" if "news_negative_attention_score" in columns else "news_attention_score"
    frame = conn.execute(
        """
        SELECT *
        FROM news_signal_daily
        WHERE signal_date = ?
        ORDER BY
          CASE WHEN scope = 'market' THEN 0 ELSE 1 END,
          {order_column} DESC NULLS LAST,
          symbol
        LIMIT 100
        """.format(order_column=order_column),
        [asof],
    ).fetchdf()
    defaults = {
        "news_negative_attention_score": 0.0,
        "news_bias_adjusted_sentiment_score": 0.5,
        "news_source_diversity_score": 0.0,
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
    items = frame.to_dict(orient="records")
    negative_values = [
        float(item.get("news_negative_attention_score") or 0.0)
        for item in items
        if item.get("news_negative_attention_score") is not None
    ]
    diversity_values = [
        float(item.get("news_source_diversity_score") or 0.0)
        for item in items
        if item.get("news_source_diversity_score") is not None
    ]
    return {
        "status": "ready" if items else "not_collected",
        "asof_date": asof,
        "summary": {
            "count": len(items),
            "negative_max": max(negative_values) if negative_values else 0.0,
            "source_diversity_max": max(diversity_values) if diversity_values else 0.0,
        },
        "items": items,
        "messages": [] if items else ["뉴스 신호가 비어 있습니다."],
    }


def _x_market_news_status(conn) -> dict[str, Any]:
    count = 0
    latest_pub_date = None
    if table_exists(conn, "market_news_feed"):
        row = conn.execute(
            """
            SELECT COUNT(*), MAX(pub_date)
            FROM market_news_feed
            WHERE source = 'x_marketnews_feed'
            """
        ).fetchone()
        count = int(row[0] or 0)
        latest_pub_date = row[1]
    latest_failure = None
    if table_exists(conn, "collection_failures"):
        frame = conn.execute(
            """
            SELECT collected_at, error_message
            FROM collection_failures
            WHERE step = 'collect_x_market_news'
               OR source = 'x_marketnews_feed'
            ORDER BY collected_at DESC
            LIMIT 1
            """
        ).fetchdf()
        if not frame.empty:
            latest_failure = frame.iloc[0].to_dict()
    status = "ready" if count > 0 else "missing_key" if _is_missing_x_token_failure(latest_failure) else "not_collected"
    messages = []
    if status == "missing_key":
        messages.append("X 뉴스 미수집: X_BEARER_TOKEN 설정 필요")
    elif status == "not_collected":
        messages.append("X 뉴스 미수집: collect_x_market_news.py 실행 필요")
    return {
        "status": status,
        "source": "x_marketnews_feed",
        "count": count,
        "latest_pub_date": latest_pub_date,
        "latest_failure_at": None if latest_failure is None else latest_failure.get("collected_at"),
        "messages": messages,
    }


def _is_missing_x_token_failure(latest_failure: dict[str, Any] | None) -> bool:
    if not latest_failure:
        return False
    return "X_BEARER_TOKEN" in str(latest_failure.get("error_message") or "")


def _x_news_top20_impact(conn) -> dict[str, Any]:
    if not table_exists(conn, "x_news_prediction_impact_daily"):
        return _empty_x_impact_payload("x_news_prediction_impact_daily 테이블이 없습니다.")
    row = conn.execute("SELECT MAX(asof_date) FROM x_news_prediction_impact_daily").fetchone()
    asof = row[0] if row else None
    if asof is None:
        return _empty_x_impact_payload("X 뉴스 영향도 미생성: X 뉴스 수집 또는 build_x_news_impact_analysis.py 실행 필요")
    frame = conn.execute(
        """
        SELECT *
        FROM x_news_prediction_impact_daily
        WHERE asof_date = ?
        ORDER BY ABS(pred_prob_delta) DESC NULLS LAST, ABS(rank_delta) DESC NULLS LAST, rank_with_x
        LIMIT 100
        """,
        [asof],
    ).fetchdf()
    items = frame.to_dict(orient="records")
    return {
        "status": "ready" if items else "not_collected",
        "asof_date": asof,
        "summary": {
            "count": len(items),
            "top20_changed": sum(1 for item in items if bool(item.get("top20_with_x")) != bool(item.get("top20_without_x"))),
            "max_probability_delta": max((abs(float(item.get("pred_prob_delta") or 0.0)) for item in items), default=0.0),
        },
        "items": items,
        "messages": [] if items else ["X 뉴스 영향도 미생성: X 뉴스 수집 또는 build_x_news_impact_analysis.py 실행 필요"],
    }


def _x_market_outlook_impact(conn) -> dict[str, Any]:
    if not table_exists(conn, "x_market_outlook_impact_daily"):
        return _empty_x_impact_payload("x_market_outlook_impact_daily 테이블이 없습니다.")
    row = conn.execute("SELECT MAX(asof_date) FROM x_market_outlook_impact_daily").fetchone()
    asof = row[0] if row else None
    if asof is None:
        return _empty_x_impact_payload("X 시장전망 영향도 미생성: X 뉴스 수집 또는 build_x_news_impact_analysis.py 실행 필요")
    frame = conn.execute(
        """
        SELECT *
        FROM x_market_outlook_impact_daily
        WHERE asof_date = ?
        ORDER BY
          CASE WHEN horizon = 'TODAY' THEN 0 WHEN horizon = 'WEEK' THEN 1 ELSE 2 END,
          CASE WHEN market = 'KOSPI' THEN 0 WHEN market = 'KOSDAQ' THEN 1 ELSE 2 END
        LIMIT 20
        """,
        [asof],
    ).fetchdf()
    items = frame.to_dict(orient="records")
    return {
        "status": "ready" if items else "not_collected",
        "asof_date": asof,
        "summary": {
            "count": len(items),
            "max_expected_return_delta": max((abs(float(item.get("expected_return_delta") or 0.0)) for item in items), default=0.0),
        },
        "items": items,
        "messages": [] if items else ["X 시장전망 영향도 미생성: X 뉴스 수집 또는 build_x_news_impact_analysis.py 실행 필요"],
    }


def _empty_x_impact_payload(message: str) -> dict[str, Any]:
    return {
        "status": "not_collected",
        "asof_date": None,
        "summary": {"count": 0},
        "items": [],
        "messages": [message],
    }


def _validate_payloads(payloads: dict[str, Any]) -> dict[str, Any]:
    x_status = payloads.get("x_market_news_status") or {}
    counts = {
        "top20": len((payloads.get("top20_upside_3M") or {}).get("items") or []),
        "top50": len((payloads.get("top50_3M") or {}).get("items") or []),
        "long_short": len((payloads.get("long_short_2M") or {}).get("long_leg") or [])
        + len((payloads.get("long_short_2M") or {}).get("short_leg") or []),
        "news": len((payloads.get("news_latest") or {}).get("items") or []),
        "x_marketnews_feed": int(x_status.get("count") or 0),
        "news_signals": len((payloads.get("news_signal_latest") or {}).get("items") or []),
        "x_news_top20_impact": len((payloads.get("x_news_top20_impact") or {}).get("items") or []),
        "x_market_outlook_impact": len((payloads.get("x_market_outlook_impact") or {}).get("items") or []),
        "market_moves": len((payloads.get("market_move_explanations") or {}).get("market") or [])
        + len((payloads.get("market_move_explanations") or {}).get("top50") or []),
        "tomorrow_markets": len(((payloads.get("tomorrow") or {}).get("market_outlook") or {}).get("items") or []),
    }
    errors = []
    warnings = []
    for name in ("dashboard", "today", "tomorrow", "top20_upside_3M", "top50_3M", "news_latest", "news_signal_latest"):
        payload = payloads.get(name)
        if payload is None:
            errors.append(f"{name}.json 생성 실패")
        elif isinstance(payload, dict) and payload.get("status") == "error":
            errors.append(f"{name}.json 오류: {payload.get('message')}")
    if sum(counts.values()) == 0:
        errors.append("모든 주요 섹션이 비어 있어 publish를 막았습니다.")
    if counts["news"] == 0:
        warnings.append("뉴스 미수집: NAVER_CLIENT_ID/SECRET 또는 공식 RSS 설정 필요")
    if counts["news_signals"] == 0:
        warnings.append("뉴스 신호 미생성: build_news_signal_features.py 실행 필요")
    if counts["top20"] == 0:
        warnings.append("Top20 추천 데이터가 비어 있습니다.")
    if counts["long_short"] == 0:
        warnings.append("롱·숏 추천 데이터가 비어 있습니다.")
    warnings.extend(str(message) for message in x_status.get("messages") or [] if x_status.get("status") == "missing_key")
    status = "failed" if errors else "ready" if not warnings else "partial_ready"
    return {
        "status": status,
        "can_publish": not errors,
        "counts": counts,
        "errors": errors,
        "warnings": warnings,
        "messages": errors + warnings,
    }


def _safe_payload(func, fallback: Any | None = None) -> Any:
    try:
        return func()
    except Exception as exc:
        if fallback is not None:
            return fallback
        return {"status": "error", "message": str(exc)}


def _asset_version(generated_at: str) -> str:
    return "".join(ch for ch in generated_at if ch.isdigit() or ch in "TZ")


def _index_html(version: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Robo Stock Daily</title>
  <link rel="stylesheet" href="assets/site.css?v={version}" />
</head>
<body>
  <main>
    <header class="topbar">
      <div>
        <p class="eyebrow">AI Robo Stock</p>
        <h1>Top50 Daily Dashboard</h1>
      </div>
      <div id="runStatus" class="status">loading</div>
    </header>
    <nav class="quick-nav" aria-label="대시보드 섹션 이동">
      <a href="#top20-section">Top20</a>
      <a href="#top50-section">Top50</a>
      <a href="#long-short-section">롱·숏</a>
      <a href="#tomorrow-section">내일 예측</a>
      <a href="#x-impact-section">X 뉴스 영향도</a>
      <a href="#moves-section">시장 설명</a>
      <a href="#news-section">뉴스</a>
      <a href="#quality-section">데이터 품질</a>
    </nav>
    <section id="summary" class="metric-grid"></section>
    <section class="layout">
      <article id="top20-section" class="panel wide">
        <h2>Top20 Upside</h2>
        <div id="top20" class="table"></div>
      </article>
      <article id="top50-section" class="panel wide">
        <h2>Top50 Universe</h2>
        <div id="top50" class="table"></div>
      </article>
      <article id="long-short-section" class="panel wide">
        <h2>Top50 Long/Short</h2>
        <div id="longShort" class="stack"></div>
      </article>
      <article id="tomorrow-section" class="panel wide">
        <h2>KOSPI/KOSDAQ Tomorrow Range</h2>
        <div id="tomorrow" class="stack"></div>
      </article>
      <article id="x-impact-section" class="panel wide">
        <h2>X News Impact</h2>
        <div id="xImpact" class="stack"></div>
      </article>
      <article id="quality-section" class="panel">
        <h2>Market Quality</h2>
        <div id="quality" class="stack"></div>
      </article>
      <article class="panel">
        <h2>News Signals</h2>
        <div id="newsSignals" class="stack"></div>
      </article>
      <article id="moves-section" class="panel wide">
        <h2>Market Move Explanations</h2>
        <div id="moves" class="table"></div>
      </article>
      <article id="news-section" class="panel wide">
        <h2>Latest News</h2>
        <div id="news" class="news-list"></div>
      </article>
    </section>
    <p id="disclaimer" class="disclaimer"></p>
  </main>
  <script src="assets/site.js?v={version}"></script>
</body>
</html>
"""


def _site_css() -> str:
    return """:root {
  --bg: #f4f6f8;
  --panel: #ffffff;
  --ink: #1e252b;
  --muted: #66717d;
  --line: #d9dee5;
  --teal: #0f766e;
  --amber: #b7791f;
  --red: #b42318;
  --green-bg: #e8f5f2;
  --amber-bg: #fff6df;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: Inter, Pretendard, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0;
}
main { width: min(1180px, calc(100vw - 28px)); margin: 0 auto; padding: 24px 0 40px; }
h1, h2, p { margin: 0; }
h1 { font-size: 28px; line-height: 1.2; }
h2 { font-size: 16px; margin-bottom: 12px; }
.topbar {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: end;
  margin-bottom: 16px;
}
.quick-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 14px;
}
.quick-nav a {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  padding: 7px 10px;
  color: var(--ink);
  font-size: 13px;
  font-weight: 800;
  text-decoration: none;
}
.quick-nav a:hover { border-color: var(--teal); color: var(--teal); }
.eyebrow { color: var(--teal); font-weight: 800; margin-bottom: 4px; }
.status {
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 8px 10px;
  border-radius: 8px;
  color: var(--muted);
  font-weight: 700;
  text-align: right;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.metric, .panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
  min-width: 0;
  scroll-margin-top: 12px;
}
.metric span, .muted { color: var(--muted); }
.metric strong { display: block; margin-top: 6px; font-size: 22px; color: var(--teal); overflow-wrap: anywhere; }
.layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.wide { grid-column: 1 / -1; }
.stack { display: grid; gap: 8px; }
.inline-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.inline-metric {
  padding: 8px 0;
  border-bottom: 1px solid var(--line);
}
.inline-metric span { color: var(--muted); font-size: 12px; }
.inline-metric strong { display: block; margin-top: 4px; font-size: 18px; color: var(--teal); }
.subgrid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.subsection h3 {
  margin: 6px 0 8px;
  font-size: 14px;
}
.market-card {
  border-top: 1px solid var(--line);
  padding-top: 10px;
}
.market-card h3 {
  margin: 0 0 6px;
  font-size: 14px;
}
.row { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.row b { text-align: right; }
.badge { display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 800; background: var(--green-bg); color: var(--teal); }
.badge.warn { background: var(--amber-bg); color: var(--amber); }
.badge.risk { background: #fff0ed; color: var(--red); }
.table { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 800; }
a { color: var(--teal); }
.news-list { display: grid; gap: 10px; }
.news-item { border-bottom: 1px solid var(--line); padding-bottom: 10px; }
.news-item a { font-weight: 800; text-decoration: none; }
.disclaimer { color: var(--muted); margin-top: 18px; font-size: 13px; }
.empty { color: var(--muted); padding: 8px 0; }
@media (max-width: 820px) {
  main { width: min(100vw - 20px, 680px); padding-top: 14px; }
  .topbar { align-items: stretch; flex-direction: column; }
  .metric-grid, .layout, .inline-metrics, .subgrid { grid-template-columns: 1fr; }
  .wide { grid-column: auto; }
}
"""


def _site_js() -> str:
    return """const DATA = "data/";

async function load(name) {
  const response = await fetch(`${DATA}${name}.json`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${name} ${response.status}`);
  return response.json();
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}
function num(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(3);
}
function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Math.round(Number(value)).toLocaleString();
}
function shortDate(value) { return value ? String(value).slice(0, 10) : "-"; }
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function metric(label, value, sub = "") {
  return `<article class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><p class="muted">${esc(sub)}</p></article>`;
}
function table(items, columns) {
  if (!items || items.length === 0) return '<div class="empty">데이터 미수집: 최신 파이프라인 실행 또는 API 키 설정이 필요합니다.</div>';
  const head = columns.map((column) => `<th>${esc(column.label)}</th>`).join("");
  const body = items.map((item) => `<tr>${columns.map((column) => {
    const raw = item[column.key];
    const value = column.format ? column.format(raw, item) : esc(raw ?? "-");
    return `<td>${value}</td>`;
  }).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function statusBadge(status) {
  const value = String(status || "-");
  const cls = value === "ready" || value === "exported" ? "badge" : value.includes("error") ? "badge risk" : "badge warn";
  return `<span class="${cls}">${esc(value)}</span>`;
}

async function boot() {
  const [manifest, dashboard, today, tomorrow, top20, top50, longShort, moves, news, newsSignals, xTop20Impact, xMarketImpact, validation] = await Promise.all([
    load("manifest"),
    load("dashboard"),
    load("today"),
    load("tomorrow"),
    load("top20_upside_3M"),
    load("top50_3M"),
    load("long_short_2M"),
    load("market_move_explanations"),
    load("news_latest"),
    load("news_signal_latest"),
    load("x_news_top20_impact"),
    load("x_market_outlook_impact"),
    load("validation"),
  ]);
  document.getElementById("runStatus").innerHTML = `${statusBadge(validation.status || manifest.status)}<br><span>${shortDate(manifest.generated_at)}</span>`;
  document.getElementById("disclaimer").textContent = manifest.disclaimer || dashboard.disclaimer || "";
  const todayQuality = today.data_quality || {};
  const tomorrowItems = representativesByMarket(tomorrow.market_outlook?.items || []);
  const longShortCount = (longShort.long_leg || []).length + (longShort.short_leg || []).length;
  document.getElementById("summary").innerHTML = [
    metric("Snapshot", shortDate(dashboard.snapshot_date || today.snapshot_date), today.status || "-"),
    metric("Top50", validation.counts?.top50 ?? top50.items?.length ?? 0, shortDate(top50.snapshot_date)),
    metric("Long/Short", longShortCount, `${longShort.horizon || "2M"} · ${shortDate(longShort.asof_date)}`),
    metric("Tomorrow", tomorrowItems.length, tomorrow.next_trading_day || "-"),
  ].join("");
  document.getElementById("top20").innerHTML = table(top20.items || [], [
    { key: "rank", label: "#" },
    { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
    { key: "market", label: "Market" },
    { key: "up_probability", label: "Up Prob", format: pct },
    { key: "upside_return", label: "Upside", format: pct },
    { key: "risk_score", label: "Risk", format: pct },
  ]);
  document.getElementById("top50").innerHTML = renderTop50(top50);
  document.getElementById("longShort").innerHTML = renderLongShort(longShort);
  document.getElementById("tomorrow").innerHTML = renderTomorrow(tomorrow);
  document.getElementById("xImpact").innerHTML = renderXImpact(xTop20Impact, xMarketImpact);
  document.getElementById("quality").innerHTML = renderQuality(todayQuality, validation);
  document.getElementById("newsSignals").innerHTML = renderNewsSignals(newsSignals);
  document.getElementById("moves").innerHTML = table([...(moves.market || []), ...(moves.top50 || [])].slice(0, 20), [
    { key: "name", label: "Name", format: (value, row) => esc(value || row.market || row.symbol || "-") },
    { key: "move_pct", label: "Move", format: pct },
    { key: "primary_reason", label: "Reason" },
    { key: "confidence", label: "Confidence", format: pct },
  ]);
  document.getElementById("news").innerHTML = renderNews(news.items || []);
}

function renderTop50(data) {
  return table(data.items || [], [
    { key: "prediction_rank", label: "#" },
    { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
    { key: "market", label: "Market" },
    { key: "market_cap", label: "Market Cap", format: money },
    { key: "close", label: "Close", format: money },
    { key: "volume", label: "Volume", format: money },
    { key: "pred_prob_top20", label: "Up Prob", format: pct },
    { key: "pred_return", label: "Return", format: pct },
    { key: "recommendation_rank", label: "Top20 Rank" },
  ]);
}

function renderLongShort(data) {
  const markets = data.markets || {};
  const kospi = markets.KOSPI || { long_leg: [], short_leg: [] };
  const kosdaq = markets.KOSDAQ || { long_leg: [], short_leg: [] };
  const totalLong = (data.long_leg || []).length;
  const totalShort = (data.short_leg || []).length;
  const kospiCount = (kospi.long_leg || []).length + (kospi.short_leg || []).length;
  const kosdaqCount = (kosdaq.long_leg || []).length + (kosdaq.short_leg || []).length;
  return [
    `<div class="inline-metrics">
      ${inlineMetric("기준일", shortDate(data.asof_date), data.horizon || "2M")}
      ${inlineMetric("LONG", totalLong, "모의 long leg")}
      ${inlineMetric("SHORT", totalShort, "모의 short leg")}
      ${inlineMetric("시장분할", kospiCount + "/" + kosdaqCount, "KOSPI/KOSDAQ")}
    </div>`,
    `<div class="subgrid">
      ${legSection("KOSPI LONG", kospi.long_leg || [], "long_score", "pred_prob_top20", "상승확률")}
      ${legSection("KOSPI SHORT", kospi.short_leg || [], "short_score", "pred_prob_bottom20", "하락확률")}
      ${legSection("KOSDAQ LONG", kosdaq.long_leg || [], "long_score", "pred_prob_top20", "상승확률")}
      ${legSection("KOSDAQ SHORT", kosdaq.short_leg || [], "short_score", "pred_prob_bottom20", "하락확률")}
    </div>`,
    `<p class="muted">${esc(data.disclaimer || "롱·숏 추천은 정보제공용 모의 시뮬레이션입니다.")}</p>`,
  ].join("");
}

function inlineMetric(label, value, sub = "") {
  return `<div class="inline-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><p class="muted">${esc(sub)}</p></div>`;
}

function legSection(title, items, scoreKey, probabilityKey, probabilityLabel) {
  return `<section class="subsection">
    <h3>${esc(title)}</h3>
    ${table(items || [], [
      { key: "rank", label: "#" },
      { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
      { key: scoreKey, label: "Score", format: num },
      { key: "pred_return", label: "Return", format: pct },
      { key: probabilityKey, label: probabilityLabel, format: pct },
      { key: "confidence", label: "Confidence", format: pct },
    ])}
  </section>`;
}

function renderTomorrow(data) {
  const outlook = data.market_outlook || {};
  const range = data.long_short_range || {};
  const outlookItems = representativesByMarket(outlook.items || []);
  const rangeByMarket = Object.fromEntries(representativesByMarket(range.items || []).map((item) => [item.market, item]));
  const duplicateNotice = (outlook.items || []).length > outlookItems.length
    ? '<p class="muted">동일 시장 복수 전망 중 confidence가 높은 대표값을 표시합니다.</p>'
    : "";
  const cards = outlookItems.map((item) => {
    const exposure = rangeByMarket[item.market] || {};
    return `<section class="market-card">
      <h3>${esc(item.market)}</h3>
      <div class="row"><span>예상 수익률</span><b>${pct(item.expected_return)}</b></div>
      <div class="row"><span>예상 범위</span><b>${pct(item.range_low)} ~ ${pct(item.range_high)}</b></div>
      <div class="row"><span>상승 / 하락</span><b>${pct(item.up_probability)} / ${pct(item.down_probability)}</b></div>
      <div class="row"><span>쇼크 확률</span><b>${pct(item.shock_probability)}</b></div>
      <div class="row"><span>Confidence</span><b>${pct(item.confidence)}</b></div>
      <div class="row"><span>LONG 범위</span><b>${pct(exposure.long_low)} ~ ${pct(exposure.long_high)}</b></div>
      <div class="row"><span>SHORT 범위</span><b>${pct(exposure.short_low)} ~ ${pct(exposure.short_high)}</b></div>
    </section>`;
  }).join("");
  return [
    `<div class="row"><span>Status</span><b>${statusBadge(data.status)}</b></div>`,
    `<div class="row"><span>Target</span><b>${esc(data.next_trading_day || outlook.target_date || "-")}</b></div>`,
    duplicateNotice,
    cards || '<div class="empty">내일 예측 미수집: market_outlook_forecasts 생성이 필요합니다.</div>',
  ].join("");
}

function representativesByMarket(items) {
  const selected = {};
  for (const item of items || []) {
    const market = String(item.market || "-");
    const current = selected[market];
    if (!current || Number(item.confidence || 0) > Number(current.confidence || 0)) {
      selected[market] = item;
    }
  }
  const ordered = [];
  for (const market of ["KOSPI", "KOSDAQ"]) {
    if (selected[market]) ordered.push(selected[market]);
  }
  for (const market of Object.keys(selected).sort()) {
    if (!["KOSPI", "KOSDAQ"].includes(market)) ordered.push(selected[market]);
  }
  return ordered;
}

function renderQuality(quality, validation) {
  const components = quality.components || {};
  const rows = Object.entries(components).map(([key, value]) => `<div class="row"><span>${esc(key)}</span><b>${statusBadge(value)}</b></div>`);
  const counts = validation.counts || {};
  const countRows = Object.entries(counts).map(([key, value]) => `<div class="row"><span>${esc(key)}</span><b>${esc(value)}</b></div>`);
  const messages = [...(validation.messages || []), ...(quality.messages || [])].slice(0, 6).map((message) => `<p class="muted">${esc(message)}</p>`);
  return [...rows, ...countRows, ...messages].join("") || '<div class="empty">품질 메시지 없음: 생성된 데이터가 정상 범위입니다.</div>';
}

function renderXImpact(top20, market) {
  const topItems = top20.items || [];
  const marketItems = market.items || [];
  const messages = [...(top20.messages || []), ...(market.messages || [])];
  if (!topItems.length && !marketItems.length) {
    const message = messages[0] || "X 뉴스 영향도 미생성: X_BEARER_TOKEN 설정 또는 build_x_news_impact_analysis.py 실행 필요";
    return `<div class="empty">${esc(message)}</div>`;
  }
  return [
    `<div class="inline-metrics">
      ${inlineMetric("Top20 영향 종목", topItems.length, shortDate(top20.asof_date))}
      ${inlineMetric("Top20 변경", top20.summary?.top20_changed ?? 0, "편입/제외 변화")}
      ${inlineMetric("최대 확률 변화", pct(top20.summary?.max_probability_delta || 0), "pred_prob_top20")}
      ${inlineMetric("시장전망 변화", marketItems.length, shortDate(market.asof_date))}
    </div>`,
    `<section class="subsection"><h3>Top20 영향도</h3>${table(topItems.slice(0, 20), [
      { key: "name", label: "Name", format: (value, row) => `${esc(value || row.symbol)}<br><span class="muted">${esc(row.symbol)}</span>` },
      { key: "market", label: "Market" },
      { key: "rank_delta", label: "Rank Δ" },
      { key: "pred_prob_delta", label: "Prob Δ", format: pct },
      { key: "pred_return_delta", label: "Return Δ", format: pct },
      { key: "impact_level", label: "Impact", format: impactBadge },
    ])}</section>`,
    `<section class="subsection"><h3>KOSPI/KOSDAQ 범위 영향도</h3>${table(marketItems, [
      { key: "market", label: "Market" },
      { key: "horizon", label: "Horizon" },
      { key: "expected_return_delta", label: "Return Δ", format: pct },
      { key: "range_low_delta", label: "Low Δ", format: pct },
      { key: "range_high_delta", label: "High Δ", format: pct },
      { key: "up_probability_delta", label: "Up Prob Δ", format: pct },
      { key: "shock_probability_delta", label: "Shock Δ", format: pct },
      { key: "impact_level", label: "Impact", format: impactBadge },
    ])}</section>`,
  ].join("");
}

function impactBadge(value) {
  const level = String(value || "low");
  const cls = level === "high" ? "badge risk" : level === "medium" ? "badge warn" : "badge";
  return `<span class="${cls}">${esc(level)}</span>`;
}

function renderNewsSignals(data) {
  const items = data.items || [];
  if (!items.length) return '<div class="empty">뉴스 신호 미수집: build_news_signal_features.py 실행 또는 공식 뉴스 설정이 필요합니다.</div>';
  return items.slice(0, 8).map((item) => `
    <div class="row">
      <span>${esc(item.scope === "market" ? "시장 전체" : item.symbol)}</span>
      <b>부정 ${pct(item.news_negative_attention_score)} · 편향보정 ${pct(item.news_bias_adjusted_sentiment_score)}</b>
    </div>
  `).join("");
}

function renderNews(items) {
  if (!items.length) return '<div class="empty">뉴스 미수집: NAVER_CLIENT_ID/SECRET 또는 공식 RSS 설정 필요</div>';
  return items.slice(0, 12).map((item) => {
    const href = item.link || item.originallink || "#";
    return `<article class="news-item">
      <a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(item.title || "-")}</a>
      <p class="muted">${esc(item.name || item.source_name || item.source || "-")} · ${shortDate(item.pub_date || item.query_date)}</p>
      <p>${esc(item.description || item.summary || "")}</p>
    </article>`;
  }).join("");
}

boot().catch((error) => {
  document.body.innerHTML = `<main><div class="panel"><h1>Dashboard load failed</h1><p>${esc(error.message)}</p></div></main>`;
});
"""


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")


def _read_json_file(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    value = Path(path)
    if not value.exists():
        return {}
    return json.loads(value.read_text(encoding="utf-8"))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_secret_key(key_text):
                output[key_text] = "[redacted]"
            else:
                output[key_text] = _sanitize(item)
        return output
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, float):
        if value != value:
            return None
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("secret", "token", "password", "api_key", "client_secret", "service_key"))


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


if __name__ == "__main__":
    main()
