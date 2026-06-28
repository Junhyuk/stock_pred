from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from roboquant.config import load_config
from roboquant.db import append_dedup_table, connect_database
from roboquant.features.build_feature_matrix import build_feature_matrix
from roboquant.us_sector_linkage import (
    attach_us_sector_features,
    build_us_sector_linkage,
    get_sector_linkage,
    normalize_domestic_sector,
    refresh_us_sector_linkage,
)


def test_domestic_sector_maps_to_us_proxy_groups() -> None:
    assert normalize_domestic_sector("자동차부품") == "auto"
    assert normalize_domestic_sector("반도체") == "semiconductor"
    assert normalize_domestic_sector("바이오") == "healthcare"
    assert normalize_domestic_sector("알수없음") == "broad"


def test_us_sector_linkage_uses_lagged_us_close_without_lookahead(tmp_path) -> None:
    conn = connect_database(tmp_path / "sector_lag.duckdb")
    _seed_sector_fixture(conn)

    frame = build_us_sector_linkage(conn, _config(), asof_date="2026-06-23")
    target = frame[
        frame["trade_date"].astype(str).eq("2026-06-23")
        & frame["domestic_sector"].eq("auto")
    ].iloc[0]

    assert round(float(target["us_sector_return_1d"]), 4) == 0.02
    assert float(target["us_sector_impact_score"]) > 0.5
    assert 0.0 <= float(target["us_sector_impact_score"]) <= 1.0


def test_attach_us_sector_features_and_missing_defaults(tmp_path) -> None:
    conn = connect_database(tmp_path / "sector_attach.duckdb")
    _seed_sector_fixture(conn)
    linkage = build_us_sector_linkage(conn, _config(), asof_date="2026-06-23")
    symbols = conn.execute("SELECT * FROM symbols").fetchdf()
    features = pd.DataFrame(
        [
            {"date": date(2026, 6, 23), "symbol": "005850", "horizon": "2M"},
            {"date": date(2026, 6, 23), "symbol": "999999", "horizon": "2M"},
        ]
    )

    output = attach_us_sector_features(features, linkage, symbols=symbols)

    sl = output[output["symbol"].eq("005850")].iloc[0]
    missing = output[output["symbol"].eq("999999")].iloc[0]
    assert sl["us_sector_return_1d"] == 0.02
    assert 0.0 <= sl["us_sector_impact_score"] <= 1.0
    assert missing["us_sector_impact_score"] != 0.5

    no_linkage = attach_us_sector_features(features, pd.DataFrame(), symbols=symbols)
    assert no_linkage[no_linkage["symbol"].eq("005850")].iloc[0]["us_sector_impact_score"] == 0.5


def test_feature_matrix_contains_us_sector_columns(tmp_path) -> None:
    conn = connect_database(tmp_path / "sector_feature.duckdb")
    _seed_sector_fixture(conn)
    linkage = build_us_sector_linkage(conn, _config(), asof_date="2026-06-23")
    prices = conn.execute("SELECT * FROM prices_daily ORDER BY symbol, date").fetchdf()
    symbols = conn.execute("SELECT * FROM symbols").fetchdf()

    features = build_feature_matrix(
        prices,
        {"2M": 42},
        us_sector_linkage=linkage,
        symbols=symbols,
    )

    latest = features[features["symbol"].eq("005850")].sort_values("date").iloc[-1]
    assert "us_sector_impact_score" in features.columns
    assert latest["us_sector_return_1d"] == 0.02


def test_sector_linkage_api_payload(tmp_path, monkeypatch) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "sector_api.duckdb"
    conn = connect_database(db_path)
    _seed_sector_fixture(conn)
    refresh_us_sector_linkage(conn, _config(), asof_date="2026-06-23")
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)

    payload = app_main.sector_linkage(date="latest", sector="auto")

    assert payload["status"] in {"ready", "partial_ready"}
    assert payload["summary"]["count"] == 1
    assert payload["items"][0]["primary_proxy"] == "DRIV"


def _config() -> dict:
    config = load_config("configs/global_market.yaml")
    config["sector_linkage"] = {
        "sectors": {
            "auto": {
                "aliases": ["자동차", "자동차부품"],
                "primary_proxy": "DRIV",
                "proxies": ["DRIV", "XLY"],
            },
            "semiconductor": {
                "aliases": ["반도체"],
                "primary_proxy": "SOXX",
                "proxies": ["SOXX"],
            },
            "broad": {
                "aliases": ["기타"],
                "primary_proxy": "SPY",
                "proxies": ["SPY"],
            },
        }
    }
    return config


def _seed_sector_fixture(conn) -> None:
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            [
                {"symbol": "005850", "name": "에스엘", "market": "KOSPI", "sector": "자동차부품", "is_active": True},
                {"symbol": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체", "is_active": True},
            ]
        ),
        ["symbol"],
    )
    price_rows = []
    current = date(2026, 4, 1)
    index = 0
    while current <= date(2026, 6, 23):
        if current.weekday() < 5:
            price_rows.extend(
                [
                    {
                        "date": current,
                        "symbol": "005850",
                        "close": 100.0 + index * 0.4,
                        "volume": 1000.0,
                        "source": "fixture",
                        "collected_at": datetime(2026, 6, 23, 16),
                    },
                    {
                        "date": current,
                        "symbol": "005930",
                        "close": 200.0 + index * 0.8,
                        "volume": 1000.0,
                        "source": "fixture",
                        "collected_at": datetime(2026, 6, 23, 16),
                    },
                ]
            )
            index += 1
        current += timedelta(days=1)
    append_dedup_table(conn, "prices_daily", pd.DataFrame(price_rows), ["date", "symbol", "source"])

    global_rows = []
    current = date(2026, 4, 1)
    close = {"DRIV": 100.0, "XLY": 200.0, "SOXX": 300.0, "SPY": 400.0}
    while current <= date(2026, 6, 23):
        if current.weekday() < 5:
            for symbol in close:
                if current == date(2026, 6, 22) and symbol in {"DRIV", "XLY"}:
                    close[symbol] *= 1.02
                elif current == date(2026, 6, 23) and symbol in {"DRIV", "XLY"}:
                    close[symbol] *= 1.50
                else:
                    close[symbol] *= 1.001
                global_rows.append(
                    {
                        "trade_date": current,
                        "symbol": symbol,
                        "market_group": "fixture",
                        "display_name": symbol,
                        "close": close[symbol],
                        "return_1d": 0.50 if current == date(2026, 6, 23) and symbol in {"DRIV", "XLY"} else (0.02 if current == date(2026, 6, 22) and symbol in {"DRIV", "XLY"} else 0.001),
                        "return_5d": 0.04 if current == date(2026, 6, 22) and symbol in {"DRIV", "XLY"} else 0.005,
                        "source_name": "fixture",
                        "source_timestamp": datetime.combine(current, datetime.min.time()),
                    }
                )
        current += timedelta(days=1)
    append_dedup_table(conn, "global_market_daily", pd.DataFrame(global_rows), ["trade_date", "symbol", "source_name"])
