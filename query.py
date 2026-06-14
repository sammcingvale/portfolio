"""
question-answering layer over the database.

importable from notebooks:
    from query import position, performance, allocation, accounts

or run from the command line:
    python query.py holdings
    python query.py position NVDA
    python query.py performance NVDA --months 6
    python query.py allocation
    python query.py accounts
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import pandas as pd

from portfolio_db import get_connection


def _df(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def latest_date() -> str | None:
    df = _df("SELECT snapshot_date FROM v_latest_date")
    return df.iloc[0, 0] if not df.empty else None


def holdings() -> pd.DataFrame:
    """every position as of the latest snapshot, across all accounts."""
    return _df(
        """SELECT account_id, ticker, description, shares, cost_basis, market_value
           FROM v_latest_holdings ORDER BY market_value DESC"""
    )


def allocation() -> pd.DataFrame:
    """holdings rolled up by ticker with portfolio weight."""
    return _df("SELECT * FROM v_position_by_ticker")


def accounts() -> pd.DataFrame:
    """total market value per account."""
    return _df("SELECT * FROM v_account_value")


def position(ticker: str) -> pd.DataFrame:
    """how much of `ticker` you own, broken out by account."""
    return _df(
        """SELECT account_id, ticker, description, shares, cost_basis, market_value
           FROM v_latest_holdings WHERE ticker = ? ORDER BY market_value DESC""",
        (ticker.upper(),),
    )


def performance(ticker: str, months: int = 6) -> dict:
    """price performance of `ticker` over a trailing window, from the prices table."""
    ticker = ticker.upper()
    start = (date.today() - timedelta(days=int(months * 30.44))).isoformat()
    prices = _df(
        """SELECT date, close FROM prices
           WHERE ticker = ? AND date >= ? ORDER BY date""",
        (ticker, start),
    )
    if prices.empty:
        return {"ticker": ticker, "error": "no price history — run ingest/yf_prices.py"}

    first, last = prices.iloc[0], prices.iloc[-1]
    change = (last["close"] - first["close"]) / first["close"] * 100

    pos = position(ticker)
    shares = float(pos["shares"].sum()) if not pos.empty else 0.0
    return {
        "ticker": ticker,
        "window_months": months,
        "start_date": first["date"],
        "start_price": round(float(first["close"]), 2),
        "end_date": last["date"],
        "end_price": round(float(last["close"]), 2),
        "pct_change": round(float(change), 2),
        "shares_held": shares,
        "current_value": round(shares * float(last["close"]), 2),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print(df: pd.DataFrame) -> None:
    if df.empty:
        print("(no rows — have you imported holdings yet?)")
    else:
        print(df.to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="ask questions about the portfolio")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("holdings", help="all positions, latest snapshot")
    sub.add_parser("allocation", help="weights by ticker")
    sub.add_parser("accounts", help="value per account")
    p_pos = sub.add_parser("position", help="one ticker, by account")
    p_pos.add_argument("ticker")
    p_perf = sub.add_parser("performance", help="trailing performance of a ticker")
    p_perf.add_argument("ticker")
    p_perf.add_argument("--months", type=int, default=6)
    args = parser.parse_args()

    d = latest_date()
    if d is None and args.cmd != "performance":
        raise SystemExit("no holdings yet — import a Fidelity CSV first:\n"
                         "  python ingest/csv_bootstrap.py data/your_export.csv")
    if d:
        print(f"# latest snapshot: {d}\n")

    if args.cmd == "holdings":
        _print(holdings())
    elif args.cmd == "allocation":
        _print(allocation())
    elif args.cmd == "accounts":
        _print(accounts())
    elif args.cmd == "position":
        _print(position(args.ticker))
    elif args.cmd == "performance":
        result = performance(args.ticker, args.months)
        for k, v in result.items():
            print(f"{k:>16}: {v}")


if __name__ == "__main__":
    main()
