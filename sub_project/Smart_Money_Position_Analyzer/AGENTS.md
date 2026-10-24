# Repository Agent Contract — Smart Money Position Analyzer

## Mission / boundaries
- KR foreign/institution data and US filed institutional holding data from public, licensed sources only.
- Never claim to know a specific actor's hidden trading position or their trade timestamps.
- Strict point-in-time availability and primary-source URLs; missing input must remain missing.
- No broker access, execution, private trading data, bypassing authentication or limits.
- Users can run the project fully offline; network collectors opt in per command.

## Responsibility routing
1. `agents/PM_AGENT.md`: approves scope, operational UX, release decision.
2. `agents/DATA_INGEST_AGENT.md`: KRX/DART/SEC/CFTC/FINRA CSV/API/ZIP/allowlisted HTML.
3. `agents/DATABASE_AGENT.md`: schema, migrations, duplicates, metadata/PIT lineage.
4. `agents/SIGNAL_AGENT.md`: explainable statistics, uncertainty, corporate actions.
5. `agents/COMPLIANCE_AGENT.md`: provider terms, privacy, rate limits and key handling.
6. `agents/QA_AGENT.md`: synthetic offline tests, failure modes, data audits.

## Development order
1. Read `README.md`, `docs/DATA_REQUIREMENTS.md`, `docs/SOURCE_CATALOG.md`.
2. Demonstrate `python3 -m smartmoney doctor`, `status`, and `python3 -m unittest discover -s tests -v`.
3. Add one source at a time, recording original headers, sample payload, authentication & license constraints.
4. Run input validation and idempotence tests before adding signals.
5. Compare signals only using data available at cutoff; document news latency and filings corrections.
6. Generate evidence reports citing source URLs; review with QA and Compliance agents.

## Invariants / stop conditions
- SEC 13F after Jan 3 2023 is USD, earlier filings can be thousand USD. `VALUE` is holding valuation, not net purchase.
- FINRA daily short volume is not short interest; COT market futures is not individual stock data.
- KRX official stock daily API has OHLCV, not investor flow. Investor flow import is manually verified CSV until proper licensed API obtained.
- No fake backfill or invented same-account linkage.
- No guessed CUSIP→ticker association; user verification required.
- When publication timestamp unknown, use ingestion timestamp, not historical report_date as publication time.
- Before making web requests: verify permission; no headless stealth, reverse-engineered endpoints, captcha bypass, auth cookies, high-volume scraping.
- API tokens never in code, logs, saved query URL, DB or checked-in fixtures.
- Dependencies must stay standard Python library for first release unless approved by PM + QA.

## Definition of done
- All offline tests pass (including SEC value unit, PIT, idempotence, invalid CSV, HTML parser).
- Reproducible DB initialized with empty market observations.
- Docs include a real user-operated collection route, sample CLI, operational caveat.
- Every signal shows evidence, conflicts, missing data and data cutoff; insufficient data returns inconclusive.
