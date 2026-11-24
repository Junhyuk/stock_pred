"""Pure parts of the local Model B/C train-and-predict pipeline.

Clock windows, payload checks, the no-future-label check, backups and the
append-only forward ledger live here so they can be tested without a GPU,
a network or the live database. ``scripts/run_local_bc_pipeline.py`` wires
them to the existing training scripts.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

KST = ZoneInfo("Asia/Seoul")
STAGES = ("evening", "premarket")
# The evening stage needs the KRX close and closing auction to be final. The
# collector stores a first-seen bar once and never rewrites it, so an
# in-session run would freeze a partial close.
EVENING_EARLIEST_AFTER_CLOSE = timedelta(minutes=60)
# US equities close 16:00 ET = 06:00 KST in winter (05:00 in summer); the repo's
# availability rule adds 15 minutes, VIX 30 minutes. 06:30 KST covers both.
PREMARKET_EARLIEST = time(6, 30)
PREMARKET_CUTOFF = time(8, 0)
EVENING_FILES = (
    "models/dnn/incremental_kospi.pt",
    "models/dnn/incremental_kosdaq.pt",
    "models/dnn/model_b_close_kospi.pt",
    "models/dnn/model_b_close_kosdaq.pt",
    "models/dnn/model_c_kospi.pt",
    "models/dnn/model_c_kosdaq.pt",
    "models/dnn/model_c_accuracy.json",
    "models/dnn/incremental_learning_curve.json",
    "docs/data/model_b_forecast.json",
    "docs/data/model_c_forecast.json",
)
PREMARKET_FILES = ("docs/data/model_b_forecast.json",)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_kst(now) -> datetime:
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is None:
        raise ValueError("clock must be timezone-aware")
    return stamp.tz_convert(KST).to_pydatetime()


def evening_window(now, sessions: pd.DataFrame) -> dict:
    """Pick the as-of session for an evening run.

    ``sessions`` has ``date`` and UTC ``close_at`` columns (XKRX). The as-of
    session is the latest one whose close is at least an hour old; the target
    is the next session after it.
    """
    now_kst = _as_kst(now)
    closes = pd.to_datetime(sessions["close_at"], utc=True)
    finished = sessions.loc[closes + EVENING_EARLIEST_AFTER_CLOSE <= pd.Timestamp(now_kst).tz_convert("UTC")]
    if finished.empty:
        return {"status": "not_ready", "reason": "no finished XKRX session"}
    asof = str(finished["date"].iloc[-1])
    later = sessions.loc[sessions["date"] > asof]
    if later.empty:
        return {"status": "not_ready", "reason": "calendar has no next session"}
    today = now_kst.date().isoformat()
    in_session_day = bool((sessions["date"] == today).any()) and asof != today
    return {
        "status": "ready",
        "asof_date": asof,
        "target_date": str(later["date"].iloc[0]),
        "note": "today's session is not final yet; using the previous session" if in_session_day else "",
    }


def premarket_window(now, sessions: pd.DataFrame) -> dict:
    """Pick the target session for a premarket run (08:00 KST cutoff, before the open)."""
    now_kst = _as_kst(now)
    today = now_kst.date().isoformat()
    match = sessions.loc[sessions["date"] == today]
    if match.empty:
        return {"status": "not_ready", "reason": f"{today} is not an XKRX session"}
    opens = pd.to_datetime(match["open_at"], utc=True) if "open_at" in match else None
    open_at = (opens.iloc[0].tz_convert(KST).to_pydatetime() if opens is not None
               else datetime.combine(now_kst.date(), time(9, 0), KST))
    earliest = datetime.combine(now_kst.date(), PREMARKET_EARLIEST, KST)
    cutoff = datetime.combine(now_kst.date(), PREMARKET_CUTOFF, KST)
    if now_kst < earliest:
        return {"status": "not_ready", "reason": "US close plus buffer not yet reached"}
    if now_kst >= open_at:
        return {"status": "missed", "reason": "the XKRX open has passed; a premarket forecast is no longer valid"}
    earlier = sessions.loc[sessions["date"] < today]
    if earlier.empty:
        return {"status": "not_ready", "reason": "no earlier session"}
    # The live cutoff is the issue clock itself (every input must be received
    # by then); 08:00 KST is the nominal schedule and the historical
    # reconstruction's cutoff. Either way the forecast precedes the open.
    return {
        "status": "ready",
        "asof_date": str(earlier["date"].iloc[-1]),
        "target_date": today,
        "prediction_cutoff": now_kst.isoformat(),
        "nominal_cutoff": cutoff.isoformat(),
        "open_at": open_at.isoformat(),
    }


def labels_before(label_dates, asof_date: str) -> list[str]:
    """Return the label (next-close) dates that are later than the as-of session.

    A training row's label is the next session's close. Every label used for
    a forecast issued after the as-of close must be dated on or before it.
    """
    return [str(day) for day in label_dates if str(day) > str(asof_date)]


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_forecast_payload(payload: dict, schema: str, *, require_expected: bool) -> list[str]:
    """Problems that would make the page renderer reject or misread a payload."""
    problems = []
    if payload.get("schema_version") != schema:
        problems.append(f"schema_version is {payload.get('schema_version')!r}, expected {schema}")
    if payload.get("production_weight") != 0:
        problems.append("production_weight is not 0")
    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets:
        return problems + ["markets is not a non-empty list"]
    for row in markets:
        name = row.get("market")
        for key in ("asof_date", "target_date"):
            try:
                if datetime.strptime(str(row.get(key)), "%Y-%m-%d").date().isoformat() != row.get(key):
                    raise ValueError
            except ValueError:
                problems.append(f"{name} {key} is not YYYY-MM-DD")
        if str(row.get("target_date")) <= str(row.get("asof_date")):
            problems.append(f"{name} target_date is not after asof_date")
        p_up, p_down = row.get("p_up"), row.get("p_down")
        if not (_finite(p_up) and 0.0 <= p_up <= 1.0):
            problems.append(f"{name} p_up is not a probability")
        elif not (_finite(p_down) and abs(p_up + p_down - 1.0) <= 1e-3):
            problems.append(f"{name} p_up and p_down do not sum to 1")
        if not _finite(row.get("close")):
            problems.append(f"{name} close is not finite")
        if require_expected:
            ret, index = row.get("expected_return"), row.get("expected_index")
            if not (_finite(ret) and _finite(index)):
                problems.append(f"{name} expected_return/expected_index missing")
            elif _finite(row.get("close")) and abs(row["close"] * (1.0 + ret) - index) > 0.02:
                problems.append(f"{name} expected_index != close * (1 + expected_return)")
    for row in payload.get("premarket_markets", []) or []:
        if row.get("issue_stage") != "premarket" or row.get("production_weight") != 0:
            problems.append(f"premarket row {row.get('market')} lacks issue_stage=premarket or production_weight 0")
    return problems


def backup_files(root: Path, destination: Path, relative_paths) -> dict:
    """Copy each existing file under ``destination`` and return its sha256 map."""
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for relative in relative_paths:
        source = Path(root) / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[relative] = file_sha256(target)
    (destination / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    return hashes


def restore_files(root: Path, backup: Path, hashes: dict) -> list[str]:
    restored = []
    for relative, digest in hashes.items():
        source = Path(backup) / relative
        if file_sha256(source) != digest:
            raise RuntimeError(f"backup {source} does not match its recorded hash")
        shutil.copy2(source, Path(root) / relative)
        restored.append(relative)
    return restored


def prune_backups(directory: Path, keep: int) -> list[str]:
    """Delete the oldest pipeline backups beyond ``keep`` (only run-id named dirs)."""
    if keep < 1:
        raise ValueError("keep at least one backup")
    runs = sorted(path for path in Path(directory).glob("*_*") if path.is_dir() and (path / "SHA256SUMS.json").is_file())
    removed = []
    for path in runs[:-keep]:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def _canonical(row: dict) -> bytes:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def append_ledger(path: Path, row: dict) -> dict:
    """Append one row to a hash-chained JSONL ledger. Existing rows are never rewritten."""
    path = Path(path)
    previous = None
    if path.is_file():
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            previous = json.loads(lines[-1])["row_sha256"]
            key = (row.get("variant"), row.get("market"), row.get("target_date"))
            for line in lines:
                old = json.loads(line)
                if (old.get("variant"), old.get("market"), old.get("target_date")) == key:
                    raise ValueError(f"ledger already has {key}; a first issued forecast is never replaced")
    body = {key: value for key, value in row.items() if key not in ("prev_row_sha256", "row_sha256")}
    body["prev_row_sha256"] = previous
    body["row_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    return body


def verify_ledger(path: Path) -> list[str]:
    problems = []
    previous = None
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        body = {key: value for key, value in row.items() if key != "row_sha256"}
        if row.get("prev_row_sha256") != previous:
            problems.append(f"line {number}: chain broken")
        if hashlib.sha256(_canonical(body)).hexdigest() != row.get("row_sha256"):
            problems.append(f"line {number}: row hash mismatch")
        previous = row.get("row_sha256")
    return problems


def staleness(last_observation: dict, asof_date: str, sessions: list[str]) -> dict:
    """Sessions between each series' latest observation and the as-of session."""
    days = np.asarray(sorted(sessions))
    out = {}
    for name, last in last_observation.items():
        if last is None:
            out[name] = None
            continue
        out[name] = int(((days > str(last)) & (days <= str(asof_date))).sum())
    return out
