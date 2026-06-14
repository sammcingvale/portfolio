"""
pull daily closing prices from yfinance for every ticker we hold, and store the
history in the `prices` table. this is the data that answers "how has X performed".

usage:
    python ingest/yf_prices.py                 # all held tickers, last ~2 years
    python ingest/yf_prices.py --period 5y
    python ingest/yf_prices.py --tickers NVDA AAPL

cash / money-market symbols (and anything yfinance can't resolve) are skipped and
reported at the end rather than failing the run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_db import get_connection, init_db  # noqa: E402

# symbols that aren't tradable tickers on yfinance — skip without complaint
KNOWN_NON_TICKERS = {"CASH", "SPAXX", "FDRXX", "FZFXX", "FCASH", "FNSXX"}


def held_tickers() -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM holdings_snapshots ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    return [r["ticker"] for r in rows if r["ticker"] not in KNOWN_NON_TICKERS]


def fetch_and_store(tickers: list[str], period: str) -> tuple[int, list[str]]:
    conn = get_connection()
    written = 0
    failed: list[str] = []
    try:
        for ticker in tickers:
            try:
                hist = yf.Ticker(ticker).history(period=period, auto_adjust=False)
            except Exception:
                failed.append(ticker)
                continue
            if hist is None or hist.empty or "Close" not in hist:
                failed.append(ticker)
                continue
            rows = [
                (idx.date().isoformat(), ticker, float(close))
                for idx, close in hist["Close"].items()
                if close == close  # skip NaN
            ]
            conn.executemany(
                """INSERT INTO prices (date, ticker, close) VALUES (?, ?, ?)
                   ON CONFLICT(date, ticker) DO UPDATE SET close = excluded.close""",
                rows,
            )
            written += len(rows)
        conn.commit()
    finally:
        conn.close()
    return written, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="pull daily prices from yfinance")
    parser.add_argument("--period", default="2y", help="yfinance period (e.g. 6mo, 1y, 5y, max)")
    parser.add_argument("--tickers", nargs="*", help="specific tickers (default: all held)")
    args = parser.parse_args()

    init_db()
    tickers = args.tickers or held_tickers()
    if not tickers:
        raise SystemExit("no tickers found — import holdings first (csv_bootstrap.py).")

    print(f"fetching {len(tickers)} tickers for period={args.period} ...")
    written, failed = fetch_and_store(tickers, args.period)
    print(f"stored {written} price rows across {len(tickers) - len(failed)} tickers")
    if failed:
        print(f"skipped (no yfinance data): {', '.join(failed)}")


if __name__ == "__main__":
    main()
