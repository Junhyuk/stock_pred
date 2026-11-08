#!/usr/bin/env python3
"""Exit 0 without a run when today's Asia/Seoul date is not an XKRX session.

The scheduled clock is 07:50 KST (22:50 UTC, Sunday–Thursday). workflow_dispatch
uses this same session-date gate. A calendar that cannot answer fails closed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.reliable_inputs import KST, session_schedule, utc_timestamp


def trading_day_decision(now: datetime | None = None, sessions=None) -> dict:
    issued = utc_timestamp(now if now is not None else datetime.now(KST))
    if pd.isna(issued):
        return {"run": False, "kst_date": None, "reason": "calendar_unavailable"}
    local = issued.tz_convert(KST).date()
    try:
        schedule, source = session_schedule(local, local, sessions)
    except (ImportError, ValueError, TypeError, OverflowError) as exc:
        return {"run": False, "kst_date": local.isoformat(), "reason": "calendar_unavailable",
                "detail": type(exc).__name__}
    is_session = local.isoformat() in set(schedule["date"].astype(str))
    return {"run": bool(is_session), "kst_date": local.isoformat(), "checked_at": issued.isoformat(),
            "calendar": source, "reason": "xkrx_session" if is_session else "not_xkrx_session"}


def emit_decision(decision: dict) -> int:
    print(json.dumps(decision, ensure_ascii=False))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"run={'true' if decision['run'] else 'false'}\n")
            if decision.get("kst_date"):
                stream.write(f"kst_date={decision['kst_date']}\n")
    return 1 if decision["reason"] == "calendar_unavailable" else 0


def main() -> int:
    return emit_decision(trading_day_decision())


if __name__ == "__main__":
    raise SystemExit(main())
