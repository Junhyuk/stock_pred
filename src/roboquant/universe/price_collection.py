from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from time import sleep
from typing import Any

import pandas as pd

from roboquant.data.collectors.krx import fetch_prices
from roboquant.data.validators.quality import validate_prices
from roboquant.db import append_dedup_table

DEFAULT_UNIVERSE_RULE = "prediction_top_market_cap"
DEFAULT_EXPECTED_COUNTS = {"KOSPI": 30, "KOSDAQ": 20}

FetchPricesFn = Callable[[str, str, str | None], pd.DataFrame]


class PredictionUniversePriceCollectionError(RuntimeError):
    """Raised when the active prediction universe cannot be fully price-covered."""


@dataclass(frozen=True)
class PriceCollectionSummary:
    snapshot_date: str
    universe_rule: str
    symbol_count: int
    inserted_rows: int
    skipped_symbols: int
    empty_symbols: int
    latest_price_date: str | None


def collect_prediction_universe_prices(
    conn,
    *,
    universe_rule: str = DEFAULT_UNIVERSE_RULE,
    snapshot_date: str = "latest",
    start_date: str = "2019-01-01",
    end_date: str | None = None,
    sleep_seconds: float = 0.0,
    strict_missing: bool = True,
    fetch_prices_fn: FetchPricesFn | None = None,
) -> PriceCollectionSummary:
    universe = load_prediction_universe(conn, universe_rule, snapshot_date)
    validate_prediction_universe(universe)

    fetcher = fetch_prices_fn or fetch_prices
    end = _parse_end_date(end_date)
    inserted_rows = 0
    skipped_symbols = 0
    empty_symbols = 0
    empty_without_history: list[str] = []
    failed_symbols: list[str] = []

    for row in universe.to_dict("records"):
        symbol = str(row["symbol"]).zfill(6)
        existing_latest = _latest_price_date(conn, symbol)
        collect_from = _next_collect_date(existing_latest, start_date)
        if collect_from > end:
            skipped_symbols += 1
            continue

        try:
            prices = fetcher(symbol, collect_from.isoformat(), end.isoformat())
        except Exception as exc:
            failed_symbols.append(f"{symbol}: {exc}")
            if sleep_seconds:
                sleep(sleep_seconds)
            continue
        if prices.empty:
            empty_symbols += 1
            if strict_missing and _price_row_count(conn, symbol) == 0:
                empty_without_history.append(symbol)
            if sleep_seconds:
                sleep(sleep_seconds)
            continue

        try:
            validate_prices(prices).raise_for_errors()
            append_dedup_table(conn, "prices_daily", prices, ["date", "symbol"])
            inserted_rows += len(prices)
        except Exception as exc:
            failed_symbols.append(f"{symbol}: {exc}")
        if sleep_seconds:
            sleep(sleep_seconds)

    missing = _missing_price_symbols(conn, universe)
    if strict_missing and (failed_symbols or empty_without_history or missing):
        symbols = sorted(set(empty_without_history).union(missing))
        raise PredictionUniversePriceCollectionError(
            "Could not fully collect prediction universe prices: "
            f"failed={failed_symbols[:10]}, missing_history={symbols}"
        )

    latest_price_date = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()[0]
    return PriceCollectionSummary(
        snapshot_date=str(universe["snapshot_date"].iloc[0]),
        universe_rule=universe_rule,
        symbol_count=len(universe),
        inserted_rows=inserted_rows,
        skipped_symbols=skipped_symbols,
        empty_symbols=empty_symbols,
        latest_price_date=str(latest_price_date) if latest_price_date is not None else None,
    )


def load_prediction_universe(conn, universe_rule: str, snapshot_date: str = "latest") -> pd.DataFrame:
    if snapshot_date == "latest":
        return conn.execute(
            """
            SELECT *
            FROM current_prediction_universe
            WHERE universe_rule = ?
            ORDER BY market, prediction_rank, symbol
            """,
            [universe_rule],
        ).fetchdf()
    return conn.execute(
        """
        SELECT *
        FROM prediction_universe_snapshot
        WHERE universe_rule = ?
          AND snapshot_date = ?
          AND is_enabled = TRUE
        ORDER BY market, prediction_rank, symbol
        """,
        [universe_rule, snapshot_date],
    ).fetchdf()


def validate_prediction_universe(
    universe: pd.DataFrame,
    expected_counts: dict[str, int] | None = None,
) -> None:
    expected_counts = expected_counts or DEFAULT_EXPECTED_COUNTS
    if universe.empty:
        raise PredictionUniversePriceCollectionError("Prediction universe is empty")

    missing_columns = {"symbol", "market", "snapshot_date"}.difference(universe.columns)
    if missing_columns:
        raise PredictionUniversePriceCollectionError(
            f"Prediction universe is missing columns: {sorted(missing_columns)}"
        )

    duplicated = universe["symbol"].astype(str).str.zfill(6).duplicated()
    if duplicated.any():
        symbols = sorted(universe.loc[duplicated, "symbol"].astype(str).str.zfill(6).tolist())
        raise PredictionUniversePriceCollectionError(
            f"Prediction universe contains duplicate symbols: {symbols}"
        )

    counts = universe.groupby("market")["symbol"].count().to_dict()
    expected_total = sum(expected_counts.values())
    if len(universe) != expected_total:
        raise PredictionUniversePriceCollectionError(
            f"Prediction universe size mismatch: expected={expected_total}, actual={len(universe)}"
        )
    for market, expected in expected_counts.items():
        actual = int(counts.get(market, 0))
        if actual != expected:
            raise PredictionUniversePriceCollectionError(
                f"{market} universe size mismatch: expected={expected}, actual={actual}"
            )


def _parse_end_date(value: str | None) -> date:
    if value in (None, "latest"):
        return date.today()
    return date.fromisoformat(value)


def _next_collect_date(existing_latest: date | None, start_date: str) -> date:
    if existing_latest is None:
        return date.fromisoformat(start_date)
    return existing_latest + timedelta(days=1)


def _latest_price_date(conn, symbol: str) -> date | None:
    value = conn.execute(
        "SELECT MAX(date) FROM prices_daily WHERE symbol = ?",
        [str(symbol).zfill(6)],
    ).fetchone()[0]
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _price_row_count(conn, symbol: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM prices_daily WHERE symbol = ?",
            [str(symbol).zfill(6)],
        ).fetchone()[0]
    )


def _missing_price_symbols(conn, universe: pd.DataFrame) -> list[str]:
    symbols = [str(symbol).zfill(6) for symbol in universe["symbol"].tolist()]
    if not symbols:
        return []
    placeholders = ", ".join(["?"] * len(symbols))
    covered = {
        str(row[0]).zfill(6)
        for row in conn.execute(
            f"""
            SELECT symbol
            FROM prices_daily
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
            HAVING COUNT(*) > 0
            """,
            symbols,
        ).fetchall()
    }
    return sorted(set(symbols).difference(covered))


def summary_as_dict(summary: PriceCollectionSummary) -> dict[str, Any]:
    return {
        "snapshot_date": summary.snapshot_date,
        "universe_rule": summary.universe_rule,
        "symbol_count": summary.symbol_count,
        "inserted_rows": summary.inserted_rows,
        "skipped_symbols": summary.skipped_symbols,
        "empty_symbols": summary.empty_symbols,
        "latest_price_date": summary.latest_price_date,
    }
