from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from roboquant.data.freshness import KST, expected_latest_trading_day, price_freshness_report
from roboquant.db import append_dedup_table, connect_database

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_latest_market_impact_retrain.py"
SPEC = importlib.util.spec_from_file_location("run_latest_market_impact_retrain", SCRIPT_PATH)
assert SPEC is not None
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def test_latest_market_impact_runner_dry_run_order() -> None:
    steps = runner.build_steps(_args(), include_training=True)
    names = [step.name for step in steps]

    assert names[:3] == ["refresh_universe", "collect_prediction_universe_prices", "collect_market_indices"]
    assert "collect_market_metrics" in names
    assert "collect_investor_flows" in names
    assert "global_market_regime" in names
    assert "collect_market_news" in names
    assert "collect_naver_news_top50" in names
    assert "collect_telegram_signals" in names
    assert "collect_x_market_news" in names
    assert "build_news_signal_features" in names
    assert "collect_market_credit_balance" in names
    assert names.index("collect_naver_news_top50") < names.index("collect_telegram_signals")
    assert names.index("collect_telegram_signals") < names.index("collect_x_market_news")
    assert names.index("collect_x_market_news") < names.index("build_news_signal_features")
    assert names.index("build_news_signal_features") < names.index("collect_market_credit_balance")
    assert names.index("collect_market_credit_balance") < names.index("freshness_check")
    assert names.index("freshness_check") < names.index("build_us_sector_linkage")
    assert names.index("build_us_sector_linkage") < names.index("build_feature_matrix")
    assert names.index("freshness_check") < names.index("build_koru_korea_linkage")
    assert names.index("build_koru_korea_linkage") < names.index("build_feature_matrix")
    assert "train_2M" in names
    assert "train_3M" in names
    assert "train_6M" in names
    assert "train_9M" in names
    assert "train_1Y" in names
    assert names.index("koru_weight_gate") < names.index("generate_recommendations")
    assert names.index("generate_market_up_down") < names.index("build_market_outlook_features")
    assert names.index("build_market_outlook_features") < names.index("train_market_outlook")
    assert names.index("train_market_outlook") < names.index("generate_market_outlook")
    assert names.index("generate_market_outlook") < names.index("build_x_news_impact_analysis")
    assert names.index("build_x_news_impact_analysis") < names.index("market_move_explanations")
    assert names.index("market_move_explanations") < names.index("today_context_refresh")
    assert "prediction_backtest_63" in names
    assert "prediction_backtest_126" in names
    assert "prediction_backtest_189" in names
    assert "prediction_backtest_252" in names
    assert names[-3:] == ["dashboard_snapshot", "market_move_explanations", "today_context_refresh"]


def test_latest_market_impact_runner_partial_plan_skips_training() -> None:
    steps = runner.build_steps(_args(), include_training=False)
    names = [step.name for step in steps]

    assert "freshness_check" in names
    assert not any(name.startswith("train_") for name in names)
    assert names[-2:] == ["market_move_explanations_stale_snapshot", "today_context_refresh"]


def test_price_freshness_report_marks_stale_prices(tmp_path) -> None:
    conn = connect_database(tmp_path / "freshness.duckdb")
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame(
            [
                {
                    "date": date(2026, 6, 19),
                    "symbol": "005930",
                    "close": 100.0,
                    "volume": 1000.0,
                    "source": "fixture",
                    "collected_at": datetime(2026, 6, 19, 15),
                }
            ]
        ),
        ["date", "symbol", "source"],
    )

    report = price_freshness_report(conn, expected_date=date(2026, 6, 23))

    assert report.status == "partial_ready"
    assert report.stale is True
    assert report.latest_date == date(2026, 6, 19)
    assert "최신 학습 미완료" in report.messages[0]


def test_expected_latest_trading_day_uses_completed_korean_session() -> None:
    before_daily_bar_ready = datetime(2026, 6, 24, 0, 40, tzinfo=KST)
    after_daily_bar_ready = datetime(2026, 6, 24, 19, 0, tzinfo=KST)

    assert expected_latest_trading_day(now=before_daily_bar_ready) == date(2026, 6, 23)
    assert expected_latest_trading_day(now=after_daily_bar_ready) == date(2026, 6, 24)


def _args() -> Namespace:
    return Namespace(
        config="configs/top50_normal.yaml",
        universe_config="configs/universe_top50.yaml",
        provider="fdr_poc",
        target_date="2026-06-23",
        flow_lookback_days=90,
        restart_web=False,
        skip_refresh=False,
        skip_collect=False,
        skip_enrichment=False,
        skip_global=False,
        skip_news=False,
    )
