"""Sequential-7 research run: 3-year OOS, champion, live 1D/2D, GitHub Pages payload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from roboquant.index_forecast.export import export_duckdb_history_to_sqlite
from roboquant.index_forecast.indexes import FORECAST_HORIZONS, RESEARCH_INDEXES, get_official_index
from roboquant.index_forecast.ingest import ingest_official_index
from roboquant.index_forecast.report import build_github_pages_payload, write_github_pages_report
from roboquant.index_forecast.sqlite_store import PRODUCTION_WEIGHT
from roboquant.index_forecast.walkforward import MODEL_TYPES, run_index_forecast_walkforward


def run_index_forecast_research(
    sqlite_conn,
    *,
    duckdb_conn=None,
    indexes: tuple[str, ...] = RESEARCH_INDEXES,
    horizon_days: tuple[int, ...] = FORECAST_HORIZONS,
    eval_years: int = 3,
    ingest_official: bool = False,
    ingest_start: str = "2019-01-01",
    skip_export: bool = False,
    persist: bool = True,
    model_types: tuple[str, ...] | None = None,
    initial_train_days: int = 252,
    retrain_step_days: int = 21,
    min_oos_rows: int = 60,
    pages_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the Sequential-7 research cycle without changing production Ridge weights."""

    export_report = None
    if duckdb_conn is not None and not skip_export:
        export_report = export_duckdb_history_to_sqlite(duckdb_conn, sqlite_conn)
    ingest_reports: dict[str, Any] = {}
    if ingest_official:
        for index_code in indexes:
            ingest_reports[index_code] = ingest_official_index(
                index_code,
                start_date=ingest_start,
                duckdb_conn=None,
                sqlite_conn=sqlite_conn,
            )

    selected_models = tuple(model_types) if model_types else MODEL_TYPES
    slots: dict[str, Any] = {}
    for index_code in indexes:
        spec = get_official_index(index_code)
        for days in horizon_days:
            key = f"{index_code}:{days}D"
            slots[key] = run_index_forecast_walkforward(
                sqlite_conn,
                index_code=index_code,
                horizon_days=days,
                initial_train_days=initial_train_days,
                retrain_step_days=retrain_step_days,
                min_oos_rows=min_oos_rows,
                eval_years=eval_years,
                persist=persist,
                model_types=selected_models,
                include_live_forecast=True,
            )
            slots[key]["display_name"] = spec.display_name
            slots[key]["korean_name"] = spec.korean_name
            slots[key]["pykrx_code"] = spec.pykrx_code

    payload = build_github_pages_payload(
        slots,
        eval_years=eval_years,
        ingest=ingest_reports,
        export=export_report,
        model_types=selected_models,
    )
    pages_path = None
    if pages_dir is not None:
        pages_path = str(write_github_pages_report(pages_dir, payload))
    return {
        "status": payload["status"],
        "production_weight": PRODUCTION_WEIGHT,
        "eval_years": eval_years,
        "ingest": ingest_reports,
        "export": export_report,
        "slots": slots,
        "pages": pages_path,
        "report": payload,
    }
