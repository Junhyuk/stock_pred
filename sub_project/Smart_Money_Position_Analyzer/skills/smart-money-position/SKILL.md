---
name: smart-money-position
summary: Public-source KR foreign institutional flow and US 13F position evidence, SQLite ingestion, conservative point-in-time analysis.
---

# Smart Money Position Skill

## When to use
User asks about KR foreign/institution accumulation, 13F managers' quarterly holdings, FINRA short-sale volume, COT market positioning, official API/CSV ingestion, local DB or Codex research workflow.

## Procedure
1. Determine KR vs US and requested date/cutoff. Confirm if requested data exists in SQLite with `smartmoney status` and source coverage.
2. Select official API/ZIP first, licensed CSV export second, explicitly permitted static HTML last. Record URL, time, units, parser and ingestion checksum.
3. Use `python3 -m smartmoney analyze --market KR|US --symbol TICKER` and capture JSON.
4. State `관측 / 추정 / 미확인`, label/confidence/evidence/contradictions/missing as supplied; don't upgrade confidence by rhetoric.
5. If data missing, identify source and exact user steps. Do not fabricate. For historical backtests exclude records whose `available_at` is after cutoff.
6. For 13F: quarterly snapshots, filing delays, USD units after 2023-01-03; short/derivative exposure absent. For FINRA: short-sale volume is not outstanding short interest.
7. Store findings and QA trail, not trade execution orders.

## Safety
No insider/private portfolio recovery, non-consensual account access, unauthorised scraping, misleading future-return probability or investment performance claims. Read `docs/VALIDATION_PLAN.md` prior to prediction studies.

## Commands
```bash
python3 -m smartmoney status
python3 -m smartmoney import-csv kr-flow file.csv --investor FOREIGN
python3 -m smartmoney analyze --market KR --symbol 005930
python3 -m smartmoney import-sec13f downloaded.zip --manager-cik 1067983
python3 -m unittest discover -s tests -v
```
