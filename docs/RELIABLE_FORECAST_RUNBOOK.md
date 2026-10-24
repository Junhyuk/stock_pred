# Reliable index forecast runbook

This workflow produces KOSPI/KOSDAQ next-session **research forecasts**. A completed run can legitimately report `rejected` or `not_ready`. Production weight remains **0**; a research result is not production approval or an order instruction.

## Environment and entry point

Use Python 3.12 and the dedicated forecast environment. On the current workstation its interpreter is `/home/joon/work/coding/AI/stock_pred/.venv-forecast/bin/python`. Run commands from the repository root. To create a separate environment on another workstation:

```bash
python3.12 -m venv .venv-forecast
.venv-forecast/bin/python -m pip install -e '.[dev]'
.venv-forecast/bin/python scripts/run_reliable_index_forecast.py --help
```

The entry point is `scripts/run_reliable_index_forecast.py`; its configuration is `configs/reliable_index_forecast.yaml`. Use the actual clock for operational runs. A simulated future decision time must never become the actual issue time or make future observations available.

Run against the research SQLite database:

```bash
.venv-forecast/bin/python scripts/run_reliable_index_forecast.py \
  --sqlite data/processed/index_forecast.sqlite \
  --config configs/reliable_index_forecast.yaml \
  --output-dir docs/data
```

The SQLite source is opened read-only. `--now` is for replay, not a normal operational run. `--target-date` must match the next KRX session. An optional `--position-evidence path/to/evidence.json` reads an existing evidence report; it does not collect missing positioning data.

The configured `participant_impact.sqlite` opens the actual positioning subproject
database read-only. Override it with `--participant-sqlite PATH.sqlite`, or use
`--participant-bridge PATH.json` for a versioned, checksummed normalized observation
snapshot. The JSON report supplied by `--position-evidence` is descriptive evidence;
it is not a feature table. A bridge contains actual flow/ownership/price records,
source URL, units, revision/finality, original availability or actual first-seen,
and ingestion timestamps. No timestamp is backdated to its observation date.

Outputs are `docs/data/reliable_index_forecast.json`, `docs/data/reliable_validation_report.json`, `docs/data/reliable_model_card.json`, and the immutable run file `docs/data/archive/<run_id>.json`.

## Inputs and point-in-time eligibility

The targets are the KOSPI and KOSDAQ indices at a one-session horizon. Optional stock inputs target the largest **100 KOSPI common shares / 50 KOSDAQ common shares**. They require verified common-share classification, active/not-suspended status, explicit exclusions for preferred shares, ETF/ETN and SPAC securities, dated market capitalisation, and an availability timestamp no later than the decision cutoff. A raw listing ranked by market capitalisation does not establish this universe.

Use only membership snapshots that were available at the simulated decision time. A historical filename downloaded today is not evidence that its contents were available on that historical date. Two dated snapshots do not justify filling the intervening membership history. Unknown classification, suspension status, or historical membership leaves the breadth group unavailable.

Participant-flow inputs need their own source, units, final/revision status, availability timestamps and complete eligible windows. Missing flow is missing, not zero. Each optional input group must establish eligibility independently; comparisons use the same eligible dates and report their coverage.

The optional SQLite input contracts are:

| Table | Required fields and eligibility |
| --- | --- |
| `reliable_membership_snapshot` | `snapshot_date, market, symbol, market_cap, security_type, available_at`; verified not-suspended flag or active trading status, plus the security exclusions above. |
| `reliable_stock_prices` | `date, symbol, close, available_at`; each used price needs a verified availability timestamp no later than the cutoff. Select the latest eligible revision; missing timestamps exclude the panel. |
| `reliable_participant_flow` | `trade_date, market, investor, net_value, turnover, is_final, available_at`; supported participant categories are FOREIGN, INSTITUTION and PENSION, including configured Korean aliases. |

Do not create these tables with fabricated rows merely to make a group eligible. The stock-common-share ranking is an input universe, not a change to the index prediction target.

The present evidence does **not** establish historical point-in-time breadth or usable participant-flow history. The runnable current experiment is therefore price-only. It must not claim incremental predictive power from stock breadth, foreign/institution/pension flows, or inferred holdings. Historical closing-price evaluation is labelled retrospective reconstruction; unknown original publication times remain unknown.

## Preliminary and final observations

Resolve the latest completed KRX session using the actual issue time and the session calendar. The target is the next session, not a calendar-day increment. Keep the input session date, actual `issued_at`, prediction cutoff and target session separate.

A preliminary observation is not a final closing-price input. Explicitly nonfinal rows, a missing/nonpositive/nonfinite current close, missing required price features, missing current retrieval time, or observations available after the decision cutoff block a valid live forecast. Do not silently use the previous close while labelling the result as current. A blocked-run status/audit record may be saved; it must not be represented as a valid issued forecast.

Keep forecast-stage terminology separate from data finality: a forecast may be marked preliminary before its target session while its required input gate is `ready`. This does not certify the forecast's statistical reliability or promote it to production.

08:00 KST is the **input snapshot cutoff**, not a fictional publication time.
Before 08:00 the stage is `preliminary`; a start at/after 08:00 and strictly before
the verified exchange open is `premarket_final` using the fixed 08:00 snapshot.
At or after the actual XKRX session open, issuance is blocked. Completion is checked
again before archiving: a run that crosses the open is blocked. `run_started_at`
records its start, and actual `issued_at`/`artifact_written_at` record when the
completed result becomes available. A 07:59 start completed at 08:01 stays a
07:59 preliminary snapshot with an honest 08:01 publication; it does not become
an 08:00 final observation. Explicit replay is labelled and is not live evidence.

Historical final observations use their target session's 08:00 cutoff. Historical
preliminary observations preserve elapsed time since their input session close,
capped by target 08:00; a Saturday 00:30 run therefore retains the after-close day
offset. Reusing only the wall-clock hour on the prior Friday would be incorrect.

Unknown original publication time may be recorded alongside a known actual retrieval time for a current research observation. It does not establish historical point-in-time availability. The configured collection/finality policy is not proof of an exchange publication timestamp.

## Reading a result

Check status and gate reasons before reading the numbers. `rejected` means the specified reliability gates were not met; `not_ready` means required data/calibration/evidence is insufficient. Research numbers shown with either status do not establish a reliable trading signal. Production weight stays 0.

`p_up` means the probability of a strictly positive return; the binary definition has `p_down = 1 - p_up` and `p_flat = 0`. Return-space ranges must be labelled as returns or percentages. An index-level range, when presented, is the latest close multiplied by `1 + return_bound`. An 80% range is a calibration target, not a guarantee.

Inspect the model card, frozen development selection, mature out-of-fold calibration, terminal sample count, aligned baselines, paired bootstrap evidence, interval coverage/width/lower-tail gates, regime slices and ablations. At least 252 terminal observations and the configured statistical gates are required by the research acceptance policy. Retrospective success does not satisfy the separate requirement for 60 actual live observations and independent ML/risk review. The PM cannot approve a model for production alone.

Previous-direction and simple-trend forecasts are additional uncalibrated audit
references. Buy-and-hold and cash show descriptive index-exposure returns on the
same scored dates, without execution, fees, financing, dividends or interest.
They are not trading profit results. The approved probability/interval gates are
unchanged.

## Participant observations as forecast candidates

The parent integration evaluates FOREIGN, INSTITUTION and PENSION independently:
missing pension rows do not remove verified foreign observations. Market-total
cash flow and the verified top100/top50 stock subset are separate scopes. Stock
groups additionally require per-date common-share membership, eligible price
revisions and corporate-action/adjustment evidence. A small stock sample is never
called market-total flow. The legacy three-participant bundle remains a separate
optional group.

Predetermined raw features use 1/5/20-session net flow divided by turnover.
Where observed comparable foreign ownership endpoints exist, a separate candidate
adds ownership change and flow persistence. These are observed aggregate evidence,
not inferred private holdings or holding probabilities. Complete PIT history and
current inputs are required before a group enters model fitting.

The participant report separates same-session association from next-session
association and reports uncertainty. Those full-history diagnostics are descriptive,
not causal effects, and never select actors, columns or weights. Forecast comparisons
are made only by the parent chronological OOF/development process; the terminal
block confirms the frozen selection. `forecast_ablation_evidence` records the
development candidate/baseline comparisons and labels their selection exposure.
Missing actual source data produces `insufficient_data`, inconclusive positioning,
and no forced group or invented improvement.

## Restoring Korean positioning evidence

The separate Smart Money Position Analyzer currently has an intentionally empty observation database. Its ten offline tests verify behaviour with test fixtures; they do not populate real data. The independent explanation is [the positioning root-cause review](../sub_project/Smart_Money_Position_Analyzer/docs/evidence/2026-10-23-positioning/root_cause_review.md). See also its [release decision](../sub_project/Smart_Money_Position_Analyzer/release_decision.md) and [data requirements](../sub_project/Smart_Money_Position_Analyzer/docs/DATA_REQUIREMENTS.md).

Obtain permitted, verified exports from the [KRX Data Marketplace](https://data.krx.co.kr/). Preserve the source URL, original file, units, checksum and actual availability/retrieval time. Convert abbreviated units to shares and KRW before import. If original publication time is unknown, use the actual ingestion time; do not backdate it to the trading date.

For a foreign-flow interpretation, the current engine needs at least 21 price sessions, foreign net-share quantities on at least 16 of the latest 20 price sessions, and at least two ownership snapshots supporting the 20-session ownership comparison. A positive label still requires the engine's directional agreement tests. These are minimum evidence conditions, not a promise of an accumulation/distribution label. The current Korean signal engine analyses `FOREIGN`; institution/pension position inference is not implemented.

From `sub_project/Smart_Money_Position_Analyzer`, using the same Python 3.12 interpreter:

```bash
python -m smartmoney doctor
python -m smartmoney status
python -m smartmoney import-csv price path/to/verified_prices.csv --market KR
python -m smartmoney import-csv kr-flow path/to/verified_foreign_flow.csv --market KR --investor FOREIGN
python -m smartmoney import-csv kr-ownership path/to/verified_ownership.csv --market KR
python -m smartmoney analyze --market KR --symbol 005930
python -m smartmoney analyze --market KR --symbol 000660
```

In these examples, `python` must resolve to the dedicated Python 3.12 environment. For a reproducible cutoff, pass an explicit timezone-aware `--as-of` timestamp that is no later than the actual decision time. A date-only cutoff uses UTC end-of-day and should not be mistaken for a Korean premarket decision time.

The [KRX Open API](https://openapi.krx.co.kr/) route requires approved credentials and does not make investor-flow coverage interchangeable with its stock daily-price endpoint. The [OpenDART source](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS004&apiId=2019021) is a separate disclosure route. Keep credential values out of code, documents, logs, query URLs and version control. Missing API credentials do not prevent a permitted manual CSV import.

Report positioning as observed / inferred / unverified, with evidence, conflicts, missing inputs and cutoff. Public aggregated flows do not identify private accounts, hidden positions, actual average entry prices or same-account derivative hedges. No verified inputs means `불확실 / LOW`, not an estimated holding fabricated from templates.

## Operational evidence and handoff

Keep the run configuration, actual issue time, input hashes, source/retrieval metadata, status, forecasts, evaluation and gate reasons together. Preserve immutable issued-run evidence separately from the replaceable latest-status output. Preserve the bundled positioning database; use a copied database for audits that write signal snapshots.

The current task evidence is under `.superpowers/sdd/index-calibration/`; its `release_decision.md` records the final test/lint/run evidence and separate software, research, positioning and production decisions. A new operator must inspect that record rather than infer readiness from a successful process exit.
