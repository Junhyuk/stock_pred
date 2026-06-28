from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from typing import Any

import pandas as pd

from roboquant.data.collectors.analyst.whynot_parser import parse_saved_html_tables

STANDARD_COLUMNS = [
    "report_id",
    "report_date",
    "symbol",
    "stock_name",
    "market",
    "sector",
    "broker_name",
    "analyst_name",
    "report_title",
    "investment_rating",
    "target_price",
    "previous_target_price",
    "target_change_pct",
    "current_price_at_report",
    "upside_pct_at_report",
    "source_name",
    "source_url",
    "imported_at",
]


def import_analyst_sources(
    source_config: dict[str, Any],
    symbols: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    import_config = source_config.get("import", {})
    frames: list[pd.DataFrame] = []

    for csv_path in import_config.get("csv_paths", []):
        path = Path(csv_path)
        if path.exists():
            frames.append(pd.read_csv(path))

    for html_path in import_config.get("html_paths", []):
        path = Path(html_path)
        if path.exists():
            frames.append(parse_saved_html_tables(path))

    if not frames:
        return pd.DataFrame(columns=STANDARD_COLUMNS), pd.DataFrame()

    raw = pd.concat(frames, ignore_index=True)
    normalized = normalize_analyst_reports(raw, source_config, symbols)
    failures = build_import_failures(normalized)
    valid = normalized[normalized["symbol"].notna()].copy()
    if import_config.get("require_source_metadata", True):
        valid = valid[valid["source_name"].notna() & valid["source_url"].notna()].copy()
    if valid.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS), failures
    return valid[STANDARD_COLUMNS].drop_duplicates("report_id"), failures


def normalize_analyst_reports(
    raw: pd.DataFrame,
    source_config: dict[str, Any],
    symbols: pd.DataFrame,
) -> pd.DataFrame:
    mapping = source_config.get("column_mapping", {})
    import_config = source_config.get("import", {})
    frame = pd.DataFrame()

    for standard_column in [
        "report_date",
        "stock_name",
        "ticker",
        "broker_name",
        "analyst_name",
        "report_title",
        "investment_rating",
        "target_price",
        "previous_target_price",
        "target_change_pct",
        "current_price_at_report",
        "upside_pct_at_report",
        "source_name",
        "source_url",
    ]:
        frame[standard_column] = _pick_column(raw, mapping.get(standard_column, [standard_column]))

    frame["report_date"] = pd.to_datetime(frame["report_date"], errors="coerce").dt.date
    frame["stock_name"] = frame["stock_name"].astype("string").str.strip()
    frame["ticker"] = frame["ticker"].astype("string").str.strip()
    for column in ("broker_name", "analyst_name", "report_title", "investment_rating"):
        frame[column] = frame[column].astype("string").str.strip()
    for column in (
        "target_price",
        "previous_target_price",
        "target_change_pct",
        "current_price_at_report",
        "upside_pct_at_report",
    ):
        frame[column] = _numeric_series(frame[column])

    frame["source_name"] = frame["source_name"].where(
        frame["source_name"].notna(),
        import_config.get("default_source_name", "manual_import"),
    )
    frame["source_url"] = frame["source_url"].where(
        frame["source_url"].notna(),
        import_config.get("default_source_url", "manual://analyst_reports"),
    )
    frame = attach_symbol_metadata(frame, symbols)
    frame["target_change_pct"] = frame["target_change_pct"].where(
        frame["target_change_pct"].notna(),
        (frame["target_price"] / frame["previous_target_price"] - 1.0) * 100.0,
    )
    frame["upside_pct_at_report"] = frame["upside_pct_at_report"].where(
        frame["upside_pct_at_report"].notna(),
        (frame["target_price"] / frame["current_price_at_report"] - 1.0) * 100.0,
    )
    frame["report_id"] = frame.apply(_report_id, axis=1)
    frame["imported_at"] = _utcnow()
    return frame.rename(columns={"ticker": "input_ticker"})


def attach_symbol_metadata(frame: pd.DataFrame, symbols: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    if symbols.empty:
        output["symbol"] = _normalize_symbol(output["ticker"])
        output["market"] = None
        output["sector"] = None
        return output

    symbol_map = symbols.copy()
    symbol_map["symbol"] = symbol_map["symbol"].astype(str).str.zfill(6)
    by_symbol = symbol_map.set_index("symbol")
    by_name = symbol_map.dropna(subset=["name"]).drop_duplicates("name").set_index("name")

    output["symbol"] = _normalize_symbol(output["ticker"])
    missing = output["symbol"].isna()
    if missing.any():
        output.loc[missing, "symbol"] = output.loc[missing, "stock_name"].map(by_name["symbol"])
    output["market"] = output["symbol"].map(by_symbol["market"])
    output["sector"] = output["symbol"].map(
        by_symbol["sector"] if "sector" in by_symbol.columns else pd.Series(dtype=object)
    )
    return output


def build_import_failures(normalized: pd.DataFrame) -> pd.DataFrame:
    if normalized.empty:
        return pd.DataFrame()
    failures: list[pd.DataFrame] = []
    unmatched = normalized[normalized["symbol"].isna()].copy()
    if not unmatched.empty:
        failures.append(
            pd.DataFrame(
                {
                    "collected_at": _utcnow(),
                    "step": "import_analyst_reports",
                    "source": unmatched["source_name"].fillna("manual_import"),
                    "symbol": None,
                    "target_date": unmatched["report_date"],
                    "error_message": "could not match analyst report stock_name/ticker to symbols table",
                    "retry_count": 0,
                }
            )
        )
    missing_source = normalized[
        normalized["symbol"].notna() & (normalized["source_name"].isna() | normalized["source_url"].isna())
    ].copy()
    if not missing_source.empty:
        failures.append(
            pd.DataFrame(
                {
                    "collected_at": _utcnow(),
                    "step": "import_analyst_reports",
                    "source": missing_source["source_name"].fillna("unknown"),
                    "symbol": missing_source["symbol"],
                    "target_date": missing_source["report_date"],
                    "error_message": "analyst report row missing required source_name/source_url metadata",
                    "retry_count": 0,
                }
            )
        )
    if not failures:
        return pd.DataFrame()
    return pd.concat(failures, ignore_index=True)


def _pick_column(raw: pd.DataFrame, candidates: list[str]) -> pd.Series:
    for candidate in candidates:
        if candidate in raw.columns:
            return raw[candidate]
    return pd.Series(pd.NA, index=raw.index)


def _numeric_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.replace(",", "", regex=False).str.replace("%", "", regex=False)
    return pd.to_numeric(cleaned, errors="coerce")


def _normalize_symbol(series: pd.Series) -> pd.Series:
    normalized = series.astype("string").str.extract(r"(\d{1,6})", expand=False)
    return normalized.dropna().str.zfill(6).reindex(series.index)


def _report_id(row: pd.Series) -> str:
    parts = [
        _string_part(row.get("report_date")),
        _string_part(row.get("symbol")),
        _string_part(row.get("stock_name")),
        _string_part(row.get("broker_name")),
        _string_part(row.get("analyst_name")),
        _string_part(row.get("report_title")),
    ]
    return sha1("|".join(parts).encode("utf-8")).hexdigest()


def _string_part(value) -> str:
    if pd.isna(value):
        return ""
    return str(value)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
