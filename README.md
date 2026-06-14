# portfolio

a small, local toolkit for understanding an equity portfolio. it keeps a dated history of **what you own** and **how those holdings have performed**, so you can ask questions like _"how much NVDA do I own and how has it done over the last 6 months?"_ and build your own reports.

## how it works

it's really two separate data streams that meet in one small database:

| stream | what it is | where it comes from |
| --- | --- | --- |
| **holdings** | what you own, per account (shares, cost basis, value) | Fidelity — via a CSV export today, or automated daily via SnapTrade |
| **prices** | daily closing prices per ticker, for performance | [yfinance](https://pypi.org/project/yfinance/) (free) |

everything lands in a local **SQLite** file (`portfolio.db`). holdings are stored as dated **snapshots** (one row per position per day), so history is preserved and "what did I own 6 months ago" is answerable. on top of that you can ask questions three ways: in plain language (have Claude query the db), in a **Jupyter notebook**, or by publishing views out to **Google Sheets**.

> **privacy:** your real financial data **never** gets committed. the database, all CSV exports, and your `.env` secrets are gitignored. only code is tracked. see [`.gitignore`](.gitignore).

## project layout

```
portfolio.db            the SQLite database (local only, gitignored)
db/schema.sql           tables + convenience views
portfolio_db.py         shared db connection / init helpers
ingest/
  csv_bootstrap.py      import a Fidelity "Portfolio Positions" CSV  ← start here
  yf_prices.py          pull daily price history from yfinance
  snaptrade_holdings.py automated daily holdings sync (Fidelity via SnapTrade)
query.py                ask questions (CLI + importable functions)
data/                   drop Fidelity CSV exports here (gitignored)
notebooks/              your Jupyter reports
sheets/                 scripts that publish report views to Google Sheets
```

## setup

```bash
git clone <this-repo> portfolio && cd portfolio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## quick start (no accounts or API keys needed)

1. **export your positions from Fidelity:** Fidelity.com → _Positions_ → the
   **Download** link. that single CSV includes every account (SMAs included).
   move it into `data/`.
2. **import it:**
   ```bash
   python ingest/csv_bootstrap.py data/Portfolio_Positions_Jun-14-2025.csv
   ```
3. **pull price history:**
   ```bash
   python ingest/yf_prices.py
   ```
4. **ask questions:**
   ```bash
   python query.py holdings              # everything you own, latest snapshot
   python query.py position NVDA         # NVDA broken out by account
   python query.py performance NVDA --months 6
   python query.py allocation            # weights by ticker
   python query.py accounts              # value per account
   ```

re-run steps 1–3 whenever you want a fresh snapshot; each import is a new dated row,
so your history accumulates.

## automated daily (Fidelity via SnapTrade)

once the CSV flow proves out, you can have holdings refresh automatically each day instead of exporting by hand. [SnapTrade](https://snaptrade.com/) connects Fidelity read-only (positions & balances; it cannot trade).

1. create a SnapTrade developer account → get a **clientId** and **consumerKey**.
2. `cp .env.example .env` and fill those two values in.
3. register yourself and link Fidelity:
   ```bash
   python ingest/snaptrade_holdings.py --connect
   ```
   this prints a `SNAPTRADE_USER_SECRET` (save it into `.env`) and a URL — open it to authorize Fidelity.
4. from then on, a daily snapshot is one command:
   ```bash
   python ingest/snaptrade_holdings.py && python ingest/yf_prices.py
   ```
   schedule those two lines (cron / launchd) to run each morning.

## using it from a notebook

```python
from query import holdings, position, performance, allocation
allocation()                      # a pandas DataFrame, ready to chart
performance("NVDA", months=6)
```

## roadmap

- [x] SQLite core + Fidelity CSV import + yfinance prices + query layer
- [ ] SnapTrade automated daily sync
- [ ] notebook with standard charts (allocation, performance, gains)
- [ ] publish daily report views to Google Sheets
