"""Small public CPU B/C forecast pair committed together by the daily job."""

import json
import math
from pathlib import Path


B_FILE = "model_b_forecast.json"
C_FILE = "model_c_forecast.json"
MARKETS = ("KOSPI", "KOSDAQ")
STATUSES = {"ready", "not_triggered", "not_ready"}
THRESHOLD = 0.03


def read_payload(path):
    path = Path(path)
    try:
        if path.stat().st_size > 64_000:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def valid_numbers(values):
    return all(type(value) in (int, float) and math.isfinite(value) for value in values)


def valid_probabilities(values):
    return (valid_numbers(values) and all(0 <= value <= 1 for value in values)
            and math.isclose(sum(values), 1.0, abs_tol=1e-12))


def valid_rows(rows, keys):
    return (isinstance(rows, list) and len(rows) <= len(MARKETS)
            and all(isinstance(row, dict) and row.get("market") in MARKETS
                    and valid_numbers([row.get("close")]) and row["close"] > 0
                    and valid_probabilities([row.get(key) for key in keys]) for row in rows)
            and len({row["market"] for row in rows}) == len(rows))


def valid_pair(b, c):
    if not all(isinstance(b.get(key), str) and b[key] and b[key] == c.get(key)
               for key in ("publication_id", "asof_date", "target_date", "target_close_at")):
        return False
    if b.get("trigger") != c.get("trigger") or b.get("trigger") not in {"github_actions_cpu", "local_cpu"}:
        return False
    for letter, data in (("b", b), ("c", c)):
        if (data.get("schema_version") != f"model_{letter}_forecast_v1"
                or data.get("inference_device") != "cpu" or data.get("production_weight") != 0):
            return False
    if not (valid_rows(b.get("markets"), ("p_up", "p_down"))
            and valid_rows(c.get("markets"), ("p_down", "p_normal", "p_up"))):
        return False
    for row in b["markets"]:
        interval = row.get("interval")
        if (not isinstance(interval, dict) or interval.get("level") != 0.8
                or not valid_numbers([interval.get("low"), interval.get("high"), row.get("expected_index")])
                or not interval["low"] <= row["expected_index"] <= interval["high"]):
            return False
    if c.get("status") not in STATUSES or c.get("threshold") != THRESHOLD or c.get("probability_mode") != "three_way":
        return False
    moves = c.get("trigger_returns")
    if not isinstance(moves, dict) or not moves.keys() <= set(MARKETS) or not valid_numbers(moves.values()):
        return False
    triggered = any(abs(value) >= THRESHOLD for value in moves.values())
    if c["status"] == "ready":
        return triggered and bool(c["markets"]) and all(
            row.get("asof_date") == c["asof_date"] and row.get("target_date") == c["target_date"]
            for row in c["markets"]
        )
    return not c["markets"] and (c["status"] != "not_triggered" or len(moves) == len(MARKETS) and not triggered)
