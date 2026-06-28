from __future__ import annotations

from datetime import datetime

import pandas as pd


def collection_failure_row(
    step: str,
    source: str,
    error: Exception | str,
    symbol: str | None = None,
    target_date: str | None = None,
    retry_count: int = 0,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "collected_at": datetime.utcnow(),
                "step": step,
                "source": source,
                "symbol": symbol,
                "target_date": pd.to_datetime(target_date).date() if target_date else None,
                "error_message": str(error),
                "retry_count": int(retry_count),
            }
        ]
    )

