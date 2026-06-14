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
import re
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_db import get_connection, init_db  # noqa: E402

# cash / money-market symbols that aren't tradable tickers — skip silently
KNOWN_NON_TICKERS = {"CASH", "SPAXX", "FDRXX", "FZFXX", "FCASH", "FNSXX"}

# Fidelity sometimes uses a symbol Yahoo doesn't recognize — most often class
# shares it concatenates with no separator (BRKB) or ADRs listed under a foreign
# ticker. there's no rule to infer these, so map the known ones by hand. add more
# as you spot them in the "no Yahoo data" output.
SYMBOL_OVERRIDES = {
    "BRKB": "BRK-B",   # Berkshire Hathaway class B
    "BFB": "BF-B",     # Brown-Forman class B
    "HEIA": "HEINY",   # Heineken — Amsterdam line vs US ADR
}

# a Yahoo-resolvable US symbol: 1-5 letters with an optional class suffix (BRK.B).
# anything else in an SMA-heavy book is almost always a bond CUSIP (e.g. 436CVR021)
# or a description line — Yahoo can't price those, so we don't even ask.
YAHOO_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

# download in chunks so 1000+ tickers don't go one-at-a-time (and don't get throttled)
CHUNK_SIZE = 100


def classify_tickers() -> tuple[list[str], list[str]]:
    """split held symbols into (yahoo candidates, non-exchange symbols we skip)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM holdings_snapshots ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    candidates, skipped = [], []
    for r in rows:
        t = r["ticker"]
        if t in KNOWN_NON_TICKERS:
            continue  # cash — skip silently
        (candidates if YAHOO_TICKER.match(t) else skipped).append(t)
    return candidates, skipped


def _store(conn, ticker: str, closes: "pd.Series") -> int:
    rows = [
        (idx.date().isoformat(), ticker, float(v))
        for idx, v in closes.items()
        if pd.notna(v)
    ]
    if rows:
        conn.executemany(
            """INSERT INTO prices (date, ticker, close) VALUES (?, ?, ?)
               ON CONFLICT(date, ticker) DO UPDATE SET close = excluded.close""",
            rows,
        )
    return len(rows)


def _yahoo_symbol(ticker: str) -> str:
    """translate a Fidelity symbol to what Yahoo expects."""
    if ticker in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[ticker]
    return ticker.replace(".", "-")  # class shares: BRK.B -> BRK-B


def fetch_and_store(tickers: list[str], period: str) -> tuple[int, list[str]]:
    """batch-download closes and upsert them; return (rows_written, no_data_tickers)."""
    conn = get_connection()
    written = 0
    no_data: list[str] = []
    try:
        for i in range(0, len(tickers), CHUNK_SIZE):
            chunk = tickers[i:i + CHUNK_SIZE]
            yh = {_yahoo_symbol(t): t for t in chunk}  # yahoo symbol -> our ticker
            print(f"  fetching {i + 1}-{i + len(chunk)} of {len(tickers)} ...")
            data = yf.download(
                list(yh), period=period, auto_adjust=False, progress=False,
                group_by="ticker", threads=True,
            )
            for ysym, ticker in yh.items():
                try:
                    # multi-ticker frames are MultiIndexed by (ticker, field);
                    # a single surviving ticker collapses to plain columns
                    if isinstance(data.columns, pd.MultiIndex):
                        closes = data[ysym]["Close"]
                    else:
                        closes = data["Close"]
                except (KeyError, TypeError):
                    no_data.append(ticker)
                    continue
                n = _store(conn, ticker, closes)  # store under our ticker, not yahoo's
                (no_data.append(ticker) if n == 0 else None)
                written += n
        conn.commit()
    finally:
        conn.close()
    return written, no_data


def main() -> None:
    parser = argparse.ArgumentParser(description="pull daily prices from yfinance")
    parser.add_argument("--period", default="2y", help="yfinance period (e.g. 6mo, 1y, 5y, max)")
    parser.add_argument("--tickers", nargs="*", help="specific tickers (default: all held)")
    args = parser.parse_args()

    init_db()
    if args.tickers:
        candidates, skipped = [t.upper() for t in args.tickers], []
    else:
        candidates, skipped = classify_tickers()
    if not candidates:
        raise SystemExit("no exchange tickers found — import holdings first (csv_bootstrap.py).")

    if skipped:
        print(f"skipping {len(skipped)} non-exchange symbols (bonds/CUSIPs/SMA lines) — "
              f"these keep their Fidelity market value but get no price history.")
    print(f"fetching prices for {len(candidates)} tickers, period={args.period} ...")
    written, no_data = fetch_and_store(candidates, args.period)
    print(f"\nstored {written} price rows across {len(candidates) - len(no_data)} tickers")
    if no_data:
        preview = ", ".join(no_data[:15]) + (" ..." if len(no_data) > 15 else "")
        print(f"no Yahoo data for {len(no_data)} (likely delisted/foreign): {preview}")


if __name__ == "__main__":
    main()
