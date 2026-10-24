# Codex implementation instructions

Read `AGENTS.md` and `.codex/prompts/00_master.md` first. The first milestone is a verifiable local SQLite system with explicit missing-data output, not dashboards that imply live data.

## Start commands
```bash
python3 -m smartmoney doctor
python3 -m smartmoney status
python3 -m unittest discover -s tests -v
```

## Task sequence
1. Reproduce schema and seed-only DB; lock down source/units/UTC timestamps.
2. Add KRX licensed market feed and KRX authorized manual CSV export, test dates & amounts.
3. Add DART official significant holdings, SEC13F TSV, FINRA off-exchange short-sale volume, CFTC TFF.
4. Add static HTML adapter behind HTTPS exact-host/robots/TOS rights gate and explicit mapping.
5. Add PIT signals, source citations, conservative unknown label and audit trail.
6. Compare 13F amendments, split-adjustment, short-interest, benchmark and futures as independent later modules; don't promise these are already implemented.

## Signoff
Require PM+Data+DB+QA+Compliance reviews. No unstated internet access, no hardcoded secrets, no fabricated actual transaction records. New features must include tests and docs.
