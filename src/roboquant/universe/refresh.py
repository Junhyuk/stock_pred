from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pandas as pd

from roboquant.universe.providers.base import MarketCapItem, MarketDataProvider

EXCLUDED_SECURITY_TYPES = {"ETF", "ETN", "SPAC", "PREFERRED"}
SUPPORTED_MARKETS = ("KOSPI", "KOSDAQ")


class UniverseRefreshError(RuntimeError):
    """Raised when a refresh cannot produce a complete prediction universe."""


@dataclass(frozen=True)
class RefreshSettings:
    fetch_limit_per_market: int = 100
    validation_price_days: int = 90
    min_listing_trading_days: int = 120
    max_missing_ratio_60d: float = 0.10
    max_latest_price_gap_days: int = 14
    kospi_target: int = 30
    kosdaq_target: int = 20


@dataclass(frozen=True)
class _MarketSelection:
    market: str
    raw_rows: list[dict]
    prediction_rows: list[dict]
    raw_count: int
    selected_count: int
    excluded_count: int


def refresh_prediction_universe(
    conn,
    provider: MarketDataProvider,
    *,
    snapshot_date: date,
    universe_rule: str,
    settings: RefreshSettings | None = None,
) -> dict[str, int | str]:
    """Refresh the v8 prediction universe with transactional snapshot replacement."""

    settings = settings or RefreshSettings()
    started_at = datetime.now(UTC)
    provider_name = provider.provider_name
    run_id = f"refresh-{snapshot_date:%Y%m%d}-{uuid4().hex[:12]}"

    try:
        selections = [
            _select_market(
                conn,
                provider,
                market=market,
                snapshot_date=snapshot_date,
                universe_rule=universe_rule,
                created_at=started_at,
                settings=settings,
            )
            for market in SUPPORTED_MARKETS
        ]
        _validate_complete_selection(selections, settings)
        _commit_refresh(
            conn,
            run_id=run_id,
            snapshot_date=snapshot_date,
            universe_rule=universe_rule,
            provider_name=provider_name,
            started_at=started_at,
            selections=selections,
        )
    except Exception as exc:
        _record_failed_run(
            conn,
            run_id=run_id,
            snapshot_date=snapshot_date,
            universe_rule=universe_rule,
            provider_name=provider_name,
            started_at=started_at,
            error_message=str(exc),
        )
        if isinstance(exc, UniverseRefreshError):
            raise
        raise UniverseRefreshError(str(exc)) from exc

    return {
        "run_id": run_id,
        "snapshot_date": str(snapshot_date),
        "universe_rule": universe_rule,
        "raw_count": sum(selection.raw_count for selection in selections),
        "prediction_count": sum(selection.selected_count for selection in selections),
        "kospi_count": _selection_by_market(selections, "KOSPI").selected_count,
        "kosdaq_count": _selection_by_market(selections, "KOSDAQ").selected_count,
        "provider": provider_name,
    }


def _select_market(
    conn,
    provider: MarketDataProvider,
    *,
    market: str,
    snapshot_date: date,
    universe_rule: str,
    created_at: datetime,
    settings: RefreshSettings,
) -> _MarketSelection:
    candidates = provider.get_market_cap_ranking(
        snapshot_date,
        market,
        settings.fetch_limit_per_market,
    )
    if not candidates:
        raise UniverseRefreshError(f"{market} provider returned no market-cap candidates")

    target_count = _target_count(settings, market)
    raw_rows: list[dict] = []
    prediction_rows: list[dict] = []

    for item in candidates:
        exclusion_reason = _static_exclusion_reason(item)
        if exclusion_reason is None and len(prediction_rows) >= target_count:
            exclusion_reason = "not_selected_after_target_filled"
        if exclusion_reason is None:
            exclusion_reason = _price_exclusion_reason(
                conn,
                provider,
                item.symbol,
                snapshot_date,
                settings,
            )

        raw_rows.append(
            {
                "snapshot_date": snapshot_date,
                "symbol": item.symbol,
                "name": item.name,
                "market": market,
                "raw_market_cap_rank": item.raw_rank,
                "market_cap": item.market_cap,
                "security_type": item.security_type,
                "is_suspended": item.is_suspended,
                "listing_date": item.listing_date,
                "provider": provider.provider_name,
                "universe_rule": universe_rule,
                "exclusion_reason": exclusion_reason,
                "created_at": created_at,
            }
        )

        if exclusion_reason is None:
            prediction_rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "symbol": item.symbol,
                    "name": item.name,
                    "market": market,
                    "raw_market_cap_rank": item.raw_rank,
                    "prediction_rank": len(prediction_rows) + 1,
                    "market_cap": item.market_cap,
                    "security_type": item.security_type,
                    "provider": provider.provider_name,
                    "universe_rule": universe_rule,
                    "is_enabled": True,
                    "exclusion_reason": None,
                    "created_at": created_at,
                }
            )

    return _MarketSelection(
        market=market,
        raw_rows=raw_rows,
        prediction_rows=prediction_rows,
        raw_count=len(raw_rows),
        selected_count=len(prediction_rows),
        excluded_count=len(raw_rows) - len(prediction_rows),
    )


def _static_exclusion_reason(item: MarketCapItem) -> str | None:
    security_type = str(item.security_type or "COMMON").upper()
    if security_type in EXCLUDED_SECURITY_TYPES:
        return f"excluded security_type={security_type}"
    if item.is_suspended:
        return "excluded suspended"
    return None


def _price_exclusion_reason(
    conn,
    provider: MarketDataProvider,
    symbol: str,
    snapshot_date: date,
    settings: RefreshSettings,
) -> str | None:
    history = _load_validation_history(conn, provider, symbol, snapshot_date, settings)
    if history.empty:
        return "excluded price_history_missing"

    history = history.copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce").dt.date
    history = history.dropna(subset=["date"]).sort_values("date")
    history = history[history["date"] <= snapshot_date]
    if history.empty:
        return "excluded price_history_missing"

    latest_date = max(history["date"])
    latest_gap_days = (snapshot_date - latest_date).days
    if latest_gap_days > settings.max_latest_price_gap_days:
        return f"excluded latest_price_gap_days={latest_gap_days}"

    trading_days = len(history.drop_duplicates("date"))
    if trading_days < settings.min_listing_trading_days:
        return f"excluded insufficient_listing_history={trading_days}"

    recent_count = len(history.drop_duplicates("date").tail(60))
    missing_ratio = max(0.0, (60 - recent_count) / 60)
    if missing_ratio > settings.max_missing_ratio_60d:
        return f"excluded recent_price_missing_ratio={missing_ratio:.3f}"

    return None


def _load_validation_history(
    conn,
    provider: MarketDataProvider,
    symbol: str,
    snapshot_date: date,
    settings: RefreshSettings,
) -> pd.DataFrame:
    start_date = snapshot_date - timedelta(
        days=max(settings.validation_price_days * 2, settings.min_listing_trading_days * 2)
    )
    existing = conn.execute(
        """
        SELECT date, symbol, open, high, low, close, adj_close, volume, trading_value, market_cap,
               source, collected_at
        FROM prices_daily
        WHERE symbol = ? AND date BETWEEN ? AND ?
        ORDER BY date
        """,
        [str(symbol).zfill(6), start_date, snapshot_date],
    ).fetchdf()
    if _has_enough_recent_history(existing, snapshot_date, settings):
        return existing
    return provider.get_price_history(str(symbol).zfill(6), start_date, snapshot_date)


def _has_enough_recent_history(
    history: pd.DataFrame,
    snapshot_date: date,
    settings: RefreshSettings,
) -> bool:
    if history.empty or "date" not in history.columns:
        return False
    dates = pd.to_datetime(history["date"], errors="coerce").dropna().dt.date
    dates = dates[dates <= snapshot_date].drop_duplicates().sort_values()
    if len(dates) < settings.min_listing_trading_days:
        return False
    latest_gap_days = (snapshot_date - max(dates)).days
    return latest_gap_days <= settings.max_latest_price_gap_days


def _validate_complete_selection(
    selections: list[_MarketSelection],
    settings: RefreshSettings,
) -> None:
    for market in SUPPORTED_MARKETS:
        selection = _selection_by_market(selections, market)
        expected = _target_count(settings, market)
        if selection.selected_count != expected:
            raise UniverseRefreshError(
                f"{market} selected {selection.selected_count}/{expected}; snapshot not updated"
            )


def _commit_refresh(
    conn,
    *,
    run_id: str,
    snapshot_date: date,
    universe_rule: str,
    provider_name: str,
    started_at: datetime,
    selections: list[_MarketSelection],
) -> None:
    raw = pd.DataFrame([row for selection in selections for row in selection.raw_rows])
    prediction = pd.DataFrame(
        [row for selection in selections for row in selection.prediction_rows]
    )
    symbols = _symbol_frame(raw, started_at)
    completed_at = datetime.now(UTC)

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "DELETE FROM raw_market_cap_snapshot WHERE snapshot_date = ? AND universe_rule = ?",
            [snapshot_date, universe_rule],
        )
        conn.execute(
            """
            DELETE FROM prediction_universe_snapshot
            WHERE snapshot_date = ? AND universe_rule = ?
            """,
            [snapshot_date, universe_rule],
        )
        _insert_frame(conn, "raw_market_cap_snapshot", raw)
        _insert_frame(conn, "prediction_universe_snapshot", prediction)
        _insert_missing_symbols(conn, symbols)
        conn.execute(
            """
            INSERT INTO universe_refresh_runs (
              run_id,
              snapshot_date,
              universe_rule,
              provider,
              status,
              kospi_raw_count,
              kosdaq_raw_count,
              kospi_selected_count,
              kosdaq_selected_count,
              kospi_excluded_count,
              kosdaq_excluded_count,
              started_at,
              completed_at
            )
            VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                snapshot_date,
                universe_rule,
                provider_name,
                _selection_by_market(selections, "KOSPI").raw_count,
                _selection_by_market(selections, "KOSDAQ").raw_count,
                _selection_by_market(selections, "KOSPI").selected_count,
                _selection_by_market(selections, "KOSDAQ").selected_count,
                _selection_by_market(selections, "KOSPI").excluded_count,
                _selection_by_market(selections, "KOSDAQ").excluded_count,
                started_at,
                completed_at,
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _record_failed_run(
    conn,
    *,
    run_id: str,
    snapshot_date: date,
    universe_rule: str,
    provider_name: str,
    started_at: datetime,
    error_message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO universe_refresh_runs (
          run_id,
          snapshot_date,
          universe_rule,
          provider,
          status,
          started_at,
          completed_at,
          error_message
        )
        VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)
        """,
        [
            run_id,
            snapshot_date,
            universe_rule,
            provider_name,
            started_at,
            datetime.now(UTC),
            error_message[:2000],
        ],
    )


def _insert_frame(conn, table: str, frame: pd.DataFrame) -> None:
    temp_name = f"refresh_{table}_{uuid4().hex}"
    conn.register(temp_name, frame)
    try:
        columns = ", ".join(frame.columns)
        conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temp_name}")
    finally:
        conn.unregister(temp_name)


def _symbol_frame(raw: pd.DataFrame, collected_at: datetime) -> pd.DataFrame:
    return raw[["symbol", "name", "market", "listing_date"]].assign(
        is_active=True,
        collected_at=collected_at,
    )


def _insert_missing_symbols(conn, frame: pd.DataFrame) -> None:
    temp_name = f"refresh_symbols_{uuid4().hex}"
    conn.register(temp_name, frame)
    try:
        conn.execute(
            f"""
            INSERT INTO symbols (symbol, name, market, listing_date, is_active, collected_at)
            SELECT
              refresh.symbol,
              refresh.name,
              refresh.market,
              refresh.listing_date,
              refresh.is_active,
              refresh.collected_at
            FROM {temp_name} AS refresh
            WHERE NOT EXISTS (
              SELECT 1 FROM symbols AS existing WHERE existing.symbol = refresh.symbol
            )
            """
        )
    finally:
        conn.unregister(temp_name)


def _selection_by_market(selections: list[_MarketSelection], market: str) -> _MarketSelection:
    for selection in selections:
        if selection.market == market:
            return selection
    raise KeyError(market)


def _target_count(settings: RefreshSettings, market: str) -> int:
    if market == "KOSPI":
        return settings.kospi_target
    if market == "KOSDAQ":
        return settings.kosdaq_target
    raise ValueError(f"Unsupported market: {market}")
