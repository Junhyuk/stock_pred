# Premarket US shadow forward check (`premarket_us_shadow_v1`)

Research-only forward confirmation of candidate C6 (`C6_pre_price_us_l2`): next-session
KOSPI/KOSDAQ `p_up` from the incumbent price features plus the US session that closed
before 08:00 KST. Logistic regression with C=0.01, no recalibration. `production_weight`
is 0. The Pages card is unchanged.

- **Pre-registration:** `preregistration.json`, sha256
  `42f0d118cf6afa078a3b3150d5bb8f6d3e1d8a0eef50b5220856cfbc2ff89daf`. The authoritative hash time is
  recorded in `preregistration.sha256`. The frozen spec hash lives in
  `configs/probability_shadow.yaml`. A mismatch blocks the run.
- **Schedule:** `.github/workflows/premarket-us-shadow.yml` runs on weekdays at 07:35 KST,
  with a 07:50 KST fallback.
  1. Collect the Yahoo KOSPI/KOSDAQ closes and the S&P 500/Nasdaq/SOX/VIX closes.
  2. Wait for 08:00 KST.
  3. Issue the forecast before the XKRX open.
  4. Append the result to `ledger.jsonl` and rewrite `summary.json`.

  A market already issued for a target is skipped. Non-XKRX days are skipped.
- **Validity:** a row counts only if all of the following hold:
  - it was produced at actual-clock `premarket_final` stage, not as a replay or rehearsal
  - it uses the frozen spec hash
  - every input receipt came before the 08:00 cutoff

  Blocked rows remain in the ledger as an audit trail.
- **Paired references:** each row records the up-rate baseline at the cutoff (the incumbent
  definition). It also records the published 18:00 after-close card for the same target
  (`docs/data/reliable_index_forecast.json`).
- **Decision rule (pre-stated):**
  - Interim safety look at 60 matured KOSPI forecasts: stop if Brier minus the up-rate
    Brier is above 0.02. This look makes no efficacy claim.
  - One decision at 252 matured forecasts, which passes only if all of these hold:
    - The 95% block-bootstrap upper bound of ΔBrier vs the up-rate is below 0.
    - The same upper bound vs the 18:00 card is below 0.
    - Log loss is no worse than the up-rate's.
    - ECE10 is at most 0.05.
  - A pass still needs independent ML/risk review. The PM cannot approve alone.
  - KOSDAQ is monitor-only.
- **Smoke:** run `workflow_dispatch` with `mode=smoke`. It writes a labelled rehearsal
  forecast to the run artifact and never touches the ledger.

Historical inputs are retrospective reconstructions. Original Yahoo publication times
are unverified, and the US close is assumed available 15 minutes after the session
close.
