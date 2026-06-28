from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from roboquant.db import append_dedup_table, table_exists

US_SECTOR_FEATURE_COLUMNS = [
    "us_sector_return_1d",
    "us_sector_return_5d",
    "us_sector_zscore_20d",
    "us_sector_beta_60d",
    "us_sector_corr_60d",
    "us_sector_impact_score",
    "us_sector_direction_agreement",
]
US_SECTOR_TRAINING_FEATURE_COLUMNS = list(US_SECTOR_FEATURE_COLUMNS)

DEFAULT_SECTOR_CONFIG = {
    "semiconductor": {
        "aliases": ["반도체", "전자", "semiconductor"],
        "primary_proxy": "SOXX",
        "proxies": ["^SOX", "SOXX", "SMH", "TSM", "NVDA", "QQQ"],
    },
    "auto": {
        "aliases": ["자동차", "자동차부품", "운수장비", "auto"],
        "primary_proxy": "DRIV",
        "proxies": ["DRIV", "XLY", "TSLA", "GM", "F"],
    },
    "industrial": {
        "aliases": ["산업재", "기계", "조선", "건설", "industrial"],
        "primary_proxy": "XLI",
        "proxies": ["XLI", "SPY"],
    },
    "financial": {
        "aliases": ["금융", "은행", "증권", "보험", "financial"],
        "primary_proxy": "XLF",
        "proxies": ["XLF", "SPY"],
    },
    "healthcare": {
        "aliases": ["헬스케어", "바이오", "제약", "healthcare"],
        "primary_proxy": "XLV",
        "proxies": ["XLV", "IBB", "XBI"],
    },
    "energy_materials": {
        "aliases": ["에너지", "화학", "소재", "철강", "배터리", "materials"],
        "primary_proxy": "XLB",
        "proxies": ["XLE", "XLB", "LIT"],
    },
    "broad": {
        "aliases": ["기타", "KOSPI", "KOSDAQ", "broad"],
        "primary_proxy": "SPY",
        "proxies": ["SPY", "QQQ", "EWY"],
    },
}


def refresh_us_sector_linkage(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str = "latest",
) -> pd.DataFrame:
    frame = build_us_sector_linkage(conn, config=config, asof_date=asof_date)
    if frame.empty:
        return frame
    append_dedup_table(conn, "us_sector_linkage_daily", frame, ["trade_date", "domestic_sector"])
    return frame


def build_us_sector_linkage(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str = "latest",
) -> pd.DataFrame:
    sectors = sector_config(config)
    dates = _analysis_dates(conn, asof_date)
    if not dates or not sectors:
        return _empty_linkage_frame()
    global_returns = _global_sector_returns(conn, sectors)
    domestic_returns = _domestic_sector_returns(conn, sectors)
    rows: list[dict[str, Any]] = []
    created_at = _utcnow()

    for target_date in dates:
        global_cutoff = target_date - timedelta(days=1)
        domestic_cutoff = target_date - timedelta(days=1)
        for sector, mapping in sectors.items():
            sector_global = global_returns[global_returns["domestic_sector"].eq(sector)].copy()
            sector_domestic = domestic_returns[domestic_returns["domestic_sector"].eq(sector)].copy()
            latest_global = _latest_global_row(sector_global, global_cutoff)
            metrics = _rolling_metrics(sector_global, sector_domestic, global_cutoff, domestic_cutoff)
            row = _linkage_row(
                target_date=target_date,
                domestic_sector=sector,
                mapping=mapping,
                latest_global=latest_global,
                metrics=metrics,
                created_at=created_at,
            )
            rows.append(row)
    return pd.DataFrame(rows, columns=_empty_linkage_frame().columns)


def attach_us_sector_features(
    features: pd.DataFrame,
    linkage: pd.DataFrame | None,
    symbols: pd.DataFrame | None = None,
    config: dict[str, Any] | None = None,
    *,
    missing_factor_default: float = 0.5,
) -> pd.DataFrame:
    if features.empty:
        return features
    output = features.copy()
    output["symbol"] = output["symbol"].astype(str).str.zfill(6)
    sector_by_symbol = _sector_by_symbol(symbols)
    output["domestic_sector"] = output["symbol"].map(sector_by_symbol).fillna("broad")
    if linkage is not None and not linkage.empty:
        items = linkage.copy()
        items["date"] = pd.to_datetime(items["trade_date"]).dt.date
        keep = ["date", "domestic_sector", *US_SECTOR_FEATURE_COLUMNS]
        items = items[[column for column in keep if column in items.columns]]
        output["date"] = pd.to_datetime(output["date"]).dt.date
        output = output.merge(items, on=["date", "domestic_sector"], how="left")
    for column in US_SECTOR_FEATURE_COLUMNS:
        if column not in output.columns:
            output[column] = None
    defaults = {
        "us_sector_impact_score": missing_factor_default,
        "us_sector_direction_agreement": missing_factor_default,
    }
    for column in US_SECTOR_FEATURE_COLUMNS:
        output[column] = pd.to_numeric(output[column], errors="coerce").fillna(defaults.get(column, 0.0))
    return output.drop(columns=["domestic_sector"], errors="ignore")


def get_sector_linkage(
    conn,
    *,
    date: str = "latest",
    sector: str = "all",
    limit: int = 50,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = _stored_linkage(conn, date=date, sector=sector, limit=limit)
    source = "us_sector_linkage_daily"
    if frame.empty:
        frame = build_us_sector_linkage(conn, config=config, asof_date=date)
        frame = _filter_linkage(frame, sector=sector, limit=limit)
        source = "computed_live"
    if frame.empty:
        return {
            "asof_date": None,
            "status": "not_collected",
            "source": source,
            "summary": {"count": 0, "sectors": []},
            "items": [],
            "data_quality": {"status": "not_collected", "messages": ["미국 유사섹터 linkage 데이터가 없습니다."]},
        }
    items = _records_with_json(frame)
    quality = items[0].get("data_quality") or {}
    return {
        "asof_date": _date_string(frame["trade_date"].max()),
        "status": quality.get("status") or "partial_ready",
        "source": source,
        "summary": {
            "count": len(items),
            "sectors": sorted({str(item.get("domestic_sector")) for item in items if item.get("domestic_sector")}),
        },
        "items": items,
        "data_quality": quality,
    }


def sector_config(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    raw = ((config or {}).get("sector_linkage") or {}).get("sectors")
    if not raw:
        return {key: dict(value) for key, value in DEFAULT_SECTOR_CONFIG.items()}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        mapping = dict(value or {})
        mapping["proxies"] = [str(item).strip() for item in mapping.get("proxies", []) if str(item).strip()]
        mapping["primary_proxy"] = str(mapping.get("primary_proxy") or (mapping["proxies"][0] if mapping["proxies"] else "")).strip()
        mapping["aliases"] = [str(item) for item in mapping.get("aliases", [])]
        result[str(key)] = mapping
    if "broad" not in result:
        result["broad"] = dict(DEFAULT_SECTOR_CONFIG["broad"])
    return result


def normalize_domestic_sector(value: Any, config: dict[str, Any] | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return "broad"
    lowered = text.lower()
    for key, mapping in sector_config(config).items():
        aliases = [key, *list(mapping.get("aliases") or [])]
        for alias in aliases:
            alias_text = str(alias).strip().lower()
            if alias_text and alias_text in lowered:
                return key
    return "broad"


def _linkage_row(
    *,
    target_date: date,
    domestic_sector: str,
    mapping: dict[str, Any],
    latest_global: pd.Series | None,
    metrics: dict[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    return_1d = _safe_float(None if latest_global is None else latest_global.get("us_sector_return_1d"))
    return_5d = _safe_float(None if latest_global is None else latest_global.get("us_sector_return_5d"))
    zscore = _safe_float(metrics.get("zscore_20d"))
    beta = _safe_float(metrics.get("beta_60d"))
    corr = _safe_float(metrics.get("corr_60d"))
    agreement = _safe_float(metrics.get("direction_agreement"), 0.5)
    quality = _data_quality(mapping, latest_global, metrics)
    return {
        "trade_date": target_date,
        "domestic_sector": domestic_sector,
        "primary_proxy": mapping.get("primary_proxy"),
        "proxy_symbols_json": _json(mapping.get("proxies") or []),
        "us_sector_return_1d": return_1d,
        "us_sector_return_5d": return_5d,
        "us_sector_zscore_20d": zscore,
        "us_sector_beta_60d": beta,
        "us_sector_corr_60d": corr,
        "us_sector_impact_score": calculate_us_sector_impact_score(return_1d, zscore, corr, agreement),
        "us_sector_direction_agreement": agreement,
        "sample_count_60d": int(metrics.get("sample_count_60d") or 0),
        "data_quality_json": _json(quality),
        "created_at": created_at,
    }


def calculate_us_sector_impact_score(
    return_1d: float | None,
    zscore_20d: float | None,
    corr_60d: float | None,
    direction_agreement: float | None,
) -> float:
    return_score = _scaled_score(return_1d, scale=0.04)
    z_score = _scaled_score(zscore_20d, scale=3.0)
    corr_score = 0.5 + max(-1.0, min(1.0, float(corr_60d or 0.0))) * 0.25
    agree_score = max(0.0, min(1.0, float(direction_agreement if direction_agreement is not None else 0.5)))
    score = return_score * 0.45 + z_score * 0.25 + corr_score * 0.15 + agree_score * 0.15
    return float(max(0.0, min(1.0, score)))


def _global_sector_returns(conn, sectors: dict[str, dict[str, Any]]) -> pd.DataFrame:
    if not table_exists(conn, "global_market_daily"):
        return pd.DataFrame(columns=["trade_date", "domestic_sector", "us_sector_return_1d", "us_sector_return_5d"])
    symbols = sorted({symbol for mapping in sectors.values() for symbol in mapping.get("proxies", [])})
    if not symbols:
        return pd.DataFrame(columns=["trade_date", "domestic_sector", "us_sector_return_1d", "us_sector_return_5d"])
    placeholders = ", ".join(["?"] * len(symbols))
    frame = conn.execute(
        f"""
        SELECT trade_date, symbol, return_1d, return_5d
        FROM global_market_daily
        WHERE symbol IN ({placeholders})
        ORDER BY trade_date, symbol
        """,
        symbols,
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "domestic_sector", "us_sector_return_1d", "us_sector_return_5d"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    rows = []
    for sector, mapping in sectors.items():
        subset = frame[frame["symbol"].isin(mapping.get("proxies", []))].copy()
        if subset.empty:
            continue
        grouped = (
            subset.groupby("trade_date", as_index=False)
            .agg(
                us_sector_return_1d=("return_1d", "mean"),
                us_sector_return_5d=("return_5d", "mean"),
            )
        )
        grouped["domestic_sector"] = sector
        rows.append(grouped)
    if not rows:
        return pd.DataFrame(columns=["trade_date", "domestic_sector", "us_sector_return_1d", "us_sector_return_5d"])
    return pd.concat(rows, ignore_index=True).sort_values(["domestic_sector", "trade_date"])


def _domestic_sector_returns(conn, sectors: dict[str, dict[str, Any]]) -> pd.DataFrame:
    if not table_exists(conn, "prices_daily"):
        return pd.DataFrame(columns=["trade_date", "domestic_sector", "domestic_return_1d"])
    frame = conn.execute(
        """
        SELECT p.date AS trade_date, p.symbol, p.close, COALESCE(s.sector, s.market, '기타') AS sector
        FROM prices_daily AS p
        LEFT JOIN symbols AS s ON p.symbol = s.symbol
        WHERE p.close IS NOT NULL
        ORDER BY p.symbol, p.date
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "domestic_sector", "domestic_return_1d"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame["domestic_sector"] = frame["sector"].map(lambda value: normalize_domestic_sector(value, {"sector_linkage": {"sectors": sectors}}))
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["domestic_return_1d"] = frame.groupby("symbol")["close"].pct_change(1)
    grouped = (
        frame.groupby(["trade_date", "domestic_sector"], as_index=False)["domestic_return_1d"]
        .mean()
        .dropna(subset=["domestic_return_1d"])
    )
    return grouped


def _rolling_metrics(
    sector_global: pd.DataFrame,
    sector_domestic: pd.DataFrame,
    global_cutoff: date,
    domestic_cutoff: date,
) -> dict[str, Any]:
    global_hist = sector_global[sector_global["trade_date"] <= global_cutoff].sort_values("trade_date").copy()
    if global_hist.empty:
        return {"sample_count_60d": 0, "direction_agreement": 0.5}
    returns = pd.to_numeric(global_hist["us_sector_return_1d"], errors="coerce").dropna()
    latest_return = returns.iloc[-1] if not returns.empty else None
    recent = returns.tail(20)
    zscore = None
    if latest_return is not None and len(recent) >= 5:
        std = float(recent.std(ddof=0))
        if std > 1e-9:
            zscore = float((latest_return - float(recent.mean())) / std)

    domestic_hist = sector_domestic[sector_domestic["trade_date"] <= domestic_cutoff].sort_values("trade_date").copy()
    joined = domestic_hist.merge(
        global_hist[["trade_date", "us_sector_return_1d"]],
        on="trade_date",
        how="inner",
    ).dropna(subset=["domestic_return_1d", "us_sector_return_1d"])
    joined = joined.tail(60)
    beta = None
    corr = None
    direction_agreement = 0.5
    if len(joined) >= 5:
        x = pd.to_numeric(joined["us_sector_return_1d"], errors="coerce").to_numpy(dtype=float)
        y = pd.to_numeric(joined["domestic_return_1d"], errors="coerce").to_numpy(dtype=float)
        variance = float(np.var(x))
        if variance > 1e-12:
            beta = float(np.cov(x, y, ddof=0)[0, 1] / variance)
        if float(np.std(x)) > 1e-12 and float(np.std(y)) > 1e-12:
            corr_value = pd.Series(x).corr(pd.Series(y))
            corr = float(corr_value) if pd.notna(corr_value) else None
        direction_agreement = float((np.sign(x) == np.sign(y)).mean())
    return {
        "zscore_20d": zscore,
        "beta_60d": beta,
        "corr_60d": corr,
        "direction_agreement": direction_agreement,
        "sample_count_60d": int(len(joined)),
    }


def _latest_global_row(sector_global: pd.DataFrame, cutoff: date) -> pd.Series | None:
    if sector_global.empty:
        return None
    rows = sector_global[sector_global["trade_date"] <= cutoff].sort_values("trade_date")
    if rows.empty:
        return None
    return rows.iloc[-1]


def _analysis_dates(conn, asof_date: str) -> list[date]:
    target = None if not asof_date or asof_date == "latest" else pd.to_datetime(asof_date).date()
    dates: set[date] = set()
    for table, column in (("prices_daily", "date"), ("features_daily", "date")):
        if not table_exists(conn, table):
            continue
        query = f"SELECT DISTINCT {column} FROM {table}"
        params: list[Any] = []
        if target is not None:
            query += f" WHERE {column} = ?"
            params.append(target)
        for (value,) in conn.execute(query, params).fetchall():
            parsed = _to_date(value)
            if parsed is not None:
                dates.add(parsed)
    return sorted(dates)


def _sector_by_symbol(symbols: pd.DataFrame | None) -> dict[str, str]:
    if symbols is None or symbols.empty:
        return {}
    frame = symbols.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return {
        row["symbol"]: normalize_domestic_sector(row.get("sector") or row.get("market"))
        for row in frame.to_dict("records")
    }


def _data_quality(mapping: dict[str, Any], latest_global: pd.Series | None, metrics: dict[str, Any]) -> dict[str, Any]:
    messages = []
    components = {
        "global_proxy": "ready" if latest_global is not None else "missing",
        "domestic_history": "ready" if int(metrics.get("sample_count_60d") or 0) >= 5 else "partial",
    }
    if latest_global is None:
        messages.append("미국 유사섹터 proxy 데이터가 없어 중립값을 사용했습니다.")
    if int(metrics.get("sample_count_60d") or 0) < 5:
        messages.append("국내 섹터와 미국 proxy의 rolling beta 표본이 부족합니다.")
    return {
        "status": "ready" if all(value == "ready" for value in components.values()) else "partial_ready",
        "components": components,
        "messages": messages,
        "primary_proxy": mapping.get("primary_proxy"),
        "proxies": list(mapping.get("proxies") or []),
        "lookahead_guard": "global daily data uses target_date - 1 or earlier",
    }


def _stored_linkage(conn, *, date: str, sector: str, limit: int) -> pd.DataFrame:
    if not table_exists(conn, "us_sector_linkage_daily"):
        return pd.DataFrame()
    target = date
    if not target or target == "latest":
        row = conn.execute("SELECT MAX(trade_date) FROM us_sector_linkage_daily").fetchone()
        if not row or row[0] is None:
            return pd.DataFrame()
        target = _date_string(row[0]) or "latest"
    params: list[Any] = [pd.to_datetime(target).date()]
    where = ["trade_date = ?"]
    normalized_sector = str(sector or "all")
    if normalized_sector != "all":
        where.append("domestic_sector = ?")
        params.append(normalize_domestic_sector(normalized_sector))
    params.append(int(max(1, limit or 50)))
    return conn.execute(
        f"""
        SELECT *
        FROM us_sector_linkage_daily
        WHERE {" AND ".join(where)}
        ORDER BY domestic_sector
        LIMIT ?
        """,
        params,
    ).fetchdf()


def _filter_linkage(frame: pd.DataFrame, *, sector: str, limit: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    if sector and sector != "all":
        output = output[output["domestic_sector"].eq(normalize_domestic_sector(sector))]
    return output.sort_values("domestic_sector").head(int(max(1, limit or 50)))


def _records_with_json(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = []
    for item in _records(frame):
        item["proxy_symbols"] = _loads(item.get("proxy_symbols_json"), [])
        item["data_quality"] = _loads(item.get("data_quality_json"), {})
        records.append(item)
    return records


def _scaled_score(value: float | None, *, scale: float) -> float:
    if value is None:
        return 0.5
    return float(max(0.0, min(1.0, 0.5 + float(value) / max(scale, 1e-9) * 0.5)))


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _empty_linkage_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trade_date",
            "domestic_sector",
            "primary_proxy",
            "proxy_symbols_json",
            "us_sector_return_1d",
            "us_sector_return_5d",
            "us_sector_zscore_20d",
            "us_sector_beta_60d",
            "us_sector_corr_60d",
            "us_sector_impact_score",
            "us_sector_direction_agreement",
            "sample_count_60d",
            "data_quality_json",
            "created_at",
        ]
    )


def _json(value: Any) -> str:
    return json.dumps(_sanitize(value), ensure_ascii=False, allow_nan=False)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return [{key: _json_default(value) for key, value in row.items()} for row in frame.to_dict(orient="records")]


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return _json_default(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
        return None if not math.isfinite(value) else value
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return pd.to_datetime(value).date()


def _date_string(value: Any) -> str | None:
    parsed = _to_date(value)
    return None if parsed is None else parsed.isoformat()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
