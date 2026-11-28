"""Immutable forecasts, public receipts and the first scored outcome for each target."""
import json
import sqlite3

import numpy as np


def init_ledger(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS benchmark_issue (
        target TEXT NOT NULL, market TEXT NOT NULL, model TEXT NOT NULL, stage TEXT NOT NULL,
        anchor TEXT NOT NULL, cutoff TEXT NOT NULL, issued TEXT NOT NULL, run_date TEXT NOT NULL,
        anchor_close REAL NOT NULL, payload TEXT NOT NULL, version TEXT NOT NULL,
        confirmed INTEGER DEFAULT 0, PRIMARY KEY(target,market,model,stage,run_date))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS benchmark_expected (
        target TEXT NOT NULL, market TEXT NOT NULL, model TEXT NOT NULL,
        PRIMARY KEY(target,market,model))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS benchmark_outcome (
        market TEXT NOT NULL, target TEXT NOT NULL, close REAL NOT NULL, source TEXT NOT NULL,
        received_at TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(market,target))""")


def save_issue(conn, *, target, market, model, stage, anchor, cutoff, issued, run_date, close, payload):
    conn.execute("""INSERT OR IGNORE INTO benchmark_issue
        (target,market,model,stage,anchor,cutoff,issued,run_date,anchor_close,payload,version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (target, market, model, stage, anchor, cutoff, issued, run_date, close,
                  json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")), payload["model_version"]))


def issued_models(conn, target, market, stage, run_date):
    rows = conn.execute("""SELECT model,payload,cutoff,issued FROM benchmark_issue
        WHERE target=? AND market=? AND stage=? AND run_date=?""", (target, market, stage, run_date))
    return {model: {**json.loads(payload), "cutoff": cutoff, "issued_at": issued}
            for model, payload, cutoff, issued in rows}


def acknowledge(conn, publication):
    """Only match real Pages receipts to the exact local forecasts that appeared on screen."""
    for market, card in publication.get("markets", {}).items():
        for name, row in card.get("models", {}).items():
            if row.get("status") not in {"ready", "research_candidate"}:
                continue
            args = (publication["target_date"], market, name, publication["issue_kind"], row.get("issued_at"))
            saved = conn.execute("""SELECT payload,cutoff FROM benchmark_issue
                WHERE target=? AND market=? AND model=? AND stage=? AND issued=?""", args).fetchone()
            if saved and {**json.loads(saved[0]), "issued_at": args[-1], "cutoff": saved[1]} == row:
                conn.execute("""UPDATE benchmark_issue SET confirmed=1
                    WHERE target=? AND market=? AND model=? AND stage=? AND issued=?""", args)


def forward_scores(conn, market, history, anchor, versions):
    result = {}
    for (target,) in conn.execute("""SELECT DISTINCT target FROM benchmark_issue
        WHERE market=? AND confirmed=1 AND stage='final' AND target<=?
        EXCEPT SELECT target FROM benchmark_outcome WHERE market=?""", (market, anchor, market)).fetchall():
        if target in history.index:
            row = history.loc[target]
            conn.execute("INSERT OR IGNORE INTO benchmark_outcome VALUES (?,?,?,?,?,?)",
                         (market, target, float(row.close), row.source, row.received_at, row.payload_sha256))
    for model in ("A", "B", "C"):
        version = versions.get(model)
        rows = conn.execute("""SELECT i.anchor_close,i.payload,o.close FROM benchmark_issue i
            JOIN benchmark_outcome o USING (market,target) WHERE market=? AND model=? AND stage='final'
            AND confirmed=1 AND i.target<=? AND version=? ORDER BY i.target""", (market, model, anchor, version))
        hits, class_hits, errors = [], [], []
        for base, data, close in rows:
            row = json.loads(data)
            prob = row.get("direction_p_up") if model == "C" else row.get("p_up")
            if prob is not None:
                hits.append((prob >= 0.5) == (close > base))
            if model == "C":
                delta = (round(close * 100) - round(base * 100)) * 100
                boundary = 3 * round(base * 100)
                label = 2 if delta >= boundary else 0 if delta <= -boundary else 1
                class_hits.append(int(np.argmax([row["p_down"], row["p_normal"], row["p_up"]])) == label)
            if row.get("expected_index") is not None:
                errors.append(abs(row["expected_index"] - close))
        expected = conn.execute("""SELECT count(*) FROM benchmark_expected
            WHERE market=? AND model=? AND target<=?""", (market, model, anchor)).fetchone()[0]
        issued = conn.execute("""SELECT count(*) FROM benchmark_issue
            WHERE market=? AND model=? AND stage='final' AND confirmed=1 AND target<=?""", (market, model, anchor)).fetchone()[0]
        result[model] = {"n": len(hits), "model_version": version, "hit_rate": float(np.mean(hits)) if hits else None,
                         "class_n": len(class_hits), "class_hit_rate": float(np.mean(class_hits)) if class_hits else None,
                         "close_mae": float(np.mean(errors)) if errors else None,
                         "issued_all_versions": issued, "missing_final": max(0, expected - issued)}
    return result
