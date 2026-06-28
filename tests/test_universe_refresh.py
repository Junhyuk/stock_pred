from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from roboquant.db import connect_database
from roboquant.universe.providers.base import MarketCapItem, MarketDataProvider
from roboquant.universe.refresh import (
    RefreshSettings,
    UniverseRefreshError,
    refresh_prediction_universe,
)
from roboquant.universe.seed_loader import seed_prediction_universe

SNAPSHOT_DATE = date(2026, 6, 9)
UNIVERSE_RULE = "prediction_top_market_cap"


class FakeProvider(MarketDataProvider):
    provider_name = "fake_provider"

    def __init__(
        self,
        rankings: dict[str, list[MarketCapItem]],
        *,
        price_days: dict[str, int] | None = None,
        ranking_error: Exception | None = None,
    ) -> None:
        self.rankings = rankings
        self.price_days = price_days or {}
        self.ranking_error = ranking_error

    def get_market_cap_ranking(
        self,
        trade_date: date,
        market: str,
        fetch_limit: int,
    ) -> list[MarketCapItem]:
        del trade_date
        if self.ranking_error is not None:
            raise self.ranking_error
        return self.rankings.get(market, [])[:fetch_limit]

    def get_price_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        del start_date
        days = self.price_days.get(str(symbol).zfill(6), 130)
        if days <= 0:
            return _price_frame([], str(symbol).zfill(6))
        dates = pd.date_range(end=end_date, periods=days, freq="D").date
        return _price_frame(dates, str(symbol).zfill(6))


def test_refresh_commits_complete_universe_and_filters_exclusions(tmp_path) -> None:
    conn = connect_database(tmp_path / "refresh_success.duckdb")
    provider = FakeProvider(
        {
            "KOSPI": [
                _item("005935", "삼성전자우", "KOSPI", 1, security_type="PREFERRED"),
                _item("069500", "KODEX 200", "KOSPI", 2, security_type="ETF"),
                _item("109999", "신규상장", "KOSPI", 3),
                _item("005930", "삼성전자", "KOSPI", 4),
                *[_item(f"10{i:04d}", f"KOSPI {i}", "KOSPI", i + 4) for i in range(1, 31)],
            ],
            "KOSDAQ": [
                _item("299999", "스팩", "KOSDAQ", 1, security_type="SPAC"),
                *[_item(f"20{i:04d}", f"KOSDAQ {i}", "KOSDAQ", i + 1) for i in range(1, 22)],
            ],
        },
        price_days={"109999": 40},
    )

    result = refresh_prediction_universe(
        conn,
        provider,
        snapshot_date=SNAPSHOT_DATE,
        universe_rule=UNIVERSE_RULE,
    )

    assert result["prediction_count"] == 50
    assert conn.execute("SELECT COUNT(*) FROM current_prediction_universe").fetchone()[0] == 50
    assert conn.execute(
        """
        SELECT market, COUNT(*)
        FROM current_prediction_universe
        GROUP BY market
        ORDER BY market
        """
    ).fetchall() == [("KOSDAQ", 20), ("KOSPI", 30)]
    assert conn.execute(
        """
        SELECT symbol, exclusion_reason
        FROM raw_market_cap_snapshot
        WHERE snapshot_date = ? AND universe_rule = ? AND symbol IN ('005935', '069500', '109999', '299999')
        ORDER BY symbol
        """,
        [SNAPSHOT_DATE, UNIVERSE_RULE],
    ).fetchall() == [
        ("005935", "excluded security_type=PREFERRED"),
        ("069500", "excluded security_type=ETF"),
        ("109999", "excluded insufficient_listing_history=40"),
        ("299999", "excluded security_type=SPAC"),
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM prediction_universe_snapshot WHERE symbol IN ('005935', '069500', '109999', '299999')"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT status, kospi_selected_count, kosdaq_selected_count FROM universe_refresh_runs"
    ).fetchone() == ("ready", 30, 20)


def test_refresh_rolls_back_shortfall_and_preserves_previous_ready_snapshot(tmp_path) -> None:
    conn = connect_database(tmp_path / "refresh_shortfall.duckdb")
    seed_prediction_universe(
        conn,
        snapshot_date=date(2026, 6, 5),
        universe_rule=UNIVERSE_RULE,
        provider="v8_seed_document",
    )
    provider = FakeProvider(
        {
            "KOSPI": [_item(f"30{i:04d}", f"KOSPI {i}", "KOSPI", i) for i in range(1, 11)],
            "KOSDAQ": [_item(f"40{i:04d}", f"KOSDAQ {i}", "KOSDAQ", i) for i in range(1, 11)],
        }
    )

    with pytest.raises(UniverseRefreshError, match="KOSPI selected 10/30"):
        refresh_prediction_universe(
            conn,
            provider,
            snapshot_date=SNAPSHOT_DATE,
            universe_rule=UNIVERSE_RULE,
        )

    assert conn.execute(
        "SELECT DISTINCT snapshot_date FROM current_prediction_universe"
    ).fetchall() == [(date(2026, 6, 5),)]
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM raw_market_cap_snapshot
        WHERE snapshot_date = ?
        """,
        [SNAPSHOT_DATE],
    ).fetchone()[0] == 0
    assert conn.execute(
        """
        SELECT status, error_message
        FROM universe_refresh_runs
        WHERE snapshot_date = ?
        """,
        [SNAPSHOT_DATE],
    ).fetchone()[0] == "failed"


def test_refresh_provider_error_records_failed_run_without_replacing_current(tmp_path) -> None:
    conn = connect_database(tmp_path / "refresh_provider_error.duckdb")
    seed_prediction_universe(
        conn,
        snapshot_date=date(2026, 6, 5),
        universe_rule=UNIVERSE_RULE,
        provider="v8_seed_document",
    )
    provider = FakeProvider({}, ranking_error=RuntimeError("provider unavailable"))

    with pytest.raises(UniverseRefreshError, match="provider unavailable"):
        refresh_prediction_universe(
            conn,
            provider,
            snapshot_date=SNAPSHOT_DATE,
            universe_rule=UNIVERSE_RULE,
        )

    assert conn.execute("SELECT COUNT(*) FROM current_prediction_universe").fetchone()[0] == 50
    assert conn.execute(
        """
        SELECT status, error_message
        FROM universe_refresh_runs
        WHERE snapshot_date = ? AND status = 'failed'
        """,
        [SNAPSHOT_DATE],
    ).fetchone() == ("failed", "provider unavailable")


def test_refresh_recent_missing_ratio_can_exclude_and_fill_next_candidate(tmp_path) -> None:
    conn = connect_database(tmp_path / "refresh_missing_ratio.duckdb")
    provider = FakeProvider(
        {
            "KOSPI": [
                _item("500001", "가격누락", "KOSPI", 1),
                *[_item(f"50{i:04d}", f"KOSPI {i}", "KOSPI", i + 1) for i in range(2, 33)],
            ],
            "KOSDAQ": [_item(f"60{i:04d}", f"KOSDAQ {i}", "KOSDAQ", i) for i in range(1, 21)],
        },
        price_days={"500001": 50},
    )

    refresh_prediction_universe(
        conn,
        provider,
        snapshot_date=SNAPSHOT_DATE,
        universe_rule=UNIVERSE_RULE,
        settings=RefreshSettings(min_listing_trading_days=30),
    )

    assert conn.execute(
        """
        SELECT exclusion_reason
        FROM raw_market_cap_snapshot
        WHERE snapshot_date = ? AND symbol = '500001'
        """,
        [SNAPSHOT_DATE],
    ).fetchone()[0] == "excluded recent_price_missing_ratio=0.167"
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM prediction_universe_snapshot
        WHERE snapshot_date = ? AND market = 'KOSPI'
        """,
        [SNAPSHOT_DATE],
    ).fetchone()[0] == 30


def _item(
    symbol: str,
    name: str,
    market: str,
    rank: int,
    *,
    security_type: str = "COMMON",
    is_suspended: bool = False,
) -> MarketCapItem:
    return MarketCapItem(
        symbol=symbol,
        name=name,
        market=market,
        market_cap=float(1_000_000_000 - rank),
        security_type=security_type,
        raw_rank=rank,
        is_suspended=is_suspended,
    )


def _price_frame(dates, symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": list(dates),
            "symbol": symbol,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "adj_close": 100.0,
            "volume": 1000.0,
            "trading_value": 100_000.0,
            "market_cap": None,
            "source": "fake_provider",
            "collected_at": pd.Timestamp("2026-06-09T09:00:00Z"),
        }
    )
