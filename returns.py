"""
money-weighted (dollar-weighted / XIRR) return for the portfolio.

a money-weighted return accounts for the SIZE and TIMING of the cash you moved in
and out — it answers "what annualized rate did my actual dollars earn?" computing
it rigorously needs three things:

    1. a BEGINNING portfolio value   (a holdings snapshot at the start date)
    2. the EXTERNAL cash flows between (deposits/withdrawals/transfers — we have these)
    3. an ENDING portfolio value      (the latest holdings snapshot)

we always have (2) and (3). (1) requires a SECOND holdings snapshot — so drop an
older Fidelity positions export (csv_bootstrap.py imports it under its own date)
and this lights up. with daily snapshots going forward it becomes automatic.

run:
    python returns.py                      # uses earliest -> latest snapshot
    python returns.py --start 2025-12-31   # over a specific window

from a notebook:
    from returns import money_weighted_return
    money_weighted_return()
"""

from __future__ import annotations

import argparse

from portfolio_db import get_connection

# external-flow categories: money/shares that crossed the portfolio boundary.
# `amount` sign in transactions is + for money IN, - for money OUT.
EXTERNAL_CATEGORIES = ("deposit", "withdrawal", "transfer_in", "transfer_out")


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


def _portfolio_value(snapshot_date: str) -> float:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(market_value), 0) FROM holdings_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        ).fetchone()[0]
    finally:
        conn.close()


def _external_flows(start: str, end: str) -> list[tuple[str, float]]:
    """external cash flows in (start, end], as [(date, amount)] with +in / -out."""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT run_date, amount FROM transactions
                WHERE category IN ({','.join('?' * len(EXTERNAL_CATEGORIES))})
                  AND amount IS NOT NULL
                  AND run_date > ? AND run_date <= ?
                ORDER BY run_date""",
            (*EXTERNAL_CATEGORIES, start, end),
        ).fetchall()
    finally:
        conn.close()
    return [(r["run_date"], r["amount"]) for r in rows]


def money_weighted_return(start: str | None = None, end: str | None = None) -> dict:
    snaps = _snapshot_dates()
    if len(snaps) < 2:
        return {
            "error": "need at least 2 holdings snapshots to compute a return. "
                     "drop an older Fidelity positions export and import it:\n"
                     "  python ingest/csv_bootstrap.py data/<older_positions>.csv",
            "snapshots_available": snaps,
        }
    start = start or snaps[0]
    end = end or snaps[-1]
    bv, ev = _portfolio_value(start), _portfolio_value(end)
    flows = _external_flows(start, end)
    net_flows = sum(a for _, a in flows)
    investment_gain = ev - bv - net_flows  # what the markets (not your deposits) produced

    # build the XIRR cashflow stream from the investor's perspective
    cashflows = [(start, -bv)] + [(d, -a) for d, a in flows] + [(end, ev)]
    rate = xirr(cashflows)

    return {
        "start_date": start,
        "end_date": end,
        "beginning_value": round(bv),
        "ending_value": round(ev),
        "net_external_flows": round(net_flows),
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
    print(f"\n  MONEY-WEIGHTED RETURN   {r['start_date']} → {r['end_date']}")
    print(f"  {'─' * 50}")
    print(f"  beginning value        ${r['beginning_value']:>14,}")
    print(f"  net additions/withdrawals ${r['net_external_flows']:>11,}")
    print(f"  ending value           ${r['ending_value']:>14,}")
    print(f"  investment gain        ${r['investment_gain']:>14,}   (markets, not deposits)")
    print(f"  money-weighted return   {r['money_weighted_return_annualized']}%   annualized")
    print()


if __name__ == "__main__":
    main()
