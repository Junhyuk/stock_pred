#!/usr/bin/env python3
"""Append completed KOSPI/KOSDAQ closes and write a new dated US observation CSV.

yfinance may refresh KOSPI (^KS11) and KOSDAQ (^KQ11) only. KOSPI100 stays on
official code 1034 and is not fetched here. KOSDAQ100 stays not_ready. Missing
investor flow is left missing. An existing index date is not replaced, and a
changed US snapshot is not overwritten.

When the SQLite file or an existing dated US CSV already holds the 2019 backfill,
only a short overlap is downloaded and new sessions are appended. Missing history
still requests bars from 2019-01-01.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.reliable_inputs import KST, session_schedule, utc_timestamp
from roboquant.index_forecast.sqlite_store import connect_index_forecast
from roboquant.index_forecast.us_market_inputs import US_SYMBOLS, normalize_us_observations

KR_SYMBOLS = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
US_PRICE_SYMBOLS = US_SYMBOLS
HISTORY_START = "2019-01-01"
BACKFILL_GRACE_DAYS = 30
MIN_BACKFILL_ROWS = 1000
INCREMENTAL_OVERLAP_DAYS = 21
YFINANCE_SOURCE = "yfinance; original publication time is unknown"

_INSERT_INDEX = """
INSERT OR IGNORE INTO index_eod (
    index_code, session_date, open, high, low, close, volume, turnover,
    source, published_at, ingested_at
) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)
"""


def yfinance_source(symbol: str) -> str:
    return f"yfinance {symbol}; original publication time is unknown"


def yahoo_history_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/history/"


def _optional_float(value: object) -> float | None:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    return float(number)


def bars_from_download(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Turn one yfinance daily frame into session_date/OHLC rows. No network."""
    columns = ["session_date", "open", "high", "low", "close", "volume"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    if isinstance(data.columns, pd.MultiIndex):
        chosen = None
        for level in range(data.columns.nlevels):
            if "Close" in {str(value) for value in data.columns.get_level_values(level)}:
                chosen = data.columns.get_level_values(level)
                break
        data.columns = chosen if chosen is not None else [str(col[0]) for col in data.columns]
    data = data.rename(columns=str).reset_index()
    date_column = "Date" if "Date" in data.columns else data.columns[0]
    stamps = pd.to_datetime(data[date_column], errors="coerce")
    # Yahoo often stamps the Seoul session at 15:00 UTC the previous calendar day.
    # A naive label is already the session date and must not be shifted.
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_convert(KST)
    session_date = stamps.dt.strftime("%Y-%m-%d")
    bars = pd.DataFrame({
        "session_date": session_date,
        "open": pd.to_numeric(data["Open"], errors="coerce") if "Open" in data else pd.NA,
        "high": pd.to_numeric(data["High"], errors="coerce") if "High" in data else pd.NA,
        "low": pd.to_numeric(data["Low"], errors="coerce") if "Low" in data else pd.NA,
        "close": pd.to_numeric(data["Close"], errors="coerce") if "Close" in data else pd.NA,
        "volume": pd.to_numeric(data["Volume"], errors="coerce") if "Volume" in data else pd.NA,
    })
    return bars.dropna(subset=["session_date"]).drop_duplicates("session_date", keep="last")


def completed_kr_rows(bars: pd.DataFrame, *, index_code: str, fetched_at, sessions=None) -> list[dict]:
    """Keep bars whose XKRX session close is at or before the fetch time."""
    if index_code not in KR_SYMBOLS:
        raise ValueError("yfinance price refresh writes KOSPI and KOSDAQ only")
    if bars is None or bars.empty:
        return []
    work = bars.copy()
    work["session_date"] = pd.to_datetime(work["session_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    work = work.dropna(subset=["session_date"]).drop_duplicates("session_date", keep="last")
    if work.empty:
        return []
    fetched = utc_timestamp(fetched_at)
    if pd.isna(fetched):
        raise ValueError("fetch time is required")
    schedule, _source = session_schedule(work["session_date"].min(), work["session_date"].max(), sessions)
    closes = dict(zip(schedule["date"], schedule["close_at"], strict=False))
    rows = []
    for record in work.to_dict("records"):
        close_at = closes.get(record["session_date"])
        close = _optional_float(record.get("close"))
        if close_at is None or pd.isna(close_at) or close_at > fetched or close is None or close <= 0:
            continue
        rows.append({
            "index_code": index_code,
            "session_date": record["session_date"],
            "open": _optional_float(record.get("open")),
            "high": _optional_float(record.get("high")),
            "low": _optional_float(record.get("low")),
            "close": close,
            "volume": _optional_float(record.get("volume")),
            "turnover": None,
            "source": yfinance_source(KR_SYMBOLS[index_code]),
            "published_at": None,
            "ingested_at": fetched.isoformat(),
        })
    return rows


def append_kr_rows(conn, rows: list[dict]) -> int:
    """Insert completed closes. Existing dates stay. published_at stays NULL."""
    allowed = [
        row for row in rows
        if row.get("index_code") in KR_SYMBOLS and row.get("published_at") is None and row.get("close")
    ]
    before = conn.total_changes
    conn.executemany(_INSERT_INDEX, [
        (row["index_code"], row["session_date"], row["open"], row["high"], row["low"],
         row["close"], row["volume"], row["source"], row["ingested_at"])
        for row in allowed
    ])
    conn.commit()
    return conn.total_changes - before


def dated_us_csv(directory: str | Path, fetched_at) -> Path:
    day = utc_timestamp(fetched_at).tz_convert(KST).strftime("%Y%m%d")
    return Path(directory) / f"us_market_observations_v2_{day}.csv"


def us_raw_frame(bars_by_symbol: dict[str, pd.DataFrame], *, fetched_at) -> pd.DataFrame:
    fetched = utc_timestamp(fetched_at)
    if pd.isna(fetched):
        raise ValueError("fetch time is required")
    rows = []
    for symbol, bars in bars_by_symbol.items():
        if symbol not in US_PRICE_SYMBOLS:
            raise ValueError("US refresh symbols are ^GSPC ^IXIC ^SOX ^VIX")
        if bars is None or bars.empty:
            continue
        for record in bars.to_dict("records"):
            close = _optional_float(record.get("close"))
            session_date = pd.to_datetime(record.get("session_date"), errors="coerce")
            if close is None or close <= 0 or pd.isna(session_date):
                continue
            rows.append({
                "symbol": symbol,
                "session_date": session_date.strftime("%Y-%m-%d"),
                "close": close,
                "source": YFINANCE_SOURCE,
                "source_url": yahoo_history_url(symbol),
                "fetched_at": fetched.isoformat(),
            })
    return pd.DataFrame(rows)


def write_us_observations(raw: pd.DataFrame, output: str | Path) -> Path:
    """Write a v2 CSV. A changed existing file is left untouched and rejected."""
    observations = normalize_us_observations(raw)["observations"]
    if observations.empty:
        raise ValueError("no completed US bars to write")
    encoded = observations.to_csv(index=False).encode("utf-8")
    destination = Path(output)
    if destination.exists() and destination.read_bytes() != encoded:
        raise ValueError("US snapshot is immutable; choose a new output path for a changed dataset")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(encoded)
    return destination


def history_covers_backfill(earliest: str | None, count: int, *, requested_start: str) -> bool:
    """True when one series still reaches the requested history start."""
    if not earliest or count < MIN_BACKFILL_ROWS:
        return False
    limit = datetime.fromisoformat(requested_start).date() + timedelta(days=BACKFILL_GRACE_DAYS)
    return str(earliest)[:10] <= limit.isoformat()


def kr_history_summary(conn) -> dict[str, tuple[str, str, int]]:
    rows = conn.execute(
        """
        SELECT index_code, MIN(session_date), MAX(session_date), COUNT(*)
        FROM index_eod
        WHERE index_code IN ('KOSPI', 'KOSDAQ')
        GROUP BY index_code
        """
    ).fetchall()
    return {str(row[0]): (str(row[1]), str(row[2]), int(row[3])) for row in rows}


def us_history_summary(frame: pd.DataFrame) -> dict[str, tuple[str, str, int]]:
    if frame.empty or not {"symbol", "session_date"}.issubset(frame.columns):
        return {}
    summary = {}
    for symbol in US_PRICE_SYMBOLS:
        subset = frame.loc[frame["symbol"].astype(str) == symbol, "session_date"].astype(str).str.slice(0, 10)
        subset = subset[subset.ne("") & subset.ne("NaT") & subset.ne("nan")]
        if subset.empty:
            return {}
        summary[symbol] = (str(subset.min()), str(subset.max()), int(subset.nunique()))
    return summary


def series_history_covered(summary: dict[str, tuple[str, str, int]], expected: set[str] | frozenset[str],
                           *, requested_start: str) -> bool:
    if set(summary) != set(expected):
        return False
    return all(history_covers_backfill(earliest, count, requested_start=requested_start)
               for earliest, _latest, count in summary.values())


def lagging_latest(summary: dict[str, tuple[str, str, int]]) -> str | None:
    if not summary:
        return None
    return min(str(latest)[:10] for _earliest, latest, _count in summary.values())


def incremental_start(latest: str | None, *, requested_start: str, covered: bool) -> str:
    """Reuse a covered cache. Anything short of that backfill starts at the request."""
    if not covered or not latest:
        return requested_start
    overlap_start = datetime.fromisoformat(str(latest)[:10]).date() - timedelta(days=INCREMENTAL_OVERLAP_DAYS)
    floor = datetime.fromisoformat(requested_start).date()
    return max(overlap_start, floor).isoformat()


def dated_us_snapshots(directory: str | Path) -> list[Path]:
    paths = []
    for path in Path(directory).glob("us_market_observations_v2_*.csv"):
        stamp = path.stem.removeprefix("us_market_observations_v2_")
        if len(stamp) == 8 and stamp.isdigit():
            paths.append(path)
    return sorted(paths)


def prior_us_snapshot(directory: str | Path, destination: str | Path) -> Path | None:
    target = Path(destination).resolve()
    others = [path for path in dated_us_snapshots(directory) if path.resolve() != target]
    if others:
        return others[-1]
    if Path(destination).is_file():
        return Path(destination)
    return None


def _read_us_snapshot(path: Path | None) -> pd.DataFrame:
    if path is None or not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (OSError, UnicodeError, ValueError, pd.errors.ParserError):
        return pd.DataFrame()


def _session_keys(frame: pd.DataFrame) -> set[tuple[str, str]]:
    if frame.empty or not {"symbol", "session_date"}.issubset(frame.columns):
        return set()
    return set(zip(frame["symbol"].astype(str), frame["session_date"].astype(str).str.slice(0, 10), strict=True))


def merge_us_observations(prior: pd.DataFrame, recent_raw: pd.DataFrame) -> pd.DataFrame:
    """Keep stored symbol-dates and append newly completed sessions."""
    recent = normalize_us_observations(recent_raw)["observations"]
    if prior is None or prior.empty:
        return recent
    if recent.empty:
        return prior.copy()
    existing = _session_keys(prior)
    additions = [
        index for index, (symbol, day) in enumerate(zip(recent["symbol"], recent["session_date"], strict=True))
        if (str(symbol), str(day)[:10]) not in existing
    ]
    if not additions:
        return prior.copy()
    return pd.concat([prior, recent.iloc[additions]], ignore_index=True).sort_values(
        ["symbol", "session_date"], kind="stable").reset_index(drop=True)


def write_merged_us_snapshot(prior: pd.DataFrame, recent_raw: pd.DataFrame, output: str | Path) -> Path:
    """Write a new dated snapshot. An existing file is left untouched."""
    merged = merge_us_observations(prior, recent_raw)
    if merged.empty:
        raise ValueError("no completed US bars to write")
    destination = Path(output)
    encoded = merged.to_csv(index=False).encode("utf-8")
    if destination.exists():
        if destination.read_bytes() == encoded:
            return destination
        existing = _read_us_snapshot(destination)
        if _session_keys(merged) <= _session_keys(existing):
            return destination
        raise ValueError("US snapshot is immutable; choose a new output path for a changed dataset")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    return destination


def _remove_sqlite_sidecars(path: str | Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_file():
            sidecar.unlink()


def _load_yfinance():
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError('yfinance is required. Install it with: pip install -e ".[global]"') from exc
    return yf


def _download_daily(symbol: str, start: str, end: str, yf_module) -> pd.DataFrame:
    frame = yf_module.download(
        symbol, start=start, end=end, progress=False, auto_adjust=False, threads=False, group_by="column",
    )
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def refresh_premarket_prices(*, sqlite_path: str | Path, output_dir: str | Path,
                             start: str = HISTORY_START, fetched_at: datetime | None = None,
                             yf_module=None) -> dict:
    fetched_at = fetched_at if fetched_at is not None else datetime.now(KST)
    fetched = utc_timestamp(fetched_at)
    if pd.isna(fetched):
        raise ValueError("fetch time is required")
    end = (fetched.tz_convert(KST).date() + timedelta(days=1)).isoformat()
    yf = yf_module or _load_yfinance()
    conn = connect_index_forecast(sqlite_path)
    checkpointed = False
    try:
        summary = kr_history_summary(conn)
        kr_start = incremental_start(
            lagging_latest(summary),
            requested_start=start,
            covered=series_history_covered(summary, set(KR_SYMBOLS), requested_start=start),
        )
        inserted = 0
        for index_code, symbol in KR_SYMBOLS.items():
            bars = bars_from_download(_download_daily(symbol, kr_start, end, yf))
            inserted += append_kr_rows(conn, completed_kr_rows(bars, index_code=index_code, fetched_at=fetched))
        stored = conn.execute(
            "SELECT COUNT(*) FROM index_eod WHERE index_code IN ('KOSPI', 'KOSDAQ')"
        ).fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpointed = True
    finally:
        conn.close()
    if checkpointed:
        _remove_sqlite_sidecars(sqlite_path)
    if stored <= 0:
        raise RuntimeError("no completed KOSPI/KOSDAQ sessions to store")
    destination = dated_us_csv(output_dir, fetched)
    prior_path = prior_us_snapshot(output_dir, destination)
    prior = _read_us_snapshot(prior_path)
    us_summary = us_history_summary(prior)
    us_covered = series_history_covered(us_summary, set(US_PRICE_SYMBOLS), requested_start=start)
    us_start = incremental_start(lagging_latest(us_summary), requested_start=start, covered=us_covered)
    raw = us_raw_frame(
        {symbol: bars_from_download(_download_daily(symbol, us_start, end, yf)) for symbol in US_PRICE_SYMBOLS},
        fetched_at=fetched,
    )
    if us_covered:
        us_csv = write_merged_us_snapshot(prior, raw, destination)
    else:
        us_csv = write_us_observations(raw, destination)
    return {"sqlite": str(sqlite_path), "us_csv": str(us_csv), "kr_rows_inserted": inserted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    parser.add_argument("--output-dir", default="data/processed")
    parser.add_argument("--start", default=HISTORY_START)
    args = parser.parse_args(argv)
    result = refresh_premarket_prices(sqlite_path=args.sqlite, output_dir=args.output_dir, start=args.start)
    print(json.dumps({"sqlite": result["sqlite"], "us_csv": result["us_csv"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
