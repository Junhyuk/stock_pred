"""Fixed volatility-normalized interval shadows; no change to point forecasts."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _positive(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _interval(records: list[dict], center: float | None, scale, min_rows: int,
              *, baseline: bool = False) -> dict:
    result = {'status': 'not_ready', 'method': 'prior_mature_vol20_normalized_order_statistic',
              'range_low': None, 'range_high': None, 'calibration_n': 0,
              'production_weight': 0.0, 'target_coverage': .8}
    if not _positive(scale) or center is None or not math.isfinite(center):
        return {**result, 'reason': 'invalid_current_scale_or_center'}
    scores = []
    for row in records:
        prior_scale = row.get('scale_at_prediction')
        if not _positive(prior_scale):
            continue
        prediction = row['baselines']['mean']['expected_return'] if baseline else row['expected_return']
        error = abs(row['actual_return'] - prediction)
        if math.isfinite(error):
            scores.append(error / max(float(prior_scale), 1e-6))
    n = len(scores)
    result['calibration_n'] = n
    rank = math.ceil((n + 1) * .8)
    if n < min_rows or rank > n:
        return {**result, 'reason': 'insufficient_prior_mature_scaled_residuals'}
    quantile = float(np.partition(scores, rank - 1)[rank - 1])
    radius = float(scale) * quantile
    return {**result, 'status': 'ready', 'expected_return': center, 'scale': float(scale),
            'normalized_quantile': quantile, 'range_low': center - radius, 'range_high': center + radius}


def normalized_interval_shadows(records: list[dict], *, cutoff, predicted: float, scale,
                                 mean_prediction: float | None, window: int = 252,
                                 min_rows: int = 60, event_bucket: bool | None = None) -> dict:
    """Filter maturity before taking the trailing window; never use current error."""
    stamp = pd.Timestamp(cutoff)
    mature = [row for row in records
              if row.get('label_available_at') is not None and row.get('target_close_at') is not None
              and pd.Timestamp(row['label_available_at']) <= stamp
              and pd.Timestamp(row['target_close_at']) <= stamp][-window:]
    pooled = _interval(mature, predicted, scale, min_rows)
    bucket = [row for row in mature if event_bucket is not None and row.get('calendar_event_bucket') is event_bucket]
    conditional = _interval(bucket, predicted, scale, max(60, min_rows))
    if event_bucket is None or conditional['status'] != 'ready':
        conditional = {**pooled, 'fallback': 'pooled_volatility_normalized',
                       'bucket_calibration_n': conditional['calibration_n'],
                       'fallback_reason': 'calendar_coverage_unknown' if event_bucket is None else 'insufficient_same_bucket_residuals'}
    else:
        conditional.update(fallback=None, bucket_calibration_n=conditional['calibration_n'])
    conditional['event_bucket'] = event_bucket
    return {'volatility_normalized': pooled, 'event_conditional': conditional,
            'mean_volatility_normalized': _interval(mature, mean_prediction, scale, min_rows, baseline=True)}


def interval_score80(actual, low, high):
    actual, low, high = map(np.asarray, (actual, low, high))
    return high - low + 10 * np.maximum(low - actual, 0) + 10 * np.maximum(actual - high, 0)
