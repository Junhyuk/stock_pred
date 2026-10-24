# Database Agent
SQLite `smartmoney/schema.sql` is the source of truth. Use WAL and foreign keys, additive migrations (schema_version). Don't delete archived source records without explicit approval.
Each observation stores source, reference URL, availability timestamp, ingestion time, ingestion run ID. Market/ticker/time/source are unique keys for common tables.
Treat CUSIP history, split adjustments and corrected filings as first-class temporal data. UTC ISO-8601 timezone for timestamps, ISO-8601 for dates.
If future migration changes semantics, back up `.sqlite` before running and add migration tests; never silently reinterpret 13F value units.
