#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.official_sources import (
    OfficialDataConfigurationError,
    collect_dart_disclosures,
)
from roboquant.db import append_dedup_table, connect_database, table_exists
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Top50 OpenDART disclosure metadata.")
    parser.add_argument("--config", default="configs/official_data_sources.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--universe-rule", default="prediction_top_market_cap")
    parser.add_argument("--allow-missing-key", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    target_date = _parse_date(args.date)
    conn = connect_database(get_database_path(config))
    try:
        symbols = _current_symbols(conn, args.universe_rule)
        days = int(args.days if args.days is not None else config.get("official_news", {}).get("dart", {}).get("days", 7))
        frame = collect_dart_disclosures(
            config=config.get("official_news", {}),
            symbols=symbols,
            days=days,
            target_date=target_date,
        )
        append_dedup_table(conn, "dart_disclosures", frame, ["disclosure_id"])
        print(f"dart_disclosures rows: {len(frame)}")
    except OfficialDataConfigurationError as exc:
        _record_failure(conn, exc, target_date)
        if args.allow_missing_key:
            print(f"OpenDART skipped without fake data: {exc}")
            return
        raise
    except Exception as exc:
        _record_failure(conn, exc, target_date)
        raise
    finally:
        conn.close()


def _current_symbols(conn, universe_rule: str) -> list[str]:
    if not table_exists(conn, "current_prediction_universe"):
        return []
    frame = conn.execute(
        """
        SELECT symbol
        FROM current_prediction_universe
        WHERE universe_rule = ?
          AND is_enabled = TRUE
        ORDER BY market, prediction_rank, symbol
        """,
        [universe_rule],
    ).fetchdf()
    return [str(value).zfill(6) for value in frame["symbol"].tolist()]


def _record_failure(conn, error: Exception, target_date) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row("collect_dart_disclosures", "opendart", error, target_date=target_date.isoformat()),
        ["collected_at", "step", "source", "error_message"],
    )


def _parse_date(value: str):
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return datetime.fromisoformat(str(value)).date()


if __name__ == "__main__":
    main()
