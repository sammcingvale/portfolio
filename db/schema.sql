-- ───────────────────────────────────────────────────────────────────────────
-- portfolio schema
--
-- two independent data streams feed everything:
--   1. holdings  — WHAT you own, from Fidelity (via CSV export or SnapTrade)
--   2. prices    — how tickers PERFORM over time, from yfinance
--
-- holdings are stored as dated SNAPSHOTS (one row per position per day), so the
-- history is preserved and questions like "what did i own 6 months ago" just work.
-- ───────────────────────────────────────────────────────────────────────────

-- one row per Fidelity account (taxable, IRA, SMA, etc.)
CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,          -- Fidelity account number (or SnapTrade id)
    name         TEXT,                       -- friendly name, e.g. "Joint Brokerage"
    type         TEXT,                       -- taxable / traditional_ira / roth_ira / sma / ...
    institution  TEXT DEFAULT 'Fidelity',
    is_sma       INTEGER DEFAULT 0,          -- 1 if a separately managed account
    notes        TEXT
);

-- optional ticker metadata (filled opportunistically; not required)
CREATE TABLE IF NOT EXISTS securities (
    ticker       TEXT PRIMARY KEY,
    description  TEXT,
    asset_class  TEXT                         -- equity / etf / mutual_fund / cash / bond / ...
);

-- the core fact table: one row per (date, account, ticker)
CREATE TABLE IF NOT EXISTS holdings_snapshots (
    snapshot_date TEXT NOT NULL,             -- ISO date 'YYYY-MM-DD' of the snapshot
    account_id    TEXT NOT NULL,
    ticker        TEXT NOT NULL,             -- 'CASH' for uninvested cash with no symbol
    description   TEXT,
    shares        REAL,
    cost_basis    REAL,                       -- total cost basis for the position
    market_value  REAL,
    price         REAL,                       -- price as reported by the source at snapshot
    source        TEXT,                       -- 'fidelity_csv' | 'snaptrade'
    PRIMARY KEY (snapshot_date, account_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_holdings_ticker ON holdings_snapshots (ticker);
CREATE INDEX IF NOT EXISTS idx_holdings_date   ON holdings_snapshots (snapshot_date);

-- daily price history per ticker, from yfinance
CREATE TABLE IF NOT EXISTS prices (
    date    TEXT NOT NULL,                   -- ISO date 'YYYY-MM-DD'
    ticker  TEXT NOT NULL,
    close   REAL,
    PRIMARY KEY (date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices (ticker);

-- account activity (deposits, withdrawals, buys, sells, dividends, transfers).
-- this is what separates "money/shares moved in or out" (external flows) from
-- "the market moved" — the basis for net additions and true performance.
CREATE TABLE IF NOT EXISTS transactions (
    run_date        TEXT NOT NULL,           -- ISO date the transaction posted
    account_id      TEXT NOT NULL,
    action          TEXT,                     -- raw Fidelity action text
    category        TEXT,                     -- our classification (see ingest)
    is_external_flow INTEGER DEFAULT 0,       -- 1 if money/shares entered/left the portfolio
    symbol          TEXT,
    description     TEXT,
    quantity        REAL,
    price           REAL,
    amount          REAL,                     -- signed cash impact (+ in / - out)
    settlement_date TEXT,
    source          TEXT DEFAULT 'fidelity_csv',
    -- Fidelity gives no transaction id; dedupe on the natural key so re-imports
    -- of overlapping date ranges don't double-count
    UNIQUE (run_date, account_id, action, symbol, quantity, amount, settlement_date)
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions (run_date);
CREATE INDEX IF NOT EXISTS idx_tx_flow ON transactions (is_external_flow);

-- ───────────────────────────────────────────────────────────────────────────
-- convenience views
-- ───────────────────────────────────────────────────────────────────────────

-- the single most recent snapshot date we have holdings for
CREATE VIEW IF NOT EXISTS v_latest_date AS
    SELECT MAX(snapshot_date) AS snapshot_date FROM holdings_snapshots;

-- authoritative holdings: exactly ONE source per snapshot date. a single day can
-- carry the same portfolio from more than one source (the one-time CSV bootstrap
-- overlapping the automated SnapTrade pull) — summing both double-counts everything
-- downstream. keep only the highest-priority source present that day:
-- snaptrade (live daily feed) > fidelity_csv (bootstrap) > anything else.
CREATE VIEW IF NOT EXISTS v_holdings AS
    SELECT h.*
    FROM holdings_snapshots h
    JOIN (
        SELECT snapshot_date,
               MIN(CASE source WHEN 'snaptrade' THEN 0
                               WHEN 'fidelity_csv' THEN 1 ELSE 2 END) AS pri
        FROM holdings_snapshots
        GROUP BY snapshot_date
    ) p ON p.snapshot_date = h.snapshot_date
       AND CASE h.source WHEN 'snaptrade' THEN 0
                         WHEN 'fidelity_csv' THEN 1 ELSE 2 END = p.pri;

-- every position as of the latest snapshot (single source, via v_holdings)
CREATE VIEW IF NOT EXISTS v_latest_holdings AS
    SELECT * FROM v_holdings
    WHERE snapshot_date = (SELECT snapshot_date FROM v_latest_date);

-- latest holdings rolled up across all accounts, by ticker, with % of portfolio
CREATE VIEW IF NOT EXISTS v_position_by_ticker AS
    SELECT
        ticker,
        MAX(description)                                   AS description,
        SUM(shares)                                        AS shares,
        SUM(cost_basis)                                    AS cost_basis,
        SUM(market_value)                                  AS market_value,
        ROUND(100.0 * SUM(market_value) /
              (SELECT SUM(market_value) FROM v_latest_holdings), 2) AS pct_of_portfolio
    FROM v_latest_holdings
    GROUP BY ticker
    ORDER BY market_value DESC;

-- latest total market value per account
CREATE VIEW IF NOT EXISTS v_account_value AS
    SELECT
        a.account_id,
        a.name,
        a.type,
        a.is_sma,
        SUM(h.market_value) AS market_value
    FROM v_latest_holdings h
    LEFT JOIN accounts a ON a.account_id = h.account_id
    GROUP BY h.account_id
    ORDER BY market_value DESC;
