"""Premarket (08:00 KST) Model B/C variants on the evening inputs plus the US session.

Issue-time contract: a forecast for XKRX session T+1 is issued at or after
06:30 KST and before the 09:00 KST open of T+1, with a cutoff of 08:00 KST.
Inputs are the 24 evening features of session T (as built by
run_incremental_index_learner.market_frame) and six US features of the US
session that closed (+15 min, VIX +30 min) before the cutoff, selected by
roboquant.index_forecast.us_market_inputs.build_us_features. Rows without the
six US values are dropped, never filled. production_weight is 0.

The variant definitions are frozen in
reports/model_c_kospi_20261125/preregistration*.json (WB7, WB3, K4).
"""
from __future__ import annotations

import numpy as np

US6 = ["us_spx_return_1d", "us_nasdaq_return_1d", "us_sox_return_1d", "us_vix_level", "us_vix_return_1d",
       "sox_nasdaq_spread_1d"]
VARIANTS = {
    "WB7_bclose_pre_ridgecv_f24us": {"model": "B", "output": "expected_return", "kind": "ridgecv", "weighted": False},
    "WB3_b_pre_logit_f24us": {"model": "B", "output": "p_up", "kind": "logit", "weighted": False},
    "K4_c_pre_logit_absw_f24us": {"model": "C", "output": "p_up_given_extreme", "kind": "logit", "weighted": True},
}
RIDGE_ALPHAS = tuple(float(a) for a in np.logspace(-1, 4, 11))
LOGIT_C = 0.01


def abs_weights(returns) -> np.ndarray:
    r = np.abs(np.asarray(returns, dtype=np.float64))
    return r / r.mean()


def fit_predict(variant: str, x_train, returns_train, x_live) -> dict:
    """Fit one frozen variant on matured rows and score the live row(s)."""
    from sklearn.linear_model import LogisticRegression, RidgeCV
    from sklearn.preprocessing import StandardScaler

    spec = VARIANTS[variant]
    x_train = np.asarray(x_train, dtype=np.float64)
    x_live = np.atleast_2d(np.asarray(x_live, dtype=np.float64))
    r = np.asarray(returns_train, dtype=np.float64)
    if not (np.isfinite(x_train).all() and np.isfinite(r).all() and np.isfinite(x_live).all()):
        raise ValueError("premarket inputs must be finite; missing rows are dropped before fitting")
    scaler = StandardScaler().fit(x_train)
    xt, xl = scaler.transform(x_train), scaler.transform(x_live)
    if spec["kind"] == "ridgecv":
        model = RidgeCV(alphas=RIDGE_ALPHAS).fit(xt, r)
        return {"value": model.predict(xl).tolist(), "alpha": float(model.alpha_), "n_train": int(len(r))}
    weights = abs_weights(r) if spec["weighted"] else None
    model = LogisticRegression(C=LOGIT_C, solver="lbfgs", max_iter=2000, random_state=42)
    model.fit(xt, (r > 0).astype(int), sample_weight=weights)
    return {"value": model.predict_proba(xl)[:, 1].tolist(), "n_train": int(len(r))}
