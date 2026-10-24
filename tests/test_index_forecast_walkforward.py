from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from roboquant.db import connect_database
from roboquant.index_forecast.dataset import active_feature_groups, build_index_forecast_dataset
from roboquant.index_forecast.export import export_duckdb_history_to_sqlite
from roboquant.index_forecast.ingest import ingest_kospi100_index, ingest_official_index
from roboquant.index_forecast.pipeline import run_index_forecast_research
from roboquant.index_forecast.sqlite_store import connect_index_forecast, table_count, upsert_frame
from roboquant.index_forecast.walkforward import forecast_latest, run_index_forecast_walkforward


def test_sqlite_schema_round_trip(tmp_path) -> None:
    conn = connect_index_forecast(tmp_path / "index.sqlite")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "index_eod",
        "macro_series",
        "investor_flow",
        "feature_snapshot",
        "model_registry",
        "prediction",
        "realized_outcome",
    }.issubset(tables)
    wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(wal).lower() == "wal"
    conn.close()


def test_duckdb_export_copies_kospi_and_overnight(tmp_path) -> None:
    duck = connect_database(tmp_path / "src.duckdb")
    dates = pd.bdate_range("2024-01-02", periods=8)
    duck.executemany(
        "INSERT INTO benchmark_daily (date, benchmark, close, volume, trading_value) VALUES (?, ?, ?, ?, ?)",
        [(item.date(), "KOSPI", 2600 + index, 1_000_000, 2_000_000_000) for index, item in enumerate(dates)]
        + [(item.date(), "KOSDAQ", 800 + index, 1_000_000, 1_000_000_000) for index, item in enumerate(dates)],
    )
    duck.executemany(
        """
        INSERT INTO global_market_daily (
          trade_date, symbol, market_group, display_name, close, return_1d, source_name
        ) VALUES (?, ?, 'US', ?, 100, ?, 'test')
        """,
        [(item.date(), "^IXIC", "Nasdaq", 0.01 * (index + 1)) for index, item in enumerate(dates)],
    )
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    report = export_duckdb_history_to_sqlite(duck, sqlite)
    assert report["index_eod_rows"] == 16
    assert report["kospi_status"] == "ready"
    assert report["kosdaq_status"] == "ready"
    assert report["kospi100_status"] == "not_ready"
    assert table_count(sqlite, "macro_series") == 8
    duck.close()
    sqlite.close()


def test_kospi100_ingest_uses_official_index_and_not_constituents(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    duck = connect_database(tmp_path / "src.duckdb")

    class _Api:
        def get_index_ohlcv_by_date(self, start, end, code):
            assert code == "1034"
            return pd.DataFrame(
                {"시가": [1000], "고가": [1010], "저가": [990], "종가": [1005], "거래량": [10], "거래대금": [1000]},
                index=pd.Index([pd.Timestamp("2024-01-02")], name="날짜"),
            )

    ready = ingest_kospi100_index(
        start_date="2024-01-02",
        end_date="2024-01-02",
        duckdb_conn=duck,
        sqlite_conn=sqlite,
        stock_api=_Api(),
    )
    assert ready["status"] == "ready"
    assert ready["rows"] == 1
    assert duck.execute("SELECT benchmark FROM benchmark_daily").fetchone()[0] == "KOSPI100"

    class _Empty:
        def get_index_ohlcv_by_date(self, start, end, code):
            return pd.DataFrame()

    empty = ingest_kospi100_index(
        start_date="2024-01-02",
        sqlite_conn=sqlite,
        stock_api=_Empty(),
    )
    assert empty["status"] == "not_ready"
    assert "constituent" in empty["reason"]
    duck.close()
    sqlite.close()


def test_features_do_not_use_future_closes(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=40)
    _seed_prices(sqlite, dates, close=2600 + np.arange(len(dates)))
    before = build_index_forecast_dataset(sqlite, index_code="KOSPI")
    asof = dates[20].date()
    before_row = before[before["asof_date"] == asof].iloc[0]
    sqlite.execute("UPDATE index_eod SET close = close * 2 WHERE session_date > ?", [asof.isoformat()])
    sqlite.commit()
    after = build_index_forecast_dataset(sqlite, index_code="KOSPI")
    after_row = after[after["asof_date"] == asof].iloc[0]
    assert after_row["return_1d"] == before_row["return_1d"]
    assert after_row["return_5d"] == before_row["return_5d"]
    assert after_row["ma_gap_20d"] == before_row["ma_gap_20d"]
    sqlite.close()


def test_ridge_overnight_beats_naive_and_stays_shadow(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=90)
    nasdaq = 0.012 * np.sin(np.arange(len(dates)) / 3.0)
    close = [2600.0]
    for index in range(1, len(dates)):
        signal = float(nasdaq[index - 2]) if index >= 2 else 0.0
        close.append(close[-1] * (1.0 + 0.55 * signal))
    _seed_prices(sqlite, dates, close=close)
    _seed_macro(sqlite, dates, nasdaq)
    report = run_index_forecast_walkforward(
        sqlite,
        index_code="KOSPI",
        initial_train_days=30,
        purge_days=1,
        retrain_step_days=8,
        calibration_window=12,
        min_oos_rows=12,
        persist=True,
        model_types=("naive", "previous_direction", "ridge"),
    )
    assert report["production_weight"] == 0.0
    overnight = report["groups"]["price_overnight"]["models"]
    assert overnight["ridge"]["metrics"]["mae"] < overnight["naive"]["metrics"]["mae"]
    stored = sqlite.execute("SELECT production_weight FROM prediction LIMIT 1").fetchone()
    assert stored is not None
    assert stored[0] == 0.0
    immutable = sqlite.execute("SELECT COUNT(*) FROM prediction").fetchone()[0]
    run_index_forecast_walkforward(
        sqlite,
        index_code="KOSPI",
        initial_train_days=30,
        purge_days=1,
        retrain_step_days=8,
        calibration_window=12,
        min_oos_rows=12,
        persist=True,
        model_types=("naive", "previous_direction", "ridge"),
    )
    assert sqlite.execute("SELECT COUNT(*) FROM prediction").fetchone()[0] == immutable
    sqlite.close()


def test_foreign_flow_group_is_used_only_when_cash_history_exists(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=40)
    _seed_prices(sqlite, dates, close=2600 + np.arange(len(dates)))
    upsert_frame(
        sqlite,
        "investor_flow",
        pd.DataFrame(
            {
                "market": "KOSPI",
                "session_date": [item.date().isoformat() for item in dates],
                "investor_type": "외국인",
                "buy_value": 8.0e11,
                "sell_value": 1.0e11,
                "net_value": 7.0e11,
                "source": "test",
                "published_at": [item.date().isoformat() for item in dates],
                "ingested_at": datetime(2024, 1, 2).isoformat(),
            }
        ),
        ["market", "session_date", "investor_type"],
    )
    frame = build_index_forecast_dataset(sqlite, index_code="KOSPI")
    groups = active_feature_groups(frame)
    assert "price_flow" in groups
    assert "foreign_signed_pressure" in groups["price_flow"]
    sqlite.close()


def test_missing_kospi100_is_not_ready(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    report = run_index_forecast_walkforward(sqlite, index_code="KOSPI100", persist=False)
    assert report["status"] == "not_ready"
    assert report["production_weight"] == 0.0
    sqlite.close()


def test_kosdaq100_is_not_reconstructed_or_replaced_with_kosdaq150(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")

    class _Forbidden:
        def get_index_ohlcv_by_date(self, start, end, code):
            raise AssertionError(f"must not fetch substitute index {code}")

    report = ingest_official_index(
        "KOSDAQ100",
        start_date="2024-01-02",
        sqlite_conn=sqlite,
        stock_api=_Forbidden(),
    )
    assert report["status"] == "not_ready"
    assert "kosdaq150" in report["reason"]
    assert table_count(sqlite, "index_eod") == 0
    sqlite.close()


def test_five_day_label_uses_t_plus_five_close(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=40)
    close = 2600 + np.arange(len(dates), dtype=float)
    _seed_prices(sqlite, dates, close=close)
    frame = build_index_forecast_dataset(sqlite, index_code="KOSPI", horizon_days=5)
    asof = dates[10].date()
    row = frame[frame["asof_date"] == asof].iloc[0]
    expected = close[15] / close[10] - 1.0
    assert abs(float(row["label_return"]) - expected) < 1e-12
    one = build_index_forecast_dataset(sqlite, index_code="KOSPI", horizon_days=1)
    assert abs(float(one[one["asof_date"] == asof].iloc[0]["label_return"]) - (close[11] / close[10] - 1.0)) < 1e-12
    sqlite.close()


def test_eval_start_scores_only_the_requested_window(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=80)
    _seed_prices(sqlite, dates, close=2600 + np.arange(len(dates)))
    report = run_index_forecast_walkforward(
        sqlite,
        index_code="KOSPI",
        initial_train_days=20,
        purge_days=1,
        retrain_step_days=8,
        calibration_window=10,
        min_oos_rows=5,
        persist=False,
        model_types=("naive",),
        eval_start_date=dates[50].date(),
    )
    n = report["groups"]["price"]["models"]["naive"]["metrics"]["n"]
    assert n < 30
    assert n >= 5
    sqlite.close()


def test_live_forecast_uses_last_unlabeled_session(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=40)
    _seed_prices(sqlite, dates, close=2600 + np.arange(len(dates)))
    live = forecast_latest(
        sqlite,
        index_code="KOSPI",
        horizon_days=1,
        model_type="naive",
        feature_columns=["return_1d", "return_5d", "return_20d"],
        persist=False,
    )
    assert live["status"] == "ready"
    assert live["asof_date"] == dates[-1].date().isoformat()
    assert live["production_weight"] == 0.0
    sqlite.close()


def test_research_pipeline_writes_pages_json(tmp_path) -> None:
    sqlite = connect_index_forecast(tmp_path / "index.sqlite")
    dates = pd.bdate_range("2024-01-02", periods=70)
    nasdaq = 0.01 * np.sin(np.arange(len(dates)) / 3.0)
    close = [2600.0]
    for index in range(1, len(dates)):
        signal = float(nasdaq[index - 2]) if index >= 2 else 0.0
        close.append(close[-1] * (1.0 + 0.45 * signal))
    _seed_prices(sqlite, dates, close=close)
    _seed_macro(sqlite, dates, nasdaq)
    pages = tmp_path / "docs"
    result = run_index_forecast_research(
        sqlite,
        indexes=("KOSPI", "KOSDAQ100"),
        horizon_days=(1, 5),
        eval_years=3,
        skip_export=True,
        persist=False,
        model_types=("naive", "previous_direction", "ridge"),
        initial_train_days=25,
        retrain_step_days=10,
        min_oos_rows=8,
        pages_dir=pages,
    )
    assert result["production_weight"] == 0.0
    assert (pages / "data" / "index_forecast.json").exists()
    kospi_1d = result["report"]["indexes"][0]["horizons"]["1D"]
    assert kospi_1d["champion"]["model_type"] in {"naive", "previous_direction", "ridge"}
    assert result["report"]["indexes"][3]["index_code"] == "KOSDAQ100"
    assert result["report"]["indexes"][3]["status"] == "not_ready"
    sqlite.close()


def _seed_prices(conn, dates: pd.DatetimeIndex, close) -> None:
    upsert_frame(
        conn,
        "index_eod",
        pd.DataFrame(
            {
                "index_code": "KOSPI",
                "session_date": [item.date().isoformat() for item in dates],
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000_000,
                "turnover": 2_000_000_000,
                "source": "test",
                "published_at": [item.date().isoformat() for item in dates],
                "ingested_at": datetime(2024, 1, 2).isoformat(),
            }
        ),
        ["index_code", "session_date"],
    )


def _seed_macro(conn, dates: pd.DatetimeIndex, nasdaq: np.ndarray) -> None:
    upsert_frame(
        conn,
        "macro_series",
        pd.DataFrame(
            {
                "series_code": "US_NASDAQ",
                "observation_date": [item.date().isoformat() for item in dates],
                "value": nasdaq,
                "published_at": [item.date().isoformat() for item in dates],
                "revision_no": 0,
                "source": "test",
                "ingested_at": datetime(2024, 1, 2).isoformat(),
            }
        ),
        ["series_code", "observation_date", "revision_no"],
    )
