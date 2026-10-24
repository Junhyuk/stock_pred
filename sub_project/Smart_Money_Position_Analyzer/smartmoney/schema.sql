PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, installed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  code TEXT PRIMARY KEY, provider TEXT NOT NULL, display_name TEXT NOT NULL,
  home_url TEXT NOT NULL, method TEXT NOT NULL,
  legal_note TEXT NOT NULL, cadence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (
  market TEXT NOT NULL CHECK (market IN ('KR','US')),
  symbol TEXT NOT NULL, name TEXT NOT NULL, currency TEXT NOT NULL,
  exchange TEXT, cusip TEXT, dart_corp_code TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(market, symbol));
CREATE TABLE IF NOT EXISTS watchlist (
  market TEXT NOT NULL, symbol TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 1,
  notes TEXT, PRIMARY KEY(market,symbol),
  FOREIGN KEY(market,symbol) REFERENCES instruments(market,symbol));
CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_code TEXT NOT NULL,
  source_url TEXT, content_sha256 TEXT, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL CHECK(status IN ('running','ok','failed')),
  rows_seen INTEGER DEFAULT 0, rows_written INTEGER DEFAULT 0, message TEXT,
  FOREIGN KEY(source_code) REFERENCES sources(code));
CREATE TABLE IF NOT EXISTS prices (
  market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL NOT NULL CHECK(close>=0),
  volume REAL CHECK(volume>=0), traded_value REAL, listed_shares REAL,
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
  ingest_run_id INTEGER, PRIMARY KEY(market,symbol,trade_date,source_code));
CREATE INDEX IF NOT EXISTS idx_price_time ON prices(market,symbol,trade_date,available_at);
CREATE TABLE IF NOT EXISTS kr_investor_flows (
  symbol TEXT NOT NULL, trade_date TEXT NOT NULL, investor_type TEXT NOT NULL
    CHECK(investor_type IN ('FOREIGN','INSTITUTION','RETAIL','OTHER')),
  buy_qty REAL, sell_qty REAL, net_qty REAL,
  buy_value REAL, sell_value REAL, net_value REAL,
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
  ingest_run_id INTEGER,
  PRIMARY KEY(symbol,trade_date,investor_type,source_code));
CREATE INDEX IF NOT EXISTS idx_flows_time ON kr_investor_flows(symbol,trade_date,available_at);
CREATE TABLE IF NOT EXISTS kr_foreign_ownership (
  symbol TEXT NOT NULL, trade_date TEXT NOT NULL, held_shares REAL CHECK(held_shares>=0),
  ownership_pct REAL CHECK(ownership_pct BETWEEN 0 AND 100),
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
  ingest_run_id INTEGER, PRIMARY KEY(symbol,trade_date,source_code));
CREATE TABLE IF NOT EXISTS us_short_volume (
  symbol TEXT NOT NULL, trade_date TEXT NOT NULL, reporting_facility TEXT NOT NULL, listing_market TEXT,
  short_volume REAL NOT NULL CHECK(short_volume>=0),
  short_exempt_volume REAL CHECK(short_exempt_volume>=0),
  total_volume REAL NOT NULL CHECK(total_volume>=0),
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
  ingest_run_id INTEGER,
  PRIMARY KEY(symbol,trade_date,reporting_facility,source_code));
CREATE TABLE IF NOT EXISTS short_interest (
  market TEXT NOT NULL CHECK(market IN ('KR','US')), symbol TEXT NOT NULL,
  settlement_date TEXT NOT NULL, shares REAL NOT NULL CHECK(shares>=0),
  days_to_cover REAL, source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
  ingest_run_id INTEGER,
  PRIMARY KEY(market,symbol,settlement_date,source_code));
CREATE TABLE IF NOT EXISTS sec13f_filings (
  accession TEXT PRIMARY KEY, manager_cik TEXT NOT NULL, manager_name TEXT,
  report_period TEXT NOT NULL, filing_date TEXT NOT NULL,
  filing_type TEXT NOT NULL, amendment_type TEXT, is_amendment INTEGER NOT NULL DEFAULT 0,
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL, ingest_run_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_13f_manager ON sec13f_filings(manager_cik,report_period,available_at);
CREATE TABLE IF NOT EXISTS sec13f_holdings (
  accession TEXT NOT NULL, info_sk TEXT NOT NULL, cusip TEXT NOT NULL,
  issuer TEXT, security_class TEXT,
  shares REAL, amount_type TEXT, reported_value_raw REAL, reported_value_usd REAL, value_unit TEXT NOT NULL,
  put_call TEXT, investment_discretion TEXT, other_manager TEXT,
  PRIMARY KEY(accession, info_sk),
  FOREIGN KEY(accession) REFERENCES sec13f_filings(accession));
CREATE INDEX IF NOT EXISTS idx_13f_cusip ON sec13f_holdings(cusip,accession);
CREATE TABLE IF NOT EXISTS cusip_symbol_map (
  cusip TEXT NOT NULL, symbol TEXT NOT NULL, valid_from TEXT NOT NULL,
  valid_to TEXT, verified_at TEXT NOT NULL, mapping_source TEXT NOT NULL,
  PRIMARY KEY(cusip,symbol,valid_from));
CREATE TABLE IF NOT EXISTS dart_major_holdings (
  receipt_no TEXT NOT NULL, corp_code TEXT NOT NULL, symbol TEXT,
  reporter TEXT NOT NULL, report_date TEXT NOT NULL,
  shares REAL, shares_change REAL, ownership_pct REAL,
  ownership_pct_change REAL, report_reason TEXT,
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL, ingest_run_id INTEGER,
  PRIMARY KEY(receipt_no,corp_code,reporter));
CREATE TABLE IF NOT EXISTS cftc_tff (
  report_date TEXT NOT NULL, contract_code TEXT NOT NULL, market_name TEXT NOT NULL,
  open_interest REAL,
  asset_mgr_long REAL, asset_mgr_short REAL,
  leveraged_long REAL, leveraged_short REAL,
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL, ingest_run_id INTEGER,
  PRIMARY KEY(report_date,contract_code,source_code));
CREATE TABLE IF NOT EXISTS corporate_actions (
  market TEXT NOT NULL, symbol TEXT NOT NULL, effective_date TEXT NOT NULL,
  action_type TEXT NOT NULL, split_multiplier REAL,
  source_code TEXT NOT NULL, source_url TEXT,
  available_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
  PRIMARY KEY(market,symbol,effective_date,action_type,source_code));
CREATE TABLE IF NOT EXISTS signal_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL, symbol TEXT NOT NULL,
  evaluated_at TEXT NOT NULL, data_cutoff TEXT NOT NULL,
  label TEXT NOT NULL, confidence TEXT NOT NULL,
  metrics_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  contradictions_json TEXT NOT NULL, missing_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_signals ON signal_snapshots(market,symbol,evaluated_at);
CREATE TABLE IF NOT EXISTS quality_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ingest_run_id INTEGER,
  source_code TEXT NOT NULL, category TEXT NOT NULL,
  detail TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(ingest_run_id) REFERENCES ingest_runs(id));
