from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def yyyymmdd(value: str | date | datetime) -> str:
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError("Date value is required")
    return parsed.strftime("%Y%m%d")


def today_string() -> str:
    return datetime.today().strftime("%Y-%m-%d")


def safe_rank_pct(series: pd.Series, ascending: bool = True) -> pd.Series:
    if series.notna().sum() == 0:
        return pd.Series(np.nan, index=series.index)
    return series.rank(pct=True, ascending=ascending)


def min_max_score(series: pd.Series) -> pd.Series:
    valid = series.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=series.index)
    low = valid.min()
    high = valid.max()
    if np.isclose(high, low):
        return pd.Series(0.5, index=series.index)
    return (series - low) / (high - low)


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    import json

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)


def coerce_date_column(df: pd.DataFrame, column: str = "date") -> pd.DataFrame:
    if column in df.columns:
        df = df.copy()
        df[column] = pd.to_datetime(df[column]).dt.date
    return df

