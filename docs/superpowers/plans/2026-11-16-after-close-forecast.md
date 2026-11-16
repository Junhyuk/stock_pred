# After-close forecast implementation plan

Approved specification: the user's accepted proposed plan in this conversation.
Goal: show current completed KOSPI/KOSDAQ closes and next-session research forecasts, updated once per KST weekday at18:00.
Constraints: preserve original checkout and paused flow subproject; keep immutable historical publications; actual receipts/cutoff/publication clocks; no fabricated probabilities or live anchor; production_weight=0; Python3.12, standard Ubuntu, forecast25min/Pages5min, artifacts14days; independent review before push. User has authorized commit, push, Actions smoke and Pages publication.

### Task 1: Core protocol and runner (Astra high)
- Add issuance protocol after_close_daily_v1, stage postclose, with actual completed collection snapshot cutoff. Retain legacy premarket path for manual compatibility; auto maps to a new postclose operation mode. Dispatch default validate remains nonpublishing. Single cron0 9 * * 1-5; weekends excluded; remove08:00 wait from new path.
- KR latest completed session->next XKRX session; US latest completed session->next XNYS session independently, including holidays/DST and issuance deadline. No KR gate blocking independently eligible US.
- Isolate postclose model artifacts, settings/feature/code identity, calibration/OOF and forward ledger from premarket. Retain2019 origin/21-session anchor. Actual live capture only at appropriate scheduled anchor, never backfill missed historical capture. Historical KR cutoff asof18:00, US first eligible KST weekday18:00 following its close. Current receipt delay must not shift every historical cutoff.
- Recompute research reference on new data/asof/target; keep retrospective/rejected status and weight0. No weakening sample/quality thresholds. New forward cohort starts0.
- Completion dedup by protocol+market+target, preserving morning records; support partial success/retry. Snapshot/hash/AES/checkpoint/code validation and Pages artifact visibility gates remain effective. Frozen postclose bundle must include observed prices and current economic/model snapshots; publish only verified allowlisted public files.
- Coordinate data/UI contract with Task2; implement core integration for append-only corrected observations without modifying old forecasts.
- Write failing behavioral tests first, then implement. Own src reliable/fit/us/automation/economic protocol integration, scripts accuracy/run/verify/status, configs, workflows, associated existing core tests and new postclose tests. Do not edit Task2-owned files.

### Task 2: Market closes and UI (Terra6.1 xhigh)
- Add market_closes module/collector with recent dated Naver KOSPI/KOSDAQ bars (direct public vendor API), using Yahoo for history and comparison/fallback. Preserve original publication unknown as null, actual received_at as actual receipt, no false claim of exchange-official finality. Check completed exchange session, positive finite price/OHLC, expected date, compare rounded two decimals when sources both present. Conflicts block that market forecast rather than silently picking.
- Store observations/revisions append-only in SQLite; preserve first writer index_eod, add only missing dates. Provide a read helper yielding effective index_eod rows as of actual cutoff for new protocol (all original columns retained), excluding future/conflicting observations; communicate exact helper signature to Task1. Preserve historical reconstruction provenance and avoid claiming original historical availability from today's receipts.
- Independent docs/data/market_closes.json with schema/run identity, checksum integration, market/session/close/source/received_at/verification status, stale/unavailable/conflict states. Collector failures preserve prior dated values, independently of forecast readiness.
- Update docs/reliable-index-forecast.html: explicit forecast baseline close/asof vs latest market close/source/receipt; next target date and issued time; prominently show recomputed calibrated research reference p_up/p_down/expected_return/index when main notready. Label research/retrospective/quality gates and not production approved. Missing data is not0/50. Direction from higher up/down probability, ties neutral. Expired target indicated, no relabeling prior prediction. Existing SHA/publication overlays respected.
- Own scripts/refresh_premarket_prices.py, new market_closes.py and collector script, sqlite_store.py if necessary, UI and dedicated data/UI tests. Do not change core pipeline/workflows owned by Task1. Agree public JSON/hash/collector outputs with Task1 early.
- Regression fixture2026-11-16 closes5100.50/665.65, prior11/13 closes5290.53/699.66; next KR target11/17. Tests first; no live data literal hardcoding in production.

### Task 3: Operations and evidence (Luna6 xhigh)
- Confirm remote/default branch, existing clean worktree base, Python3.12 environment, tool availability. Run baseline appropriate core tests before implementation changes when practical; preserve original dirty checkout.
- Read-only live data source verification, record bounded test/command evidence and repo artifact/cache usage. Do not edit files assigned to others. May write ignored report and run mocked tests/builds.
- Wait for root's explicit integration/review green signal before source commit/push or Actions dispatch. Then own Git commit/push and actual workflow validate+postclose run, track same commit/attempt, inspect forecast+deploy logs and public Pages JSON/UI freshness. No secrets in output; no cache/artifact deletion/settings changes unless root coordinates concrete necessity.
- Check standard public runner/no additional schedule/25+5min/14day storage budget. Distinguish repo measured usage from unknown account-shared budget. Report run links, SHA, job outcomes and final page.

### Final independent review
- Root reviews data/UI and protocol integration. Terra independently audits core (not its own UI/data); a fresh Sol6.1 high reviews whole change if needed after implementation slots free.
- Run relevant regression suite and full pytest once, report any baseline-only paused-subproject missing-schema failure by name. Mock late/partial/unavailable/429, weekends/holidays/DST, anchor/no anchor, cohort mismatch, no future leakage, duplicate/retry, immutable issued SHA.
- Following green relevant checks and independent review, commit/push, real actual-clock validate and postclose smoke/publish, confirm Pages artifact and HTTP payload/content. Preserve old archives/receipts.
