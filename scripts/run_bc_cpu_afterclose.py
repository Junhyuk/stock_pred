#!/usr/bin/env python3
"""Calculate the daily CPU B/C forecasts from a verified after-close snapshot."""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

from roboquant.index_forecast import automation_state as state
from roboquant.index_forecast.bc_cpu import forecast_market
from roboquant.index_forecast.bc_publication import B_FILE, C_FILE, MARKETS, THRESHOLD, read_payload, valid_pair
from roboquant.index_forecast.market_closes import effective_index_eod_frame
from roboquant.index_forecast.postclose import PROTOCOL, verify_market_closes
from roboquant.index_forecast.reliable_inputs import KST, session_schedule, utc_timestamp


HISTORY_START = "2019-01-01"
B_VARIANT = "장후 검증 기준선 · 종가 유지"
C_VARIANTS = {"hist_gradient": "히스토그램 그래디언트 부스팅", "regime_prior": "변동성 구간별 과거 빈도"}


def cents(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount > 0:
            return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except ArithmeticError:
        pass
    return None


def usable_observation(item, *, close_at, cutoff):
    received = utc_timestamp(item.get("received_at"))
    return (item.get("status") == "verified" and bool(cents(item.get("close")))
            and pd.notna(received) and close_at <= received <= cutoff)


def daily_inputs(frame, quotes, sessions, cutoff):
    day, prior = sessions.iloc[-1], sessions.iloc[-2]
    selected = frame[frame.index_code.isin(MARKETS)]
    revisions = {(row["index_code"], row["session_date"]): row for row in frame.attrs.get("live_revisions", [])}
    moves, histories = {}, {}
    for market in MARKETS:
        latest = revisions.get((market, day.date), {})
        previous = revisions.get((market, prior.date), {})
        public = quotes.get(market, {})
        if (not usable_observation(latest, close_at=day.close_at, cutoff=cutoff)
                or not usable_observation(public, close_at=day.close_at, cutoff=cutoff)
                or public.get("session_date") != day.date
                or cents(latest["close"]) != cents(public["close"])):
            continue
        if usable_observation(previous, close_at=prior.close_at, cutoff=cutoff):
            last_cents, prior_cents = cents(latest["close"]), cents(previous["close"])
            moves[market] = float(Decimal(last_cents - prior_cents) / Decimal(prior_cents))
        rows = selected.loc[selected.index_code.eq(market)].set_index("session_date").reindex(sessions["date"])
        levels = pd.to_numeric(rows["close"], errors="coerce")
        levels = levels.where(levels.gt(0) & levels.lt(float("inf")))
        conflicts = [
            date for (name, date), revision in revisions.items()
            if name == market and date in levels.index and revision["status"] == "conflict"
        ]
        levels.loc[conflicts] = float("nan")
        receipts = pd.to_datetime(rows["ingested_at"], utc=True, errors="coerce", format="mixed")
        levels.loc[day.date] = latest["close"]
        receipts.loc[day.date] = utc_timestamp(latest["received_at"])
        if market in moves:
            levels.loc[prior.date] = previous["close"]
            receipts.loc[prior.date] = utc_timestamp(previous["received_at"])
        # Only the cutoff-eligible close is live. Earlier vendor bars retain their
        # original values and are reconstructed history unless captured that session.
        if levels.tail(21).notna().sum() == 21:
            same_session = receipts.dt.tz_convert(KST).dt.strftime("%Y-%m-%d").eq(rows.index)
            sessions_received = receipts.loc[same_session & receipts.le(cutoff)].to_dict()
            histories[market] = (levels.tolist(), sessions_received)
    return moves, histories


def fresh_rows(payload, day):
    return [row for row in payload["markets"] if row.get("asof_date") == day]


def run_prepared(manifest, output_dir, *, now=None, run_id=None):
    current = utc_timestamp(now if now is not None else datetime.now(UTC))
    day = current.tz_convert(KST).date()
    schedule, _ = session_schedule(day - timedelta(days=15), day + timedelta(days=30))
    dates = schedule["date"].tolist()
    if day.isoformat() not in dates:
        return {"status": "non_trading_day"}
    if not state.operation_decision(current, "postclose")["allowed"]:
        return {"status": "outside_postclose_window"}
    anchor_index = dates.index(day.isoformat())
    anchor, target = schedule.iloc[anchor_index:anchor_index + 2].itertuples(index=False)
    if not anchor.close_at <= current < target.open_at:
        return {"status": "outside_postclose_window"}

    output_dir = Path(output_dir)
    b_path, c_path = output_dir / B_FILE, output_dir / C_FILE
    old_b, old_c = read_payload(b_path), read_payload(c_path)
    same_day = valid_pair(old_b, old_c) and (old_c["asof_date"], old_c["target_date"]) == (anchor.date, target.date)
    previous_b = fresh_rows(old_b, anchor.date) if same_day else []
    previous_c = fresh_rows(old_c, anchor.date) if same_day else []
    duplicate = {"status": "already_published", "redeploy": bool(run_id and old_c.get("run_id") == run_id)}
    if same_day and len(previous_b) == len(MARKETS) and (
        old_c["status"] == "not_triggered" or len(previous_c) == len(MARKETS)
    ):
        return duplicate

    restored = state.restore_inputs(Path(manifest).parent, now=current, code_hash=state.source_fingerprint(),
                                   issuance_protocol=PROTOCOL)
    files = restored["paths"]
    if restored["status"] != "ready" or not {"sqlite", "market_closes", "market_closes_receipt"} <= files.keys():
        return {"status": "not_ready", "reason": restored.get("reason", "incomplete_frozen_prices")}
    prepared = restored["manifest"]
    cutoff = utc_timestamp(prepared["prepared_at"])
    if not anchor.close_at <= cutoff <= current:
        return {"status": "not_ready", "reason": "cutoff_outside_completed_session"}
    data = verify_market_closes(files["market_closes"], files["market_closes_receipt"])
    prices = effective_index_eod_frame(files["sqlite"], cutoff=cutoff)
    sessions, _ = session_schedule(HISTORY_START, anchor.date)
    moves, histories = daily_inputs(prices, data["markets"], sessions, cutoff)
    confirmed_today = set(moves)
    if same_day and old_c["status"] != "not_ready":
        moves.update(old_c["trigger_returns"])
    triggered = any(abs(value) >= THRESHOLD for value in moves.values())
    shared = {
        "issue_stage": "postclose",
        "trigger": "github_actions_cpu" if run_id else "local_cpu",
        "inference_device": "cpu",
        "fit_device": "cpu",
        "production_weight": 0.0,
        "publication_id": prepared["manifest_hash"],
        "run_id": run_id,
        "asof_date": anchor.date,
        "target_date": target.date,
        "target_close_at": target.close_at.isoformat(),
        "prediction_cutoff": cutoff.isoformat(),
        "historical_input_mode": "reconstructed",
    }
    b_rows, c_rows = previous_b.copy(), previous_c.copy()
    for market in MARKETS:
        needs_b = market not in {row["market"] for row in b_rows}
        needs_c = (triggered and market in confirmed_today
                   and market not in {row["market"] for row in c_rows})
        if market not in histories or not (needs_b or needs_c):
            continue
        levels, receipts = histories[market]
        try:
            forecast = forecast_market(market, levels, sessions=sessions["date"].tolist(),
                                       close_received_at=receipts, prediction_cutoff=cutoff, run_c=needs_c)
        except ValueError:
            # Bad or too-short history in one market cannot make the other market stale.
            continue
        identity = {"market": market, "asof_date": anchor.date, "target_date": target.date,
                    "trained_through": anchor.date, "prediction_cutoff": cutoff.isoformat()}
        if needs_b:
            b_rows.append({**forecast["b"], **identity, "variant": B_VARIANT})
        if forecast["c"] is not None:
            c_rows.append({**forecast["c"], **identity, "variant": C_VARIANTS[forecast["c"]["method"]]})
    c_status = "ready" if c_rows else "not_triggered" if not triggered and len(moves) == len(MARKETS) else "not_ready"
    if same_day and len(b_rows) == len(previous_b) and len(c_rows) == len(previous_c) and c_status == old_c["status"]:
        return duplicate
    prior_b = old_b.get("markets", [])
    if not isinstance(prior_b, list):
        prior_b = []
    for row in prior_b:
        if (isinstance(row, dict) and row.get("market") in MARKETS and row.get("interval")
                and row.get("asof_date", "") < anchor.date
                and row["market"] not in {forecast["market"] for forecast in b_rows}):
            b_rows.append({**row, "carried_forward": True})
    b = {**shared, "schema_version": "model_b_forecast_v1", "model_name": "모델 B",
         "architecture": "장후 종가 유지 · 방향 과거 빈도", "markets": b_rows}
    c = {**shared, "schema_version": "model_c_forecast_v1", "model_name": "모델 C",
         "architecture": "전체 거래일 급변 3분류", "probability_mode": "three_way",
         "threshold": THRESHOLD, "status": c_status, "trigger_returns": moves, "markets": c_rows}
    completed = current if now is not None else utc_timestamp(datetime.now(UTC))
    if not completed < target.open_at:
        return {"status": "outside_postclose_window"}
    b["generated_at"] = c["generated_at"] = completed.isoformat()
    if not valid_pair(b, c):
        raise ValueError("invalid CPU forecast publication pair")
    state.atomic_json(b_path, b, indent=2)
    state.atomic_json(c_path, c, indent=2)
    return {"status": "published", "model_c_status": c_status,
            "trigger_returns": moves, "asof_date": anchor.date, "target_date": target.date}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-manifest", required=True, type=Path)
    parser.add_argument("--output-dir", default=Path("docs/data"), type=Path)
    args = parser.parse_args()
    result = run_prepared(args.prepared_manifest, args.output_dir, run_id=os.environ.get("GITHUB_RUN_ID"))
    print(json.dumps(result, ensure_ascii=False))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            print(f"redeploy={'true' if result.get('redeploy') else 'false'}", file=stream)


if __name__ == "__main__":
    main()
