"""
equity money-weighted (dollar-weighted / XIRR) return.

scoped to the EQUITY sleeve: it answers "what annualized rate did my equity dollars
earn?", accounting for the size and timing of money moving into and out of equities
(buys, sells, dividends, equity transfers). bonds/cash only matter when they cross
that boundary. computing it rigorously needs three things:

    1. a BEGINNING equity value      (a holdings snapshot at the start date)
    2. the equity FLOWS between        (buys/sells/dividends — we have these)
    3. an ENDING equity value          (the latest holdings snapshot)

we always have (2) and (3). (1) requires a SECOND holdings snapshot — these
accumulate automatically once SnapTrade syncs daily (or import an older positions
export now). with one snapshot, this reports PENDING.

run:
    python returns.py                      # uses earliest -> latest snapshot
    python returns.py --start 2025-12-31   # over a specific window

from a notebook:
    from returns import money_weighted_return
    money_weighted_return()
"""

from __future__ import annotations

import argparse

from classify import classify
from portfolio_db import get_connection

# the equity sleeve is treated as its own account. flows across ITS boundary:
#   INTO equity  (contributions): buying equities, reinvesting, equities transferred in
#   OUT of equity (withdrawals):  selling equities, dividends paid out, equities transferred out
# bonds/cash only matter when they cross this line (e.g. a bond sale that buys equity
# shows up as the equity buy). dividends are counted out -> this is a TOTAL return.
EQUITY_FLOW_IN = ("buy", "reinvestment", "transfer_in")
EQUITY_FLOW_OUT = ("sell", "dividend", "transfer_out")


def xirr(cashflows: list[tuple[str, float]], guess: float = 0.1) -> float | None:
    """annualized internal rate of return for dated cashflows [(iso_date, amount)].

    sign convention (investor's view): money you put IN is negative, money/value you
    get OUT is positive. returns the annual rate, or None if it doesn't converge.
    """
    from datetime import date

    if len(cashflows) < 2:
        return None
    dates = [date.fromisoformat(d) for d, _ in cashflows]
    amounts = [a for _, a in cashflows]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None  # need at least one inflow and one outflow
    t0 = min(dates)
    years = [(d - t0).days / 365.0 for d in dates]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** y for a, y in zip(amounts, years))

    def dnpv(rate: float) -> float:
        return sum(-y * a / (1 + rate) ** (y + 1) for a, y in zip(amounts, years))

    # Newton's method, then bisection fallback for robustness
    rate = guess
    for _ in range(100):
        f = npv(rate)
        df = dnpv(rate)
        if abs(df) < 1e-12:
            break
        step = f / df
        rate -= step
        if rate <= -1:
            rate = -0.9999
        if abs(step) < 1e-8:
            return rate
    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fmid = npv(mid)
        if abs(fmid) < 1e-6:
            return mid
        if flo * fmid < 0:
            hi, fhi = mid, fmid
        else:
            lo, flo = mid, fmid
    return (lo + hi) / 2


def _snapshot_dates() -> list[str]:
    conn = get_connection()
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT snapshot_date FROM holdings_snapshots ORDER BY snapshot_date"
        ).fetchall()]
    finally:
        conn.close()


def _equity_value(snapshot_date: str) -> float:
    """total market value of the EQUITY-classified positions in a snapshot."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT ticker, description, market_value FROM holdings_snapshots
               WHERE snapshot_date = ? AND market_value IS NOT NULL""",
            (snapshot_date,),
        ).fetchall()
    finally:
        conn.close()
    return sum(r["market_value"] for r in rows
               if classify(r["ticker"], r["description"]) == "equity")


def _equity_flows(start: str, end: str) -> list[tuple[str, float]]:
    """equity-sleeve cashflows in (start, end], from the investor's perspective:
    contributions (buying equity) negative, withdrawals (selling/dividends) positive."""
    conn = get_connection()
    try:
        cats = EQUITY_FLOW_IN + EQUITY_FLOW_OUT
        rows = conn.execute(
            f"""SELECT run_date, category, symbol, description, amount FROM transactions
                WHERE category IN ({','.join('?' * len(cats))})
                  AND amount IS NOT NULL
                  AND run_date > ? AND run_date <= ?
                ORDER BY run_date""",
            (*cats, start, end),
        ).fetchall()
    finally:
        conn.close()
    flows = []
    for r in rows:
        if classify(r["symbol"], r["description"]) != "equity":
            continue  # bond/cash trades don't touch the equity sleeve
        mag = abs(r["amount"])
        cf = -mag if r["category"] in EQUITY_FLOW_IN else mag
        flows.append((r["run_date"], cf))
    return flows


def money_weighted_return(start: str | None = None, end: str | None = None) -> dict:
    """equity-sleeve money-weighted (XIRR) return between two holdings snapshots."""
    snaps = _snapshot_dates()
    if len(snaps) < 2:
        return {
            "error": "need at least 2 holdings snapshots to compute a return. "
                     "snapshots accumulate automatically once SnapTrade runs daily, "
                     "or import an older Fidelity positions export now.",
            "snapshots_available": snaps,
        }
    start = start or snaps[0]
    end = end or snaps[-1]
    bv, ev = _equity_value(start), _equity_value(end)
    flows = _equity_flows(start, end)
    # net contribution INTO equity = -(sum of signed investor cashflows)
    net_into_equity = -sum(cf for _, cf in flows)
    investment_gain = ev - bv - net_into_equity  # market appreciation + dividends

    cashflows = [(start, -bv)] + flows + [(end, ev)]
    rate = xirr(cashflows)

    return {
        "scope": "equity",
        "start_date": start,
        "end_date": end,
        "beginning_value": round(bv),
        "ending_value": round(ev),
        "net_invested_into_equity": round(net_into_equity),
        "investment_gain": round(investment_gain),
        "money_weighted_return_annualized": round(rate * 100, 2) if rate is not None else None,
        "num_flows": len(flows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="money-weighted (XIRR) portfolio return")
    parser.add_argument("--start", help="start date YYYY-MM-DD (default: earliest snapshot)")
    parser.add_argument("--end", help="end date YYYY-MM-DD (default: latest snapshot)")
    args = parser.parse_args()

    r = money_weighted_return(args.start, args.end)
    if "error" in r:
        print(r["error"])
        print(f"\nsnapshots currently available: {r['snapshots_available']}")
        return
    print(f"\n  EQUITY MONEY-WEIGHTED RETURN   {r['start_date']} → {r['end_date']}")
    print(f"  {'─' * 52}")
    print(f"  beginning equity value   ${r['beginning_value']:>14,}")
    print(f"  net invested into equity ${r['net_invested_into_equity']:>14,}   (buys - sells/divs)")
    print(f"  ending equity value      ${r['ending_value']:>14,}")
    print(f"  investment gain          ${r['investment_gain']:>14,}   (appreciation + dividends)")
    print(f"  money-weighted return     {r['money_weighted_return_annualized']}%   annualized")
    print()


if __name__ == "__main__":
    main()
