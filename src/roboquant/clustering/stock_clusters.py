from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

from roboquant.db import append_dedup_table

CLUSTER_FEATURES = [
    "ret_21d",
    "ret_63d",
    "ret_126d",
    "momentum_score",
    "volatility_60d",
    "liquidity_score",
    "risk_score",
    "market_cap_score",
]


def build_stock_clusters(
    features: pd.DataFrame,
    horizon: str = "3M",
    n_clusters: int = 5,
    min_symbols: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = features[features["horizon"].eq(horizon)].copy()
    if frame.empty:
        raise ValueError(f"No features available for clustering horizon={horizon}")
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    asof_date = frame["date"].max()
    frame = frame[frame["date"].eq(asof_date)].drop_duplicates("symbol").copy()
    if len(frame) < min_symbols:
        raise ValueError(f"Clustering requires at least {min_symbols} symbols; found {len(frame)}")

    for column in CLUSTER_FEATURES:
        if column not in frame.columns:
            frame[column] = 0.5 if column.endswith("_score") else np.nan
        values = pd.to_numeric(frame[column], errors="coerce")
        median = values.median()
        frame[column] = values.fillna(0.5 if pd.isna(median) else median)

    scaler = StandardScaler()
    matrix = scaler.fit_transform(frame[CLUSTER_FEATURES])
    model = KMeans(n_clusters=int(n_clusters), random_state=42, n_init=20)
    cluster_ids = model.fit_predict(matrix)
    distances = np.linalg.norm(matrix - model.cluster_centers_[cluster_ids], axis=1)
    labels = _cluster_labels(model.cluster_centers_, scaler)
    version = f"kmeans-{n_clusters}-{asof_date}"
    now = datetime.now(UTC).replace(tzinfo=None)

    assignments = pd.DataFrame(
        {
            "asof_date": asof_date,
            "horizon": horizon,
            "symbol": frame["symbol"].astype(str).str.zfill(6).to_numpy(),
            "cluster_id": cluster_ids,
            "cluster_label": [labels[int(cluster_id)] for cluster_id in cluster_ids],
            "distance_to_centroid": distances,
            "feature_values_json": [
                json.dumps(
                    {column: _float(row[column]) for column in CLUSTER_FEATURES},
                    ensure_ascii=False,
                )
                for _, row in frame.iterrows()
            ],
            "model_version": version,
            "created_at": now,
        }
    )

    summaries = []
    for cluster_id in range(int(n_clusters)):
        members = assignments[assignments["cluster_id"].eq(cluster_id)].sort_values(
            "distance_to_centroid"
        )
        centroid = scaler.inverse_transform(model.cluster_centers_[[cluster_id]])[0]
        summaries.append(
            {
                "asof_date": asof_date,
                "horizon": horizon,
                "cluster_id": cluster_id,
                "cluster_label": labels[cluster_id],
                "member_count": int(len(members)),
                "centroid_json": json.dumps(
                    dict(zip(CLUSTER_FEATURES, map(_float, centroid), strict=True)),
                    ensure_ascii=False,
                ),
                "top_symbols_json": json.dumps(members["symbol"].head(10).tolist()),
                "model_version": version,
                "created_at": now,
            }
        )
    return assignments.sort_values(["cluster_id", "distance_to_centroid"]), pd.DataFrame(summaries)


def persist_stock_clusters(conn, assignments: pd.DataFrame, summaries: pd.DataFrame) -> None:
    append_dedup_table(
        conn,
        "stock_clusters",
        assignments,
        ["asof_date", "horizon", "symbol"],
    )
    append_dedup_table(
        conn,
        "cluster_summary",
        summaries,
        ["asof_date", "horizon", "cluster_id"],
    )


def _cluster_labels(centers: np.ndarray, scaler: StandardScaler) -> dict[int, str]:
    raw = pd.DataFrame(
        scaler.inverse_transform(centers),
        columns=CLUSTER_FEATURES,
    )
    labels: dict[int, str] = {}
    for cluster_id, row in raw.iterrows():
        if row["momentum_score"] >= raw["momentum_score"].quantile(0.75):
            label = "고모멘텀"
        elif (
            row["volatility_60d"] <= raw["volatility_60d"].quantile(0.35)
            and row["market_cap_score"] >= raw["market_cap_score"].median()
        ):
            label = "저변동·대형주"
        elif row["liquidity_score"] >= raw["liquidity_score"].quantile(0.75):
            label = "고유동성"
        elif row["ret_21d"] > row["ret_126d"]:
            label = "반등 후보"
        else:
            label = "중립 혼합"
        labels[int(cluster_id)] = label
    return labels


def _float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)
