from __future__ import annotations

from datetime import timedelta

import pandas as pd

DART_TRAINING_FEATURE_COLUMNS = [
    "dart_disclosure_count_1d",
    "dart_disclosure_count_7d",
    "dart_disclosure_count_30d",
    "dart_material_count_7d",
    "dart_material_count_30d",
    "dart_report_diversity_30d",
]

DART_DISCLOSURE_DEFAULTS = {column: 0.0 for column in DART_TRAINING_FEATURE_COLUMNS}

MATERIAL_REPORT_KEYWORDS = (
    "주요사항보고서",
    "유상증자",
    "무상증자",
    "합병",
    "분할",
    "영업양수도",
    "소송",
    "횡령",
    "배임",
    "감자",
    "자기주식",
    "공개매수",
)


def attach_dart_disclosure_features(
    features: pd.DataFrame,
    dart_disclosures: pd.DataFrame | None,
) -> pd.DataFrame:
    if features.empty:
        return features

    output = features.copy()
    output["date"] = pd.to_datetime(output["date"], errors="coerce").dt.date
    output["symbol"] = output["symbol"].astype(str).str.zfill(6)

    if dart_disclosures is None or dart_disclosures.empty:
        return _fill_defaults(output)

    disclosures = dart_disclosures.copy()
    disclosures["receipt_date"] = pd.to_datetime(disclosures.get("receipt_date"), errors="coerce").dt.date
    disclosures["symbol"] = disclosures.get("symbol", pd.Series(dtype="object")).fillna("").astype(str).str.zfill(6)
    disclosures = disclosures[disclosures["receipt_date"].notna() & disclosures["symbol"].ne("000000")]
    if disclosures.empty:
        return _fill_defaults(output)

    disclosures["report_name"] = disclosures.get("report_name", pd.Series(dtype="object")).fillna("").astype(str)
    disclosures["is_material"] = disclosures["report_name"].map(_is_material_report)

    frames = []
    for symbol, symbol_features in output[["date", "symbol"]].drop_duplicates().groupby("symbol"):
        symbol_dates = sorted(symbol_features["date"].dropna().unique())
        if not symbol_dates:
            continue
        symbol_disclosures = disclosures[disclosures["symbol"].eq(symbol)]
        frames.append(_aggregate_symbol_features(symbol, symbol_dates, symbol_disclosures))

    if frames:
        aggregated = pd.concat(frames, ignore_index=True)
        output = output.merge(aggregated, on=["date", "symbol"], how="left")

    return _fill_defaults(output)


def _aggregate_symbol_features(symbol: str, feature_dates: list, disclosures: pd.DataFrame) -> pd.DataFrame:
    start = min(feature_dates) - timedelta(days=29)
    end = max(feature_dates)
    calendar = pd.DataFrame({"date": pd.date_range(start, end, freq="D").date})
    calendar["symbol"] = symbol

    if disclosures.empty:
        daily = calendar.assign(disclosure_count=0.0, material_count=0.0)
    else:
        counts = (
            disclosures.groupby("receipt_date", as_index=False)
            .agg(disclosure_count=("receipt_no", "count"), material_count=("is_material", "sum"))
            .rename(columns={"receipt_date": "date"})
        )
        daily = calendar.merge(counts, on="date", how="left")
        daily[["disclosure_count", "material_count"]] = daily[["disclosure_count", "material_count"]].fillna(0.0)

    daily["dart_disclosure_count_1d"] = daily["disclosure_count"]
    daily["dart_disclosure_count_7d"] = daily["disclosure_count"].rolling(7, min_periods=1).sum()
    daily["dart_disclosure_count_30d"] = daily["disclosure_count"].rolling(30, min_periods=1).sum()
    daily["dart_material_count_7d"] = daily["material_count"].rolling(7, min_periods=1).sum()
    daily["dart_material_count_30d"] = daily["material_count"].rolling(30, min_periods=1).sum()
    daily["dart_report_diversity_30d"] = _rolling_report_diversity(daily["date"].tolist(), disclosures)

    return daily[daily["date"].isin(feature_dates)][["date", "symbol", *DART_TRAINING_FEATURE_COLUMNS]]


def _rolling_report_diversity(dates: list, disclosures: pd.DataFrame) -> list[float]:
    if disclosures.empty:
        return [0.0 for _ in dates]
    values = []
    for current_date in dates:
        window_start = current_date - timedelta(days=29)
        window = disclosures[
            (disclosures["receipt_date"] >= window_start)
            & (disclosures["receipt_date"] <= current_date)
            & disclosures["report_name"].ne("")
        ]
        values.append(float(window["report_name"].nunique()))
    return values


def _is_material_report(report_name: str) -> bool:
    return any(keyword in report_name for keyword in MATERIAL_REPORT_KEYWORDS)


def _fill_defaults(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column, default in DART_DISCLOSURE_DEFAULTS.items():
        if column not in output.columns:
            output[column] = default
        output[column] = pd.to_numeric(output[column], errors="coerce").fillna(default)
    return output