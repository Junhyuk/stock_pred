#!/usr/bin/env python3
"""Exit 0 without a run when today's Asia/Seoul date is not an XKRX session.

The scheduled clock is daily at 07:50 KST (22:50 UTC). workflow_dispatch uses
this same session-date gate. A calendar that cannot answer fails closed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load_reliable_inputs():
    """Load the calendar helper without importing the forecast package.

    ``import roboquant.index_forecast`` runs the package init, which imports the
    model stack. The trading-day gate has to run before that stack is installed.
    """
    path = ROOT / "src" / "roboquant" / "index_forecast" / "reliable_inputs.py"
    spec = importlib.util.spec_from_file_location("_premarket_reliable_inputs", path)
    if spec is None or spec.loader is None:
        raise ImportError("reliable_inputs")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_INPUTS = _load_reliable_inputs()
KST = _INPUTS.KST
session_schedule = _INPUTS.session_schedule
utc_timestamp = _INPUTS.utc_timestamp


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
            stream.write(f"reason={decision['reason']}\n")
            if decision.get("kst_date"):
                stream.write(f"kst_date={decision['kst_date']}\n")
    return 1 if decision["reason"] == "calendar_unavailable" else 0


def main() -> int:
    return emit_decision(trading_day_decision())


if __name__ == "__main__":
    raise SystemExit(main())
