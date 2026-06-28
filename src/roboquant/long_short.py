from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from roboquant.backtest.metrics import summarize_equity
from roboquant.reports.prompt_templates import validate_report_text

LONG_SHORT_DISCLAIMER = (
    "본 리포트는 시장중립 랭크 스프레드 모의 결과이며 실제 공매도 체결, "
    "대차 가능성, 세금, 슬리피지를 보장하지 않습니다. 투자 권유가 아니며 참고 신호입니다."
)


def build_long_short_recommendations(
    predictions: pd.DataFrame,
    features: pd.DataFrame | None = None,
    symbols: pd.DataFrame | None = None,
    universe: pd.DataFrame | None = None,
    horizon: str = "2M",
    config: dict[str, Any] | None = None,
    asof_date: str | None = None,
) -> pd.DataFrame:
    """Build a market-neutral simulated long-short portfolio from ranked predictions."""
    ls_cfg = _long_short_config(config)
    frame = _prepare_prediction_frame(predictions, horizon)
    if frame.empty:
        return _empty_recommendations()

    if asof_date and asof_date != "latest":
        selected_date = pd.to_datetime(asof_date).date()
    else:
        selected_date = pd.to_datetime(frame["asof_date"]).max().date()
    frame = frame[pd.to_datetime(frame["asof_date"]).dt.date == selected_date].copy()
    if frame.empty:
        return _empty_recommendations()

    frame = _merge_features(frame, features, horizon)
    frame = _merge_symbols(frame, symbols, universe=universe)
    frame = _ensure_scores(frame)
    eligible = _filter_liquidity(frame, ls_cfg)
    if eligible.empty:
        return _empty_recommendations()

    created_at = datetime.now(UTC).replace(tzinfo=None)
    if _market_split_enabled(ls_cfg):
        rows = _build_market_split_legs(eligible, ls_cfg, created_at=created_at)
    else:
        rows = _build_combined_legs(eligible, ls_cfg, created_at=created_at)
    if not rows:
        return _empty_recommendations()
    return pd.DataFrame(rows).sort_values(["market", "side", "leg_rank"]).reset_index(drop=True)


def run_long_short_backtest(
    predictions_with_returns: pd.DataFrame,
    horizon: str,
    config: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, float | int | None]]:
    """Backtest a simulated long-short rank spread using realized future returns."""
    ls_cfg = _long_short_config(config)
    frame = _prepare_prediction_frame(predictions_with_returns, horizon)
    required = {"future_return"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"long-short backtest input is missing columns: {sorted(missing)}")
    if frame.empty:
        return pd.DataFrame(), summarize_equity(pd.DataFrame())

    frame = _ensure_scores(frame)
    frame = frame.dropna(subset=["future_return", "long_score", "short_score"])
    if frame.empty:
        return pd.DataFrame(), summarize_equity(pd.DataFrame())

    rebalance_frequency = _rebalance_frequency(ls_cfg, horizon)
    selected_dates = _select_rebalance_dates(frame["asof_date"], rebalance_frequency)
    frame = frame[frame["asof_date"].isin(selected_dates)].copy()
    if frame.empty:
        return pd.DataFrame(), summarize_equity(pd.DataFrame())

    cost_rate = float(ls_cfg.get("transaction_cost_bps", 30.0)) / 10000.0
    created_at = datetime.now(UTC).replace(tzinfo=None)
    if _market_split_enabled(ls_cfg):
        rows = _run_market_split_backtest_rows(frame, horizon, ls_cfg, cost_rate, created_at)
    else:
        rows = _run_combined_backtest_rows(frame, horizon, ls_cfg, cost_rate, created_at)

    curve = pd.DataFrame(rows)
    summary = summarize_equity(curve)
    if not curve.empty:
        summary.update(
            {
                "final_equity": float(curve["equity"].iloc[-1]),
                "avg_long_return": float(curve["long_return"].mean()),
                "avg_short_return": float(curve["short_return"].mean()),
                "avg_spread_return": float(curve["gross_spread_return"].mean()),
                "long_count": int(ls_cfg.get("long_count", 10)),
                "short_count": int(ls_cfg.get("short_count", 10)),
                "market_split": _market_split_enabled(ls_cfg),
            }
        )
        metrics_json = json.dumps(summary, ensure_ascii=False, default=_json_default)
        curve["metrics_json"] = metrics_json
    return curve, summary


def render_long_short_report(recommendations: pd.DataFrame, horizon: str) -> str:
    if recommendations.empty:
        return validate_report_text(
            f"# Long-Short 관심종목 {horizon}\n\n생성된 롱숏 관심종목이 없습니다.\n\n> {LONG_SHORT_DISCLAIMER}\n"
        )

    asof_date = recommendations["asof_date"].iloc[0]
    gross_long = float(recommendations.loc[recommendations["weight"] > 0, "weight"].sum())
    gross_short = float(-recommendations.loc[recommendations["weight"] < 0, "weight"].sum())
    long_leg = recommendations[recommendations["side"] == "LONG"].sort_values(["market", "leg_rank"])
    short_leg = recommendations[recommendations["side"] == "SHORT"].sort_values(["market", "leg_rank"])
    expected_spread = gross_long * _mean(long_leg, "pred_return") - gross_short * _mean(
        short_leg, "pred_return"
    )
    confidence = _mean(recommendations, "confidence")
    split_mode = "market" if "market" in recommendations.columns and recommendations["market"].notna().any() else "combined"

    lines = [
        f"# Long-Short 관심종목 {horizon}",
        "",
        f"- 기준일: `{asof_date}`",
        "- 모드: `시장중립 랭크 스프레드 시뮬레이션`",
        f"- 분할: `{split_mode}`",
        f"- 예상 스프레드: `{expected_spread:.4f}`",
        f"- 평균 신뢰도: `{confidence:.4f}`",
    ]
    if split_mode == "market":
        for market in _ordered_markets(recommendations["market"]):
            market_frame = recommendations[recommendations["market"] == market]
            market_long = market_frame[market_frame["side"] == "LONG"].sort_values("leg_rank")
            market_short = market_frame[market_frame["side"] == "SHORT"].sort_values("leg_rank")
            lines += [
                "",
                f"## {market} Long Leg",
                "",
                "| Rank | Symbol | Name | Weight | Long Score | Pred Return | Risk Flags |",
                "|---:|---|---|---:|---:|---:|---|",
            ]
            lines.extend(_report_rows(market_long, "long_score"))
            lines += [
                "",
                f"## {market} Short Leg",
                "",
                "| Rank | Symbol | Name | Weight | Short Score | Pred Return | Risk Flags |",
                "|---:|---|---|---:|---:|---:|---|",
            ]
            lines.extend(_report_rows(market_short, "short_score"))
    else:
        lines += [
            "",
            "## Long Leg",
            "",
            "| Rank | Symbol | Name | Weight | Long Score | Pred Return | Risk Flags |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
        lines.extend(_report_rows(long_leg, "long_score"))
        lines += [
            "",
            "## Short Leg",
            "",
            "| Rank | Symbol | Name | Weight | Short Score | Pred Return | Risk Flags |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
        lines.extend(_report_rows(short_leg, "short_score"))
    lines += ["", f"> {LONG_SHORT_DISCLAIMER}", ""]
    return validate_report_text("\n".join(lines))


def _prepare_prediction_frame(predictions: pd.DataFrame, horizon: str) -> pd.DataFrame:
    if predictions.empty:
        return predictions.copy()
    frame = predictions.copy()
    frame["horizon"] = frame["horizon"].astype(str)
    frame = frame[frame["horizon"] == horizon].copy()
    if frame.empty:
        return frame
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame.sort_values(["asof_date", "symbol"]).reset_index(drop=True)


def _merge_features(frame: pd.DataFrame, features: pd.DataFrame | None, horizon: str) -> pd.DataFrame:
    if features is None or features.empty:
        return frame
    feature_frame = features.copy()
    feature_frame["date"] = pd.to_datetime(feature_frame["date"]).dt.date
    feature_frame["symbol"] = feature_frame["symbol"].astype(str).str.zfill(6)
    if "horizon" in feature_frame.columns:
        feature_frame["horizon"] = feature_frame["horizon"].astype(str)
        feature_frame = feature_frame[feature_frame["horizon"] == horizon].copy()
    columns = [
        column
        for column in [
            "date",
            "symbol",
            "horizon",
            "trading_value_ma20",
            "liquidity_score",
            "risk_score",
            "momentum_score",
            "supply_demand_score",
            "rsi_14",
        ]
        if column in feature_frame.columns
    ]
    if not columns:
        return frame
    return frame.merge(
        feature_frame[columns].rename(columns={"date": "asof_date"}),
        on=["asof_date", "symbol", "horizon"] if "horizon" in columns else ["asof_date", "symbol"],
        how="left",
    )


def _merge_symbols(
    frame: pd.DataFrame,
    symbols: pd.DataFrame | None,
    *,
    universe: pd.DataFrame | None = None,
) -> pd.DataFrame:
    metadata = _symbol_metadata(symbols, universe)
    if metadata.empty:
        return frame
    return frame.merge(metadata, on="symbol", how="left")


def _symbol_metadata(
    symbols: pd.DataFrame | None,
    universe: pd.DataFrame | None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if universe is not None and not universe.empty:
        universe_frame = universe.copy()
        universe_frame["symbol"] = universe_frame["symbol"].astype(str).str.zfill(6)
        columns = [column for column in ["symbol", "name", "market"] if column in universe_frame]
        if columns:
            frames.append(universe_frame[columns].drop_duplicates("symbol"))
    if symbols is not None and not symbols.empty:
        symbol_frame = symbols.copy()
        symbol_frame["symbol"] = symbol_frame["symbol"].astype(str).str.zfill(6)
        columns = [column for column in ["symbol", "name", "market", "sector"] if column in symbol_frame]
        if columns:
            frames.append(symbol_frame[columns].drop_duplicates("symbol"))
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined["symbol"] = combined["symbol"].astype(str).str.zfill(6)
    return combined.drop_duplicates("symbol", keep="last")


def _ensure_scores(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["pred_return"] = _numeric(out, "pred_return", 0.0)
    out["pred_prob_top20"] = _numeric(out, "pred_prob_top20", 0.5).clip(0.0, 1.0)
    if "pred_prob_bottom20" not in out.columns:
        out["pred_prob_bottom20"] = 1.0 - out["pred_prob_top20"]
    out["pred_prob_bottom20"] = _numeric(out, "pred_prob_bottom20", 0.5).clip(0.0, 1.0)
    if "market" in out.columns and out["market"].notna().any():
        pred_rank = out.groupby(["asof_date", "market"])["pred_return"].rank(pct=True).fillna(0.5)
    else:
        pred_rank = out.groupby("asof_date")["pred_return"].rank(pct=True).fillna(0.5)
    if "long_score" not in out.columns:
        out["long_score"] = np.nan
    if "short_score" not in out.columns:
        out["short_score"] = np.nan
    out["long_score"] = _numeric(out, "long_score", np.nan)
    out["short_score"] = _numeric(out, "short_score", np.nan)
    out["long_score"] = out["long_score"].fillna(
        0.6 * out["pred_prob_top20"] + 0.4 * pred_rank
    )
    out["short_score"] = out["short_score"].fillna(
        0.6 * out["pred_prob_bottom20"] + 0.4 * (1.0 - pred_rank)
    )
    out["long_score"] = out["long_score"].clip(0.0, 1.0)
    out["short_score"] = out["short_score"].clip(0.0, 1.0)
    if "confidence" not in out.columns:
        out["confidence"] = np.maximum(out["pred_prob_top20"], out["pred_prob_bottom20"])
    out["confidence"] = _numeric(out, "confidence", 0.5).clip(0.0, 1.0)
    if "risk_score" not in out.columns:
        out["risk_score"] = out.get("pred_risk", 0.5)
    out["risk_score"] = pd.to_numeric(out["risk_score"], errors="coerce").fillna(0.5)
    return out


def _filter_liquidity(frame: pd.DataFrame, ls_cfg: dict[str, Any]) -> pd.DataFrame:
    if "trading_value_ma20" not in frame.columns:
        return frame.copy()
    threshold = float(ls_cfg.get("min_trading_value_20d", 0) or 0)
    if threshold <= 0:
        return frame.copy()
    trading_value = pd.to_numeric(frame["trading_value_ma20"], errors="coerce")
    return frame[trading_value.fillna(0.0) >= threshold].copy()


def _leg_rows(
    leg: pd.DataFrame,
    side: str,
    weight: float,
    created_at: datetime,
) -> list[dict[str, object]]:
    rows = []
    score_column = "long_score" if side == "LONG" else "short_score"
    for rank, (_, row) in enumerate(leg.iterrows(), start=1):
        rows.append(
            {
                "asof_date": row["asof_date"],
                "horizon": row["horizon"],
                "symbol": row["symbol"],
                "side": side,
                "leg_rank": rank,
                "long_score": float(row.get("long_score", 0.0)),
                "short_score": float(row.get("short_score", 0.0)),
                "pred_return": float(row.get("pred_return", 0.0)),
                "pred_prob_top20": float(row.get("pred_prob_top20", 0.5)),
                "pred_prob_bottom20": float(row.get("pred_prob_bottom20", 0.5)),
                "risk_score": float(row.get("risk_score", row.get("pred_risk", 0.5))),
                "confidence": float(row.get("confidence", 0.5)),
                "weight": float(weight),
                "reason_json": json.dumps(_reasons(row, side, score_column), ensure_ascii=False),
                "risk_flags_json": json.dumps(_risk_flags(row, side), ensure_ascii=False),
                "model_version": row.get("model_version"),
                "created_at": created_at,
                "name": row.get("name"),
                "market": row.get("market"),
                "sector": row.get("sector"),
            }
        )
    return rows


def _market_split_enabled(ls_cfg: dict[str, Any]) -> bool:
    split = ls_cfg.get("market_split", {})
    return bool(split.get("enabled", False))


def _market_leg_counts(ls_cfg: dict[str, Any]) -> dict[str, dict[str, int]]:
    split = ls_cfg.get("market_split", {})
    kospi_target = int(split.get("kospi_target", 30))
    kosdaq_target = int(split.get("kosdaq_target", 20))
    total = max(1, kospi_target + kosdaq_target)
    long_total = int(ls_cfg.get("long_count", 10))
    short_total = int(ls_cfg.get("short_count", 10))
    kospi_long = round(long_total * kospi_target / total)
    kosdaq_long = max(0, long_total - kospi_long)
    kospi_short = round(short_total * kospi_target / total)
    kosdaq_short = max(0, short_total - kospi_short)
    return {
        "KOSPI": {"long": kospi_long, "short": kospi_short},
        "KOSDAQ": {"long": kosdaq_long, "short": kosdaq_short},
    }


def _market_gross_weights(ls_cfg: dict[str, Any], market: str) -> tuple[float, float]:
    split = ls_cfg.get("market_split", {})
    kospi_target = int(split.get("kospi_target", 30))
    kosdaq_target = int(split.get("kosdaq_target", 20))
    total = max(1, kospi_target + kosdaq_target)
    share = kospi_target / total if market == "KOSPI" else kosdaq_target / total
    gross_long = float(ls_cfg.get("gross_long", 0.5)) * share
    gross_short = float(ls_cfg.get("gross_short", 0.5)) * share
    return gross_long, gross_short


def _select_legs(
    eligible: pd.DataFrame,
    *,
    long_count: int,
    short_count: int,
    gross_long: float,
    gross_short: float,
    created_at: datetime,
    market: str | None = None,
) -> list[dict[str, object]]:
    if eligible.empty or long_count <= 0 or short_count <= 0:
        return []
    long_leg = (
        eligible.sort_values(["long_score", "confidence"], ascending=[False, False])
        .head(long_count)
        .copy()
    )
    short_candidates = eligible[~eligible["symbol"].isin(long_leg["symbol"])].copy()
    short_leg = (
        short_candidates.sort_values(["short_score", "confidence"], ascending=[False, False])
        .head(short_count)
        .copy()
    )
    if long_leg.empty or short_leg.empty:
        return []
    if market is not None:
        long_leg["market"] = market
        short_leg["market"] = market
    rows: list[dict[str, object]] = []
    rows.extend(
        _leg_rows(
            long_leg,
            side="LONG",
            weight=gross_long / max(len(long_leg), 1),
            created_at=created_at,
        )
    )
    rows.extend(
        _leg_rows(
            short_leg,
            side="SHORT",
            weight=-gross_short / max(len(short_leg), 1),
            created_at=created_at,
        )
    )
    return rows


def _build_combined_legs(
    eligible: pd.DataFrame,
    ls_cfg: dict[str, Any],
    *,
    created_at: datetime,
) -> list[dict[str, object]]:
    long_count = int(ls_cfg.get("long_count", 10))
    short_count = int(ls_cfg.get("short_count", 10))
    gross_long = float(ls_cfg.get("gross_long", 0.5))
    gross_short = float(ls_cfg.get("gross_short", 0.5))
    return _select_legs(
        eligible,
        long_count=long_count,
        short_count=short_count,
        gross_long=gross_long,
        gross_short=gross_short,
        created_at=created_at,
    )


def _build_market_split_legs(
    eligible: pd.DataFrame,
    ls_cfg: dict[str, Any],
    *,
    created_at: datetime,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for market, counts in _market_leg_counts(ls_cfg).items():
        market_frame = eligible[eligible["market"].astype(str).eq(market)].copy()
        gross_long, gross_short = _market_gross_weights(ls_cfg, market)
        rows.extend(
            _select_legs(
                market_frame,
                long_count=counts["long"],
                short_count=counts["short"],
                gross_long=gross_long,
                gross_short=gross_short,
                created_at=created_at,
                market=market,
            )
        )
    return rows


def _run_combined_backtest_rows(
    frame: pd.DataFrame,
    horizon: str,
    ls_cfg: dict[str, Any],
    cost_rate: float,
    created_at: datetime,
) -> list[dict[str, object]]:
    long_count = int(ls_cfg.get("long_count", 10))
    short_count = int(ls_cfg.get("short_count", 10))
    gross_long = float(ls_cfg.get("gross_long", 0.5))
    gross_short = float(ls_cfg.get("gross_short", 0.5))
    rebalance_frequency = _rebalance_frequency(ls_cfg, horizon)
    selected_dates = _select_rebalance_dates(frame["asof_date"], rebalance_frequency)
    filtered = frame[frame["asof_date"].isin(selected_dates)].copy()

    rows: list[dict[str, object]] = []
    previous_positions: set[str] | None = None
    equity = 1.0
    for asof, group in filtered.groupby("asof_date", sort=True):
        eligible = _filter_liquidity(group.copy(), ls_cfg)
        legs = _select_legs(
            eligible,
            long_count=long_count,
            short_count=short_count,
            gross_long=gross_long,
            gross_short=gross_short,
            created_at=created_at,
        )
        if not legs:
            continue
        leg_frame = pd.DataFrame(legs).merge(
            eligible[["symbol", "future_return"]],
            on="symbol",
            how="left",
        )
        long_leg = leg_frame[leg_frame["side"] == "LONG"]
        short_leg = leg_frame[leg_frame["side"] == "SHORT"]
        row, previous_positions, equity = _backtest_step_row(
            asof=asof,
            horizon=horizon,
            market=None,
            long_leg=long_leg,
            short_leg=short_leg,
            gross_long=gross_long,
            gross_short=gross_short,
            cost_rate=cost_rate,
            previous_positions=previous_positions,
            equity=equity,
            model_version=_model_version(group),
            created_at=created_at,
        )
        if row is not None:
            rows.append(row)
    return rows


def _run_market_split_backtest_rows(
    frame: pd.DataFrame,
    horizon: str,
    ls_cfg: dict[str, Any],
    cost_rate: float,
    created_at: datetime,
) -> list[dict[str, object]]:
    rebalance_frequency = _rebalance_frequency(ls_cfg, horizon)
    selected_dates = _select_rebalance_dates(frame["asof_date"], rebalance_frequency)
    filtered = frame[frame["asof_date"].isin(selected_dates)].copy()
    leg_counts = _market_leg_counts(ls_cfg)

    rows: list[dict[str, object]] = []
    state: dict[str, tuple[set[str] | None, float]] = {
        market: (None, 1.0) for market in leg_counts
    }
    for asof, group in filtered.groupby("asof_date", sort=True):
        for market, counts in leg_counts.items():
            eligible = _filter_liquidity(
                group[group["market"].astype(str).eq(market)].copy(),
                ls_cfg,
            )
            gross_long, gross_short = _market_gross_weights(ls_cfg, market)
            legs = _select_legs(
                eligible,
                long_count=counts["long"],
                short_count=counts["short"],
                gross_long=gross_long,
                gross_short=gross_short,
                created_at=created_at,
                market=market,
            )
            if not legs:
                continue
            leg_frame = pd.DataFrame(legs).merge(
                eligible[["symbol", "future_return"]],
                on="symbol",
                how="left",
            )
            long_leg = leg_frame[leg_frame["side"] == "LONG"]
            short_leg = leg_frame[leg_frame["side"] == "SHORT"]
            previous_positions, equity = state[market]
            row, previous_positions, equity = _backtest_step_row(
                asof=asof,
                horizon=horizon,
                market=market,
                long_leg=long_leg,
                short_leg=short_leg,
                gross_long=gross_long,
                gross_short=gross_short,
                cost_rate=cost_rate,
                previous_positions=previous_positions,
                equity=equity,
                model_version=_model_version(group),
                created_at=created_at,
            )
            state[market] = (previous_positions, equity)
            if row is not None:
                rows.append(row)
    return rows


def _backtest_step_row(
    *,
    asof,
    horizon: str,
    market: str | None,
    long_leg: pd.DataFrame,
    short_leg: pd.DataFrame,
    gross_long: float,
    gross_short: float,
    cost_rate: float,
    previous_positions: set[str] | None,
    equity: float,
    model_version: str | None,
    created_at: datetime,
) -> tuple[dict[str, object] | None, set[str] | None, float]:
    if long_leg.empty or short_leg.empty:
        return None, previous_positions, equity
    current_positions = {
        *(f"L:{symbol}" for symbol in long_leg["symbol"].astype(str)),
        *(f"S:{symbol}" for symbol in short_leg["symbol"].astype(str)),
    }
    turnover = (
        1.0 if previous_positions is None else _signed_turnover(previous_positions, current_positions)
    )
    transaction_cost = cost_rate * turnover
    long_return = float(pd.to_numeric(long_leg["future_return"], errors="coerce").mean())
    short_return = float(pd.to_numeric(short_leg["future_return"], errors="coerce").mean())
    gross_spread = gross_long * long_return - gross_short * short_return
    net_return = gross_spread - transaction_cost
    equity *= 1.0 + net_return
    return (
        {
            "asof_date": asof,
            "horizon": horizon,
            "market": market,
            "long_symbols": ",".join(long_leg["symbol"].astype(str).tolist()),
            "short_symbols": ",".join(short_leg["symbol"].astype(str).tolist()),
            "long_return": long_return,
            "short_return": short_return,
            "gross_spread_return": gross_spread,
            "transaction_cost": transaction_cost,
            "net_return": net_return,
            "excess_return": net_return,
            "turnover": float(turnover),
            "equity": float(equity),
            "model_version": model_version,
            "created_at": created_at,
        },
        current_positions,
        equity,
    )


def _ordered_markets(series: pd.Series) -> list[str]:
    preferred = ["KOSPI", "KOSDAQ"]
    present = [market for market in preferred if market in set(series.dropna().astype(str))]
    return present or sorted(set(series.dropna().astype(str)))


def _reasons(row: pd.Series, side: str, score_column: str) -> list[str]:
    reasons = [
        f"{side} 관심도 {float(row.get(score_column, 0.0)):.2f}",
        f"예상수익 점수 {float(row.get('pred_return', 0.0)):.2f}",
    ]
    if side == "LONG":
        reasons.append(f"상위20 확률 {float(row.get('pred_prob_top20', 0.5)):.2f}")
    else:
        reasons.append(f"하위20 확률 {float(row.get('pred_prob_bottom20', 0.5)):.2f}")
    if pd.notna(row.get("momentum_score")):
        reasons.append(f"모멘텀 {float(row.get('momentum_score')):.2f}")
    if pd.notna(row.get("liquidity_score")):
        reasons.append(f"유동성 {float(row.get('liquidity_score')):.2f}")
    return reasons[:5]


def _risk_flags(row: pd.Series, side: str) -> list[str]:
    flags = []
    risk_score = float(row.get("risk_score", row.get("pred_risk", 0.5)))
    if side == "SHORT":
        flags.append("모의 숏 레그: 실제 공매도 가능 여부 미확인")
    if risk_score >= 0.7:
        flags.append("변동성/리스크 점수 높음")
    trading_value = row.get("trading_value_ma20")
    if pd.notna(trading_value) and float(trading_value) < 2_000_000_000:
        flags.append("거래대금 여유 낮음")
    rsi = row.get("rsi_14")
    if side == "LONG" and pd.notna(rsi) and float(rsi) >= 75:
        flags.append("단기 과열 가능성")
    if side == "SHORT" and pd.notna(rsi) and float(rsi) <= 25:
        flags.append("단기 반등 위험")
    return flags


def _report_rows(frame: pd.DataFrame, score_column: str) -> list[str]:
    rows = []
    for _, row in frame.iterrows():
        risk_flags = ", ".join(json.loads(row.get("risk_flags_json") or "[]")) or "-"
        name = row.get("name") if pd.notna(row.get("name")) else ""
        rows.append(
            f"| {int(row['leg_rank'])} | `{row['symbol']}` | {name} | "
            f"{float(row['weight']):.3f} | {float(row[score_column]):.4f} | "
            f"{float(row['pred_return']):.4f} | {risk_flags} |"
        )
    return rows


def _select_rebalance_dates(dates: pd.Series, frequency: str) -> set:
    date_series = pd.to_datetime(pd.Series(dates).dropna().unique()).sort_values()
    if date_series.empty:
        return set()
    frequency = frequency.upper()
    if frequency in {"D", "DAILY"}:
        return set(date_series.dt.date)
    if frequency in {"W", "WEEKLY"}:
        selected = date_series.to_series().groupby(date_series.to_period("W")).max()
    elif frequency in {"Q", "QUARTERLY"}:
        selected = date_series.to_series().groupby(date_series.to_period("Q")).max()
    else:
        selected = date_series.to_series().groupby(date_series.to_period("M")).max()
    return set(selected.dt.date)


def _signed_turnover(previous: set[str], current: set[str]) -> float:
    if not current:
        return 0.0
    return min(1.0, len(previous.symmetric_difference(current)) / (2.0 * len(current)))


def _long_short_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not config:
        return {}
    return dict(config.get("long_short", config))


def _rebalance_frequency(ls_cfg: dict[str, Any], horizon: str) -> str:
    value = ls_cfg.get("rebalance_frequency", "M")
    if isinstance(value, dict):
        return str(value.get(horizon, "M"))
    return str(value)


def _numeric(frame: pd.DataFrame, column: str, default: float) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _mean(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    value = pd.to_numeric(frame[column], errors="coerce").mean()
    return 0.0 if pd.isna(value) else float(value)


def _model_version(frame: pd.DataFrame) -> str | None:
    if "model_version" not in frame.columns:
        return None
    values = frame["model_version"].dropna().astype(str).unique()
    if len(values) == 0:
        return None
    if len(values) == 1:
        return values[0]
    return "mixed"


def _empty_recommendations() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "asof_date",
            "horizon",
            "symbol",
            "side",
            "leg_rank",
            "long_score",
            "short_score",
            "pred_return",
            "pred_prob_top20",
            "pred_prob_bottom20",
            "risk_score",
            "confidence",
            "weight",
            "reason_json",
            "risk_flags_json",
            "model_version",
            "created_at",
            "name",
            "market",
            "sector",
            "future_return",
        ]
    )


def _json_default(value):
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if pd.isna(value):
        return None
    return str(value)
