"""Checks applied both at issue time and when staging the public benchmark file."""
from datetime import date
from math import isclose, isfinite


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def _private(value):
    if isinstance(value, dict):
        return any(k in {"evaluation_cache", "rows", "training_rows", "raw_response", "rights_json",
                         "secret", "feature_matrix", "residuals"} or _private(v) for k, v in value.items())
    return isinstance(value, list) and any(_private(x) for x in value)


def valid_benchmark_payload(payload):
    try:
        markets = payload["markets"]
        if (payload["schema_version"] != 1 or set(markets) != {"KOSPI200", "KOSDAQ150"}
                or payload["issue_kind"] not in {"final", "preliminary"} or _private(payload)
                or date.fromisoformat(payload["target_date"]) <= date.fromisoformat(payload["asof_date"])):
            return False
        gate = payload["c_trigger"]["status"]
        if gate not in {"unknown", "triggered", "not_triggered"}:
            return False
        for market in markets.values():
            chart = market.get("chart") or {}
            points = chart.get("points", [])
            if points:
                dates = [date.fromisoformat(p["date"]) for p in points]
                if (chart.get("raw_display_permitted") is not True or len(points) > 367
                        or dates != sorted(set(dates)) or dates[-1].isoformat() != chart["asof_date"]
                        or dates[-1].isoformat() > payload["asof_date"]
                        or any(not _finite(p["close"]) or p["close"] <= 0 for p in points)):
                    return False
            if market.get("close") is not None and (
                not points or dates[-1].isoformat() != payload["asof_date"] or market["close"] != points[-1]["close"]
            ):
                return False
            models = market["models"]
            if set(models) - set("ABC"):
                return False
            for name, row in models.items():
                if row.get("status") not in {"ready", "research_candidate"}:
                    continue
                if "close" in row or (name == "C" and gate != "triggered"):
                    return False
                probs = [row["p_down"], row["p_up"]]
                if name == "C":
                    probs.append(row["p_normal"])
                if not all(_finite(x) and 0 <= x <= 1 for x in probs) or not isclose(sum(probs), 1, abs_tol=1e-7):
                    return False
                if name != "C" or row.get("expected_index") is not None:
                    lo, hi = row["interval"]["low"], row["interval"]["high"]
                    if not all(_finite(x) for x in (lo, hi, row["expected_index"])) or not 0 < lo <= row["expected_index"] <= hi:
                        return False
                evidence = row.get("evaluation", {})
                if not row.get("model_version") or evidence.get("model_version") != row["model_version"]:
                    return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return False
