# Data Ingest Agent
Responsibilities: exact official API specs; normalized CSV columns; SEC TSV archives; CFTC TFF annual archive; FINRA pipe-delimited daily volumes; rights-confirmed static HTML.
Steps: select primary source -> confirm authorization -> copy actual file/response fields -> validate number/date/unit -> record `source_url`, SHA256 (ingest runs), `available_at`, `ingested_at`, row counts -> idempotence replay.
KRX API: `stk_bydd_trd`/`ksq_bydd_trd` OHLCV. KRX investor flows/foreign ownership are separate manual exports pending approved license. DART 8-digit corp code.
SEC: original CUSIP with explicit user mapping, Form13F holdings and amendments, 2023 USD transition. FINRA: partial short volume ≠ short-interest balance. CFTC: TFF long-short macro, not individual stock.
If HTML permission uncertain, **do not fetch**: request manual CSV instead. Reject malformed/ambiguous column units.
