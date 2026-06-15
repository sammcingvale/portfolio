"""
portfolio scoreboard — the top-line highlights, meant to be iterated on.

run:
    python scoreboard.py

or from a notebook:
    from scoreboard import scoreboard
    s = scoreboard()        # dict of the numbers, ready to render

what it shows today:
  * total equity exposure ($) — excludes bonds and cash
  * YTD and TTM performance of the current equity basket
  * net additions / withdrawals — PENDING until transaction history is loaded

note on performance: we currently have a single holdings snapshot, so "performance"
here is the price return of your CURRENT equity holdings over the window (what
today's shares would have done) — not a true money-weighted return, which needs
the trades you made mid-period. it converges to the real thing as daily snapshots
(or transactions) accumulate.
"""

from __future__ import annotations

import pandas as pd

from classify import classify
from portfolio_db import get_connection
from returns import money_weighted_return


def _latest_holdings() -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            """SELECT ticker, MAX(description) AS description,
                      SUM(market_value) AS market_value
               FROM v_latest_holdings GROUP BY ticker""",
            conn,
        )
    finally:
        conn.close()
    df["bucket"] = [classify(t, d) for t, d in zip(df.ticker, df.description)]
    return df


def _asof_prices(tickers: list[str]) -> pd.DataFrame:
    """date x ticker matrix of closes, forward-filled so every date has a price."""
    conn = get_connection()
    try:
        q = f"""SELECT date, ticker, close FROM prices
                WHERE ticker IN ({','.join('?' * len(tickers))})"""
        prices = pd.read_sql_query(q, conn, params=tickers, parse_dates=["date"])
    finally:
        conn.close()
    wide = prices.pivot_table(index="date", columns="ticker", values="close")
    return wide.sort_index().ffill()


def _basket_return(holdings: pd.DataFrame, wide: pd.DataFrame, start: pd.Timestamp) -> dict:
    """value-weighted price return of the current equity basket since `start`."""
    end_date = wide.index.max()
    start_date = wide.index[wide.index >= start].min()
    start_row, end_row = wide.loc[start_date], wide.loc[end_date]

    rows = []
    for _, h in holdings.iterrows():
        t = h.ticker
        s, e = start_row.get(t), end_row.get(t)
        if pd.notna(s) and pd.notna(e) and s > 0:
            start_val = h.market_value * (s / e)        # value of today's shares at start
            rows.append((h.market_value, start_val))
    covered_now = sum(c for c, _ in rows)
    covered_start = sum(s for _, s in rows)
    total_now = holdings.market_value.sum()

    pct = (covered_now / covered_start - 1) * 100 if covered_start else None
    return {
        "start_date": start_date.date().isoformat(),
        "end_date": end_date.date().isoformat(),
        "pct_change": round(pct, 2) if pct is not None else None,
        "dollar_change": round(covered_now - covered_start),
        "coverage_pct": round(100 * covered_now / total_now, 1) if total_now else 0,
    }


def _net_additions(start: str, end: str) -> dict | None:
    """net external flows (deposits - withdrawals, transfers) over [start, end].

    returns None if no transaction history has been loaded yet."""
    conn = get_connection()
    try:
        has_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
        if not has_tbl or not conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]:
            return None
        # the window is only as trustworthy as the loaded history — if transactions
        # begin after `start`, the figure is partial and we say so
        first_tx = conn.execute("SELECT MIN(run_date) FROM transactions").fetchone()[0]
        effective_start = max(start, first_tx)
        net = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM transactions
               WHERE is_external_flow = 1 AND run_date >= ? AND run_date <= ?""",
            (effective_start, end),
        ).fetchone()[0]
        # transferred securities sometimes post with no cash Amount — flag if any exist
        untracked = conn.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE is_external_flow = 1 AND COALESCE(amount, 0) = 0
                 AND run_date >= ? AND run_date <= ?""",
            (effective_start, end),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "net": round(net),
        "untracked_transfers": untracked,
        "from": effective_start,
        "partial": first_tx > start,
    }


def scoreboard() -> dict:
    holdings = _latest_holdings()
    equity = holdings[holdings.bucket == "equity"].copy()

    by_bucket = holdings.groupby("bucket").market_value.sum().to_dict()
    equity_total = by_bucket.get("equity", 0.0)

    wide = _asof_prices(equity.ticker.tolist())
    end_date = wide.index.max()
    end_iso = end_date.date().isoformat()
    ytd = _basket_return(equity, wide, pd.Timestamp(end_date.year, 1, 1))
    ttm = _basket_return(equity, wide, end_date - pd.Timedelta(days=365))

    return {
        "as_of": end_iso,
        "equity_exposure": round(equity_total),
        "bonds": round(by_bucket.get("bond", 0.0)),
        "cash": round(by_bucket.get("cash", 0.0)),
        "ytd": ytd,
        "ttm": ttm,
        "net_additions_ytd": _net_additions(f"{end_date.year}-01-01", end_iso),
        "net_additions_ttm": _net_additions(ttm["start_date"], end_iso),
        "money_weighted": money_weighted_return(),  # rigorous once >=2 snapshots exist
    }


def _fmt(n) -> str:
    return f"${n:,.0f}" if n is not None else "—"


def _pct(p: dict) -> str:
    if p["pct_change"] is None:
        return "n/a"
    sign = "+" if p["pct_change"] >= 0 else ""
    note = "" if p["coverage_pct"] >= 99 else f"  ({p['coverage_pct']}% of equity priced)"
    return f"{sign}{p['pct_change']}%   {sign}{_fmt(p['dollar_change'])}{note}"


def main() -> None:
    s = scoreboard()
    line = "─" * 52
    print(f"\n  PORTFOLIO SCOREBOARD            as of {s['as_of']}")
    print(f"  {line}")
    print(f"  total equity exposure     {_fmt(s['equity_exposure']):>18}")
    print(f"    YTD ({s['ytd']['start_date']} →)     {_pct(s['ytd'])}")
    print(f"    TTM ({s['ttm']['start_date']} →)     {_pct(s['ttm'])}")
    print(f"  {line}")
    na_ytd, na_ttm = s["net_additions_ytd"], s["net_additions_ttm"]
    if na_ytd is None:
        print(f"  net additions / withdrawals      PENDING (needs transactions)")
    else:
        def _flow(na):
            sign = "+" if na["net"] >= 0 else ""
            notes = []
            if na["partial"]:
                notes.append(f"partial — history starts {na['from']}")
            if na["untracked_transfers"]:
                notes.append(f"{na['untracked_transfers']} transfers $-unvalued")
            note = f"  ({'; '.join(notes)})" if notes else ""
            return f"{sign}{_fmt(na['net'])}{note}"
        print(f"  net additions / withdrawals")
        print(f"    YTD                     {_flow(na_ytd)}")
        print(f"    TTM                     {_flow(na_ttm)}")
    print(f"  {line}")
    mwr = s["money_weighted"]
    if "error" in mwr:
        print(f"  equity money-weighted return     PENDING (needs a 2nd holdings snapshot)")
    else:
        sign = "+" if (mwr["money_weighted_return_annualized"] or 0) >= 0 else ""
        print(f"  equity money-weighted return  {mwr['start_date']} → {mwr['end_date']}")
        print(f"    annualized (XIRR)       {sign}{mwr['money_weighted_return_annualized']}%")
        print(f"    investment gain         {sign if mwr['investment_gain']>=0 else ''}"
              f"{_fmt(mwr['investment_gain'])}   (markets, net of your deposits/withdrawals)")
    print(f"  {line}")
    print(f"  for context — excluded from equity exposure:")
    print(f"    bonds                   {_fmt(s['bonds']):>18}")
    print(f"    cash & money market     {_fmt(s['cash']):>18}")
    print()


if __name__ == "__main__":
    main()
