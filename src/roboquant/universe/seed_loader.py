from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pandas as pd

from roboquant.universe.seed_data import PREDICTION_SEED, RAW_SEED


class UniverseSeedExistsError(RuntimeError):
    """Raised when a protected snapshot already exists."""


def seed_prediction_universe(
    conn,
    *,
    snapshot_date: date,
    universe_rule: str,
    provider: str,
    force: bool = False,
    kospi_target: int = 30,
    kosdaq_target: int = 20,
) -> dict[str, int | str]:
    _validate_seed(kospi_target=kospi_target, kosdaq_target=kosdaq_target)
    existing = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM raw_market_cap_snapshot
           WHERE snapshot_date = ? AND universe_rule = ?) AS raw_count,
          (SELECT COUNT(*) FROM prediction_universe_snapshot
           WHERE snapshot_date = ? AND universe_rule = ?) AS prediction_count
        """,
        [snapshot_date, universe_rule, snapshot_date, universe_rule],
    ).fetchone()
    if not force and (existing[0] or existing[1]):
        raise UniverseSeedExistsError(
            f"Universe snapshot already exists: date={snapshot_date}, rule={universe_rule}"
        )

    now = datetime.now(UTC)
    raw = _raw_frame(snapshot_date, universe_rule, provider, now)
    prediction = _prediction_frame(snapshot_date, universe_rule, provider, now)
    symbols = _symbol_frame(now)
    run_id = f"seed-{snapshot_date:%Y%m%d}-{uuid4().hex[:12]}"

    conn.execute("BEGIN TRANSACTION")
    try:
        if force:
            conn.execute(
                "DELETE FROM raw_market_cap_snapshot WHERE snapshot_date = ? AND universe_rule = ?",
                [snapshot_date, universe_rule],
            )
            conn.execute(
                """
                DELETE FROM prediction_universe_snapshot
                WHERE snapshot_date = ? AND universe_rule = ?
                """,
                [snapshot_date, universe_rule],
            )
            conn.execute(
                """
                DELETE FROM universe_refresh_runs
                WHERE snapshot_date = ? AND universe_rule = ? AND provider = ?
                """,
                [snapshot_date, universe_rule, provider],
            )

        _insert_frame(conn, "raw_market_cap_snapshot", raw)
        _insert_frame(conn, "prediction_universe_snapshot", prediction)
        _insert_missing_symbols(conn, symbols)
        conn.execute(
            """
            INSERT INTO universe_refresh_runs (
              run_id,
              snapshot_date,
              universe_rule,
              provider,
              status,
              kospi_raw_count,
              kosdaq_raw_count,
              kospi_selected_count,
              kosdaq_selected_count,
              kospi_excluded_count,
              kosdaq_excluded_count,
              started_at,
              completed_at
            )
            VALUES (?, ?, ?, ?, 'ready', 32, 20, 30, 20, 2, 0, ?, ?)
            """,
            [run_id, snapshot_date, universe_rule, provider, now, now],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "snapshot_date": str(snapshot_date),
        "universe_rule": universe_rule,
        "raw_count": len(raw),
        "prediction_count": len(prediction),
        "kospi_count": int((prediction["market"] == "KOSPI").sum()),
        "kosdaq_count": int((prediction["market"] == "KOSDAQ").sum()),
        "run_id": run_id,
    }


def _validate_seed(*, kospi_target: int, kosdaq_target: int) -> None:
    raw_symbols = [item.symbol for item in RAW_SEED]
    prediction_symbols = [item.symbol for item in PREDICTION_SEED]
    if len(RAW_SEED) != 52 or len(set(raw_symbols)) != 52:
        raise ValueError("Raw universe seed must contain 52 unique symbols")
    if len(PREDICTION_SEED) != kospi_target + kosdaq_target:
        raise ValueError("Prediction universe seed size does not match configured targets")
    if len(set(prediction_symbols)) != len(prediction_symbols):
        raise ValueError("Prediction universe seed contains duplicate symbols")
    market_counts = pd.Series([item.market for item in PREDICTION_SEED]).value_counts().to_dict()
    if market_counts != {"KOSPI": kospi_target, "KOSDAQ": kosdaq_target}:
        raise ValueError(f"Prediction universe market counts are invalid: {market_counts}")


def _raw_frame(
    snapshot_date: date,
    universe_rule: str,
    provider: str,
    created_at: datetime,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "snapshot_date": snapshot_date,
                "symbol": item.symbol,
                "name": item.name,
                "market": item.market,
                "raw_market_cap_rank": item.raw_rank,
                "market_cap": None,
                "security_type": item.security_type,
                "is_suspended": False,
                "listing_date": None,
                "provider": provider,
                "universe_rule": universe_rule,
                "exclusion_reason": item.exclusion_reason,
                "created_at": created_at,
            }
            for item in RAW_SEED
        ]
    )


def _prediction_frame(
    snapshot_date: date,
    universe_rule: str,
    provider: str,
    created_at: datetime,
) -> pd.DataFrame:
    ranks = {"KOSPI": 0, "KOSDAQ": 0}
    rows = []
    for item in PREDICTION_SEED:
        ranks[item.market] += 1
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "symbol": item.symbol,
                "name": item.name,
                "market": item.market,
                "raw_market_cap_rank": item.raw_rank,
                "prediction_rank": ranks[item.market],
                "market_cap": None,
                "security_type": item.security_type,
                "provider": provider,
                "universe_rule": universe_rule,
                "is_enabled": True,
                "exclusion_reason": None,
                "created_at": created_at,
            }
        )
    return pd.DataFrame(rows)


def _symbol_frame(collected_at: datetime) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": item.symbol,
                "name": item.name,
                "market": item.market,
                "is_active": True,
                "collected_at": collected_at,
            }
            for item in RAW_SEED
        ]
    ).drop_duplicates("symbol")


def _insert_frame(conn, table: str, frame: pd.DataFrame) -> None:
    temp_name = f"seed_{table}_{uuid4().hex}"
    conn.register(temp_name, frame)
    try:
        columns = ", ".join(frame.columns)
        conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temp_name}")
    finally:
        conn.unregister(temp_name)


def _insert_missing_symbols(conn, frame: pd.DataFrame) -> None:
    temp_name = f"seed_symbols_{uuid4().hex}"
    conn.register(temp_name, frame)
    try:
        conn.execute(
            f"""
            INSERT INTO symbols (symbol, name, market, is_active, collected_at)
            SELECT seed.symbol, seed.name, seed.market, seed.is_active, seed.collected_at
            FROM {temp_name} AS seed
            WHERE NOT EXISTS (
              SELECT 1 FROM symbols AS existing WHERE existing.symbol = seed.symbol
            )
            """
        )
    finally:
        conn.unregister(temp_name)

