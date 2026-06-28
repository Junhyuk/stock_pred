from __future__ import annotations

from uuid import uuid4

import pandas as pd

DEFAULT_UNIVERSE_RULE = "prediction_top_market_cap"


def sync_prediction_universe_symbols(
    conn,
    universe_rule: str = DEFAULT_UNIVERSE_RULE,
) -> int:
    """Insert missing Top50 symbols and refresh market/name from current_prediction_universe."""
    frame = conn.execute(
        """
        SELECT
          symbol,
          name,
          market,
          TRUE AS is_active,
          CURRENT_TIMESTAMP AS collected_at
        FROM current_prediction_universe
        WHERE universe_rule = ?
        """,
        [universe_rule],
    ).fetchdf()
    if frame.empty:
        return 0

    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    temp_name = f"sync_universe_symbols_{uuid4().hex}"
    conn.register(temp_name, frame)
    try:
        conn.execute(
            f"""
            UPDATE symbols AS existing
            SET
              name = src.name,
              market = src.market,
              is_active = src.is_active,
              collected_at = src.collected_at
            FROM {temp_name} AS src
            WHERE existing.symbol = src.symbol
              AND (
                existing.market IS DISTINCT FROM src.market
                OR existing.name IS DISTINCT FROM src.name
                OR existing.is_active IS DISTINCT FROM src.is_active
              )
            """
        )
        conn.execute(
            f"""
            INSERT INTO symbols (symbol, name, market, is_active, collected_at)
            SELECT
              src.symbol,
              src.name,
              src.market,
              src.is_active,
              src.collected_at
            FROM {temp_name} AS src
            WHERE NOT EXISTS (
              SELECT 1 FROM symbols AS existing WHERE existing.symbol = src.symbol
            )
            """
        )
    finally:
        conn.unregister(temp_name)
    return len(frame)


def load_prediction_universe_symbols(
    conn,
    universe_rule: str = DEFAULT_UNIVERSE_RULE,
) -> pd.DataFrame:
    """Return symbol metadata from the active prediction universe."""
    frame = conn.execute(
        """
        SELECT symbol, name, market
        FROM current_prediction_universe
        WHERE universe_rule = ?
        ORDER BY market, raw_market_cap_rank NULLS LAST, market_cap DESC
        """,
        [universe_rule],
    ).fetchdf()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame
