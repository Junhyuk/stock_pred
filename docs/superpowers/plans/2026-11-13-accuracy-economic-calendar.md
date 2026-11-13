# Accuracy and Economic Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans with the explicitly assigned Astra implementation, Terra independent review, and Luna execution/Git roles. Follow the tasks in order and record their evidence in the ignored SDD ledger.

**Goal:** Make evaluated and issued forecasts use the same frozen training snapshot; add independently evaluated volatility intervals and cutoff-safe economic calendar/actual-value research.

**Architecture:** Preserve the current research engine and production weight zero. A shared exchange-session anchor and immutable fit snapshot define the forecast protocol. Separate economic event/version storage feeds calendar context and gated numeric/interval shadows without inventing missing values or surprise estimates.

**Tech Stack:** Existing Python, pandas, scikit-learn, SQLite, exchange_calendars, static JSON/HTML/JavaScript and GitHub Actions.

**Spec:** The user's approved implementation plan in this conversation is authoritative. This file records its executable scope; it does not authorize unrelated changes.

## Global Constraints

- Work only in `.worktrees/accuracy-calendar-20261113`; preserve the dirty original checkout and paused foreign-flow subproject.
- Astra implements, Terra independently verifies, Luna executes tests/data/Git. No additional agents. The user authorized execution and commits; do not ask again.
- Preserve `production_weight=0`, current main acceptance gates, and rejected/not-ready states. Previously viewed terminal results are exploratory. Promotion needs at least 252 mature future observations and independent review.
- Refit origin `2019-01-02`, every 21 exchange sessions; XKRX for Korea, XNYS for US. Missing input rows never change the schedule.
- Retain prior mature OOF window 252/minimum 60, one-session purge, explicit cutoff/target maturity, 80% nominal intervals, bootstrap 2000/block20/seed42.
- Calendar geography is Korea, US, Japan, eurozone ECB. Do not substitute all Europe for eurozone.
- Store schedule versions and actual release vintages immutably. Keep original timezone and UTC, first-seen and source publication times. Date-only/TBA is not a fabricated timestamp.
- Never fabricate consensus or use revised current history as original-vintage PIT history.
- Daily calendar retrieval has a hard total budget of 120 seconds, request timeout 10 seconds and at most one retry. Failure disables dependent shadows, preserving price context and a failure ledger.
- UI: next 30 days and recent 7 days, expected/actual/previous/change/revision/source/availability states; compatibility with existing JSON consumers.
- Exclude KST Sunday consistently in schedules and retries. Prepare 07:50 KST (`50 22 * * 0-5` UTC), predict at/after08:00 KST (`0 23 * * 0-5` UTC). Saturday may refresh calendar/state. The final step consumes only hash-verified frozen prepare inputs; late/missing receipts are not backdated.
- Final verification includes actual Actions duration/count/state storage and a 30-day worst-case estimate on standard public runners. Report unknown account-wide shared usage; do not claim an account billing cap was verified.
- Finish with reviewed commits, actual-clock latest-completed-session refresh where market/deadline gates permit, Pages update and links. Do not spoof a past clock to publish; use validation-only Actions outside valid windows and retain explicit unavailable/open-session states.

## Review Focus

- Missing sessions, delayed labels and later revisions cannot move a fit anchor or rewrite its snapshot (Task 1).
- Invalid or absent stored snapshots must produce explicit not-ready, not silently train on current data (Task 1).
- Scale changes and future residuals cannot alter already issued intervals; insufficient event buckets fall back explicitly (Tasks 2 and 4).
- DST, date-only, postponements and late receipt cannot turn unavailable events/actuals into available features (Task 3).
- Incomplete public sources, process deadlines and failed publication cannot erase durable evidence or block the normal price display (Tasks 4 and 5).

### Task 1: Shared fit protocol and direction metrics

**Files:** `src/roboquant/index_forecast/fit_protocol.py`, `reliable.py`, both reliable pipelines, `tests/test_reliable_accuracy_protocol.py`.

**Interfaces:** `session_anchor(asof, calendar_name, origin, step) -> dict`; `make_fit_snapshot(data, features, anchor, settings) -> dict`; `restore_fit_snapshot(snapshot, features, settings) -> DataFrame`. Snapshot has anchor and cutoff, ordered row IDs and values, target/availability timestamps, feature order, settings and content hash. Pipelines preserve the market calendar and pass durable snapshots; latest never replaces an anchor snapshot with current data. Historical snapshots are explicitly retrospective.

- [ ] Write tests for fixed anchor despite missing rows, cutoff-safe purged snapshot, tamper rejection, walk/live parity on intermediate and boundary sessions, and separate return/probability directions; Luna observes RED.
- [ ] Implement fixed exchange-session ordinal from the first exchange session on/after origin. Training may use earlier history, but forecasts before origin are unavailable. Apply purge to scheduled sessions before maturity and feature filtering. A failed scheduled fit remains unavailable until the next scheduled anchor.
- [ ] Share snapshot selection between walk/latest; calibration updates separately on each prediction's prior mature OOF. Preserve snapshot values or an equally verifiable model artifact, not a hash alone. Export compact metadata while keeping complete snapshots in durable artifacts.
- [ ] Full ordered training values stay local/private. Public durable state contains only a JSON fitted model/transform (no training arrays), ordered input hashes and protocol/clock metadata. Verify content hash and trusted repository provenance; absent state is not-ready. Luna verifies focused/existing tests; Terra reviews and commit.

### Task 2: Volatility interval and evaluation evidence

**Files:** `reliable.py`, `tests/test_reliable_accuracy_protocol.py`, model-card/report serialization.

**Interfaces:** Store `scale_at_prediction`; separate `interval_shadows` from incumbent interval fields. Use `abs(y-pred)/max(volatility_20d,1e-6)` on prior mature records and the unchanged finite-sample order statistic; multiply by current positive finite volatility. Missing scale yields unavailable shadow. Include normalized mean reference.

- [ ] Write RED tests for numeric interval values, unavailable scale, delayed labels, unchanged point/probability outputs and interval score.
- [ ] Implement one fixed vol20 candidate, coverage/width/upper+lower breach/80% interval score and aligned differences. Keep original main gates and fields.
- [ ] Record interval method/config/hash, exploratory versus forward evidence and per-slice sample sizes. Luna runs focused tests; Terra reviews and Luna commits.

### Task 3: Economic event and release-vintage store

**Files:** new `economic_calendar.py`, `economic_sources.py`, `configs/economic_calendar.yaml`, collector/import CLI and `tests/test_economic_calendar.py`.

**Interfaces:** SQLite immutable `economic_event_versions`, `macro_release_versions` and collection-run records; compact JSON history export/import; `events_asof(cutoff)` and `macro_features_asof(cutoff)`.

- [ ] Write RED tests for append-only replay, schedule revisions/cancellations, timezone/DST, date-only/TBA, unknown publication, actual arrival after cutoff, first release versus revision and incompatible units.
- [ ] Implement official-source adapters for Fed/BLS/BEA, BOK/Korean statistics, BOJ/Japan statistics and ECB/Eurostat. A public current snapshot is usable only from its receipt onward. Key-dependent sources have explicit unavailability plus a validated structured import path, never fake data.
- [ ] Store policy levels (Fed lower/upper with derived midpoint, BOK base, BOJ defined target, ECB deposit), national CPI/HICP headline/core variants, employment/unemployment, US NFP and PCE, original units/reference periods and release revisions. NFP is part of Employment Situation, not a duplicate event. Flash/final and Tokyo/national series remain distinct.
- [ ] Fixed numeric candidate has 14 changes: four policy changes in bp, four headline inflation YoY changes in pp, four unemployment changes in pp, US NFP monthly change in thousands, US core-PCE YoY change in pp. Keep raw levels and additional measures for display. Missing prior vintage or incompatible definition makes the feature unavailable.
- [ ] Register the single numeric candidate as `observed_current_receipt_v1`: latest actually observed values as of each cutoff, usable only after real receipt. Keep `verified_first_release` queries separate and unavailable without authenticated original-vintage evidence. Normal structured imports cannot backdate first receipt; only restoration of our trusted immutable ledger preserves earlier receipt times. A hash establishes integrity, not historical authenticity. Require explicit series definition/variant/adjustment/unit compatibility; NFP official monthly change is never differenced again.
- [ ] Numeric shadow is linear only, complete PIT history and existing sample gates required; no automatic model-family or feature search. Report missingness and distinct release counts. Luna runs tests/import smoke; Terra reviews; commit.

### Task 4: Forecast integration and forward evidence

**Files:** both reliable pipelines, `reliable.py`, `economic_calendar.py`, daily scripts/config, forward ledger tests.

**Interfaces:** Forecast cutoff is authoritative (existing KR final cutoff 08:00); 07:50 is collection start. Exposure `(asof_close,target_close]` is separate from future risk `(cutoff,target_close]`. Export scheduled/released/pending context and complete-source status.

- [ ] Write RED tests for post-target events, already released overseas events, incomplete calendar, event-bucket fallback and immutable first-issued future evidence.
- [ ] Add conditional normalized interval with minimum 60 prior mature same-bucket OOF rows within the existing 252 window. Unavailable calendar or insufficient bucket uses the pooled normalized shadow with a reason. No main-gate bypass.
- [ ] Wire the predeclared numeric shadow and actual storage/import through end-to-end pipelines; unavailable history is honest not-ready. Register candidate protocol/hash and immutable per-session forecasts/baselines; score only mature targets, record missing/late/revised/failure rows.
- [ ] Compare future Brier/log-loss for numeric probability and interval score for intervals as equally important objectives. Report 60-row interim operations; at least 252 mature future rows before promotion consideration, with predeclared market multiplicity correction.
- [ ] Luna executes tests and runtime checks; Terra reviews and commit.

### Task 5: Workflow, durable state and calendar UI

**Files:** daily GitHub Actions workflow, `docs` UI/assets, allowlisted JSON exports and integration tests.

- [ ] Write behavior tests for hard 120-second fetch budget, one retry, failure preserving base outputs, durable history round-trip and compact public schema.
- [ ] Add 07:50 collection with 10-second request limit/one retry, cached conditional fetches and source failures. Persist calendar/release versions and fit snapshots as durable versioned state; caches are optional accelerators, never the sole evidence copy.
- [ ] Split prepare/final modes, bind the frozen input manifest to its actual receipt day/hash, and add validation-only/manual latest modes. Test KST Sunday exclusion and UTC previous-day weekday mapping, missing/late prepare state, public standard-runner runtime and state-size accounting. Produce the budget report and actual-clock final refresh without bypassing market gates.
- [ ] Render next 30 days/recent 7 days with confirmed/date-only/TBA/revised states, local and UTC times, actuals and units, missing consensus, source links and research readiness. Preserve existing cards/API JSON consumers.
- [ ] Run relevant full integration checks, static UI smoke, import replay, latency report and independent Terra release audit. Record state in this plan and ignored ledger; the original user's dirty `.codex/STATE.md` is excluded from edits/synchronization/commits. Luna stages only reviewed paths and commits the completed changes.

## Evidence requirements

Every new model/feature report records the economic hypothesis, access path, true publication/receipt timestamp, cost, missingness denominator, history length, leakage risks, aligned baseline increase, regime performance and ablation. Unmeasured fields remain explicitly unmeasured. Historical/current-source reconstruction does not become independent validation through a label change.

## Implemented operating contract and release checklist

- KST Monday–Saturday preparation at07:50 (`50 22 * * 0-5`) and final invocation at08:00 (`0 23 * * 0-5`). Sunday is excluded by both schedule and actual-clock gate. `validate` exercises deterministic synthetic integration and bounded actual official collection without publishing forecasts. `latest` uses actual receipts and only markets whose next session has not opened; an ineligible US run retains its previously verified publication.
- Preparation files have content-addressed private copies and a first-writer same-day manifest. Final mode verifies source/config fingerprint, receipt-before08:00, date, content hashes and actual market context; it does not fetch replacement prices. Preparation missing/late/incompatible means not-ready. Public forecast context is kept distinct from the fixed model's readiness and retrospective reference.
- Public Actions cache access is not private. A dedicated `FORECAST_CACHE_KEY` (32 random bytes, base64) authenticates/encrypts a restricted input bundle with AES-256-GCM from `cryptography`. Existing secrets are not rotated. Cache absence, missing key, authentication failure, or expired preparation never creates a model. Only `.forecast-cache/state.enc` is saved. Old owned plaintext `premarket-forecast-history-*` caches are removed only after encrypted restore is verified; pip and unrelated caches remain.
- Source collection has an outer process-group120-second deadline and internal118-second budget, per-request10seconds/one retry and response size limits. Timeout preserves the prior public calendar view and records a failed attempt; partial/unavailable sources remain explicit. Normal ingestion uses actual collection completion as knowledge time. Current API values do not become historical first-release evidence.
- Fit model/failure state shares one atomic first-writer slot per scheduled anchor. A failed actual anchor stays unavailable until the next scheduled anchor; a preliminary/no-capture read does not seal failure. Public model JSON includes fitted parameters and hashes, never training rows or raw responses. Private SQLite is outside `docs` and public uploads. Pages deployment copies only tracked, reviewed documents.
- The 14-feature candidate is fixed `linear:price_macro_v1`, policy `observed_current_receipt_v1`. All historical features are queried at their own cutoff. Its matched `linear:price` ablation uses identical complete prediction/training dates. Synthetic positive-path tests establish executable fit/evaluate/forecast behavior, not market accuracy. Unknown release definitions/units/periods remain unavailable.
- Forward protocols are registered before verified publication. Actual issue time is the checksum-verified manifest's `publication_checked_at`. Primary, incumbent reference and numeric shadow are separate cohorts; failures/replays/late or nonfinal runs are attempts. First outcomes require the published target close and actual mature receipt; later revisions never overwrite the scored first observation. Probability baselines and interval variants are scored on aligned mature pairs; missing predictions are not zeroes. Reports retain Brier/log-loss, MAE, calibration bins,80% interval score, width, coverage, both tails, denominators and paired block-bootstrap intervals. At least252 future mature observations and independent review are necessary; no automatic promotion.
- Final release requires whole-scope Terra verification, real GitHub validation-only/prepare or eligible latest operation, encrypted state restore, current eligible Korean forecast and honest US status, Pages render/link, and Actions budget evidence (actual run count/runtime, cache/artifact/model bytes,30-day worst estimate, standard public runner only; account-wide unrelated usage remains unknown). Artifact retention is14days; forecast job timeout25minutes and Pages10minutes.

Final preparation refinement: the immutable manifest includes the economicSQLite snapshot (including coverage/collection state) and both market fitted-model bundles, in addition to priceSQLite/USCSV. Final execution stages only those verified bytes and disables currentGit economic-history imports. New actual anchor artifacts are first-writer exported back to reviewed durable model paths; a synthetic real-fit integration verifies next-day restoration. Writer and reader share a lock and require a postmanifest completion receipt, preventing a crash or after08 completion from masquerading as a finished preparation. Cache decoding accepts canonical256-bit standard/URL-safe Base64 with or without its single padding character; malformed keys fail closed without reading or rotating the secret.

Final mode budgets: prepare6minutes, validation10minutes, predict/latest25minutes and Pages5minutes. A30-day interval has at most26 non-Sundays, giving a scheduled upperbound of1066runner-minutes (26×31 forecast/preparation plus52×5 Pages); manual runs are additional and explicitly counted in the final audit. These are conservative time caps, not observed consumption or account-wide billing totals.
