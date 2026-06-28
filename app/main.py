from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.pages import (
    backtest_html,
    dashboard_html,
    focus_stock_demo_html,
    four_stock_demo_html,
    long_short_html,
    market_up_down_html,
    stock_html,
    today_market_html,
    tomorrow_market_html,
    top20_upside_html,
    top50_universe_html,
    two_stock_demo_html,
)
from roboquant.config import get_database_path, load_config
from roboquant.dashboard.dashboard_service import (
    get_backtest_by_model,
    get_backtest_summary,
    get_cluster_members,
    get_clusters,
    get_current_market_regime,
    get_focus_stock,
    get_focus_stocks_demo,
    get_four_stock_demo,
    get_latest_dashboard_snapshot,
    get_latest_global_markets,
    get_latest_news,
    get_koru_linkage,
    get_market_move_explanations,
    get_market_outlook,
    get_model_accuracy,
    get_prediction_history,
    get_sector_linkage,
    get_sector_backtest,
    get_stock_backtest,
    get_stock_cluster,
    get_stock_detail,
    get_today_market_snapshot,
    get_tomorrow_market_snapshot,
    get_top20_backtest,
    get_top20_recommendations,
    get_top20_upside_recommendations,
    get_top50_universe,
    get_two_stock_demo,
)
from roboquant.dashboard.gatekeeper_service import run_model_gatekeeper
from roboquant.dashboard.long_short_service import (
    get_latest_long_short,
    get_long_short_backtest,
)
from roboquant.dashboard.market_up_down_service import get_latest_market_up_down
from roboquant.dashboard.price_gap_service import build_prediction_price_gap
from roboquant.dashboard.price_forecast_service import get_top20_price_forecast
from roboquant.db import connect_database

CONFIG = load_config(ROOT / "configs" / "poc.yaml")
TODAY_CONFIG = load_config(ROOT / "configs" / "today_update.yaml")

app = FastAPI(title="AI Robo Quant API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _conn():
    return connect_database(get_database_path(CONFIG), read_only=True, initialize_schema=False)


def _write_conn():
    return connect_database(get_database_path(CONFIG))


@app.get("/")
def root():
    return dashboard_html()


@app.get("/dashboard")
def dashboard_page():
    return dashboard_html()


@app.get("/backtest")
def backtest_page():
    return backtest_html()


@app.get("/demo/two-stocks")
def two_stock_demo_page():
    return two_stock_demo_html()


@app.get("/demo/focus-stocks")
def focus_stock_demo_page():
    return focus_stock_demo_html()


@app.get("/demo/four-stocks")
def four_stock_demo_page():
    return four_stock_demo_html()


@app.get("/demo/today")
def today_market_page():
    return today_market_html()


@app.get("/demo/tomorrow")
def tomorrow_market_page():
    return tomorrow_market_html()


@app.get("/recommendations/top20-upside")
def top20_upside_page():
    return top20_upside_html()


@app.get("/recommendations/long-short")
def long_short_page():
    return long_short_html()


@app.get("/recommendations/up-down")
def market_up_down_page():
    return market_up_down_html()


@app.get("/universe/top50")
def top50_universe_page():
    return top50_universe_html()


@app.get("/stock/{symbol}")
def stock_page(symbol: str):
    return stock_html(symbol)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard/snapshot")
def dashboard_snapshot():
    conn = _conn()
    try:
        return get_latest_dashboard_snapshot(conn)
    finally:
        conn.close()


@app.get("/api/demo/two-stocks")
def demo_two_stocks(horizon: str = "3M"):
    conn = _conn()
    try:
        return get_two_stock_demo(conn, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/demo/focus-stocks")
def demo_focus_stocks(horizon: str = "3M"):
    conn = _conn()
    try:
        return get_focus_stocks_demo(conn, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/demo/four-stocks")
def demo_four_stocks(horizon: str = "3M"):
    conn = _conn()
    try:
        return get_four_stock_demo(conn, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/market-regime/current")
def market_regime_current():
    conn = _conn()
    try:
        return get_current_market_regime(conn)
    finally:
        conn.close()


@app.get("/api/global-markets/latest")
def global_markets_latest():
    conn = _conn()
    try:
        return get_latest_global_markets(conn)
    finally:
        conn.close()


@app.get("/api/today/update-snapshot")
def today_update_snapshot():
    conn = _conn()
    try:
        return get_today_market_snapshot(conn, TODAY_CONFIG)
    finally:
        conn.close()


@app.get("/api/tomorrow/update-snapshot")
def tomorrow_update_snapshot():
    conn = _conn()
    try:
        return get_tomorrow_market_snapshot(conn, TODAY_CONFIG)
    finally:
        conn.close()


@app.get("/api/news/latest")
def news_latest(symbol: str | None = None, limit: int = 10):
    conn = _conn()
    try:
        return {"items": get_latest_news(conn, symbol=symbol, limit=limit)}
    finally:
        conn.close()


@app.get("/api/market-move/explanations")
def market_move_explanations(date: str = "latest", scope: str | None = None, limit: int = 100):
    conn = _conn()
    try:
        return get_market_move_explanations(conn, date=date, scope=scope, limit=limit, config=TODAY_CONFIG)
    finally:
        conn.close()


@app.get("/api/market-outlook")
def market_outlook(date: str = "latest", horizon: str = "all", market: str = "all", limit: int = 20):
    conn = _conn()
    try:
        return get_market_outlook(
            conn,
            date=date,
            horizon=horizon,
            market=market,
            limit=limit,
            config=TODAY_CONFIG,
        )
    finally:
        conn.close()


@app.get("/api/sector-linkage")
def sector_linkage(date: str = "latest", sector: str = "all", limit: int = 50):
    conn = _conn()
    try:
        return get_sector_linkage(conn, date=date, sector=sector, limit=limit, config=TODAY_CONFIG)
    finally:
        conn.close()


@app.get("/api/koru/linkage")
def koru_linkage(date: str = "latest"):
    conn = _conn()
    try:
        return get_koru_linkage(conn, date=date)
    finally:
        conn.close()


@app.get("/api/focus/{symbol}")
def focus_stock(symbol: str, horizon: str = "3M"):
    conn = _conn()
    try:
        return get_focus_stock(conn, symbol=symbol, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/clusters")
def clusters(horizon: str = "3M"):
    conn = _conn()
    try:
        return {"items": get_clusters(conn, horizon=horizon)}
    finally:
        conn.close()


@app.get("/api/clusters/{cluster_id}")
def cluster_members(cluster_id: int, horizon: str = "3M"):
    conn = _conn()
    try:
        return get_cluster_members(conn, cluster_id=cluster_id, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/backtest/summary")
def backtest_summary(horizon: str | int = Query("60")):
    conn = _conn()
    try:
        return get_backtest_summary(conn, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/backtest/by-model")
def backtest_by_model(
    model: str,
    version: str | None = None,
    horizon: str | int = Query("60"),
):
    conn = _conn()
    try:
        return {"items": get_backtest_by_model(conn, model=model, version=version, horizon=horizon)}
    finally:
        conn.close()


@app.get("/api/backtest/by-stock")
def backtest_by_stock(ticker: str, horizon: str | int | None = Query(None)):
    conn = _conn()
    try:
        return {"items": get_stock_backtest(conn, symbol=ticker, horizon=horizon)}
    finally:
        conn.close()


@app.get("/api/backtest/top20")
def backtest_top20(horizon: str | int = Query("60")):
    conn = _conn()
    try:
        return {"items": get_top20_backtest(conn, horizon=horizon)}
    finally:
        conn.close()


@app.get("/api/backtest/sector")
def backtest_sector(sector: str, horizon: str | int = Query("60")):
    conn = _conn()
    try:
        return get_sector_backtest(conn, sector=sector, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/backtest/price-gap")
def backtest_price_gap(
    lookback_days: int = 30,
    target_days: int = 30,
    horizon: str = "3M",
    symbols: str | None = None,
    include_pending: bool = True,
    limit: int = 5000,
):
    conn = _conn()
    try:
        return build_prediction_price_gap(
            conn,
            lookback_days=lookback_days,
            target_days=target_days,
            horizon=horizon,
            symbols=_parse_symbol_list(symbols),
            include_pending=include_pending,
            limit=limit,
        )
    finally:
        conn.close()


@app.get("/api/models/accuracy")
def models_accuracy():
    conn = _conn()
    try:
        return {"items": get_model_accuracy(conn)}
    finally:
        conn.close()


@app.get("/api/models/gate-status")
def models_gate_status():
    conn = _conn()
    try:
        return {"items": get_model_accuracy(conn)}
    finally:
        conn.close()


@app.post("/api/models/run-gatekeeper")
def models_run_gatekeeper(baseline_model: str = "lightgbm", min_sample_count: int = 200):
    conn = _write_conn()
    try:
        decisions = run_model_gatekeeper(
            conn,
            baseline_model=baseline_model,
            min_sample_count=min_sample_count,
        )
        return {"items": decisions.to_dict(orient="records")}
    finally:
        conn.close()


@app.get("/api/recommendations/top20")
def recommendations_top20(horizon: str = "3M"):
    conn = _conn()
    try:
        return {"items": get_top20_recommendations(conn, horizon=horizon)}
    finally:
        conn.close()


@app.get("/api/recommendations/top20-upside")
def recommendations_top20_upside(horizon: str = "3M", limit: int = Query(20, ge=1, le=100)):
    conn = _conn()
    try:
        return get_top20_upside_recommendations(conn, horizon=horizon, limit=limit)
    finally:
        conn.close()


@app.get("/api/recommendations/top20-price-forecast")
def recommendations_top20_price_forecast(
    horizons: str = "3M,6M,9M,1Y",
    limit: int = Query(20, ge=1, le=100),
    base_horizon: str = "3M",
    date: str | None = None,
):
    conn = _conn()
    try:
        return get_top20_price_forecast(
            conn,
            horizons=horizons,
            limit=limit,
            base_horizon=base_horizon,
            asof_date=date,
        )
    finally:
        conn.close()


@app.get("/api/long-short/latest")
def long_short_latest(horizon: str = "2M", market: str | None = None):
    conn = _conn()
    try:
        return get_latest_long_short(conn, horizon=horizon, market=market)
    finally:
        conn.close()


@app.get("/api/long-short/backtest")
def long_short_backtest(horizon: str = "6M", market: str | None = None):
    conn = _conn()
    try:
        return get_long_short_backtest(conn, horizon=horizon, market=market)
    finally:
        conn.close()


@app.get("/api/recommendations/up-down")
def market_up_down_latest(horizon: str = "2M", market: str | None = None):
    conn = _conn()
    try:
        return get_latest_market_up_down(conn, horizon=horizon, market=market)
    finally:
        conn.close()


@app.get("/api/universe/top50")
def universe_top50(horizon: str = "3M", universe_rule: str = "prediction_top_market_cap"):
    conn = _conn()
    try:
        return get_top50_universe(conn, horizon=horizon, universe_rule=universe_rule)
    finally:
        conn.close()


@app.get("/api/recommendations/core-portfolio")
def recommendations_core_portfolio():
    conn = _conn()
    try:
        return {"items": get_latest_dashboard_snapshot(conn).get("core_portfolio", [])}
    finally:
        conn.close()


@app.get("/api/recommendations/sector-portfolio")
def recommendations_sector_portfolio():
    conn = _conn()
    try:
        return get_latest_dashboard_snapshot(conn).get("theme_data", {})
    finally:
        conn.close()


@app.get("/api/recommendations/upside-ranking")
def recommendations_upside_ranking():
    conn = _conn()
    try:
        return {"items": get_latest_dashboard_snapshot(conn).get("upside_ranking", [])}
    finally:
        conn.close()


@app.get("/api/recommendations/position-summary")
def recommendations_position_summary():
    conn = _conn()
    try:
        return get_latest_dashboard_snapshot(conn).get("position_summary", {})
    finally:
        conn.close()


@app.get("/api/stocks/{symbol}")
def stock_detail(symbol: str, horizon: str = "3M"):
    conn = _conn()
    try:
        return get_stock_detail(conn, symbol=symbol, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/stocks/{symbol}/chart")
def stock_chart(symbol: str):
    conn = _conn()
    try:
        return {"items": get_stock_detail(conn, symbol=symbol).get("chart", [])}
    finally:
        conn.close()


@app.get("/api/stocks/{symbol}/prediction-history")
def stock_prediction_history(symbol: str, horizon: str | int | None = Query(None)):
    conn = _conn()
    try:
        return {"items": get_prediction_history(conn, symbol=symbol, horizon=horizon)}
    finally:
        conn.close()


@app.get("/api/stocks/{symbol}/cluster")
def stock_cluster(symbol: str, horizon: str = "3M"):
    conn = _conn()
    try:
        return get_stock_cluster(conn, symbol=symbol, horizon=horizon)
    finally:
        conn.close()


@app.get("/api/stocks/{symbol}/reports")
def stock_reports(symbol: str):
    conn = _conn()
    try:
        return {"items": get_stock_detail(conn, symbol=symbol).get("analyst_reports", [])}
    finally:
        conn.close()


@app.get("/api/stocks/{symbol}/backtest")
def stock_backtest(symbol: str):
    conn = _conn()
    try:
        return {"items": get_stock_detail(conn, symbol=symbol).get("backtest", [])}
    finally:
        conn.close()


def _parse_symbol_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().zfill(6) for item in value.split(",") if item.strip()]
