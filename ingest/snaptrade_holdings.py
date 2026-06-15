"""
pull live holdings from Fidelity (and any other linked brokerage) via SnapTrade,
and append a fresh dated snapshot to holdings_snapshots. this is the "automated
daily" path — once your SnapTrade account is connected, schedule this to run each
morning alongside yf_prices.py.

ONE-TIME SETUP (see README for the full walkthrough):
  1. create a SnapTrade developer account -> get a clientId + consumerKey
  2. put them in .env  (copy from .env.example)
  3. register yourself as a SnapTrade user and connect Fidelity via the portal link
     this script prints (run:  python ingest/snaptrade_holdings.py --connect)
  4. save the returned userId + userSecret into .env
  5. from then on:  python ingest/snaptrade_holdings.py   appends today's snapshot

this file is intentionally written to fail loudly with guidance until those env
vars exist, so nothing here blocks the CSV-based workflow.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_db import get_connection, init_db  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _client():
    """build a SnapTrade SDK client from env, with a friendly error if not set up."""
    client_id = os.getenv("SNAPTRADE_CLIENT_ID")
    consumer_key = os.getenv("SNAPTRADE_CONSUMER_KEY")
    if not client_id or not consumer_key:
        raise SystemExit(
            "SnapTrade is not configured yet.\n"
            "  1. copy .env.example -> .env\n"
            "  2. fill in SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY\n"
            "see the README 'automated daily via SnapTrade' section for details.\n"
            "(meanwhile, the CSV path works today: ingest/csv_bootstrap.py)"
        )
    try:
        from snaptrade_client import SnapTrade
    except ImportError:
        raise SystemExit("pip install snaptrade-python-sdk  (see requirements.txt)")
    return SnapTrade(client_id=client_id, consumer_key=consumer_key)


def register_and_connect() -> None:
    """one-time: register a SnapTrade user and print the Fidelity connection portal URL."""
    client = _client()
    user_id = os.getenv("SNAPTRADE_USER_ID") or "portfolio-owner"
    reg = client.authentication.register_snap_trade_user(user_id=user_id)
    user_secret = reg.body["userSecret"]
    login = client.authentication.login_snap_trade_user(
        user_id=user_id, user_secret=user_secret
    )
    body = login.body
    portal_url = body.get("redirectURI") if hasattr(body, "get") else body
    print("save these two lines into your .env, then re-run without --connect:\n")
    print(f"SNAPTRADE_USER_ID={user_id}")
    print(f"SNAPTRADE_USER_SECRET={user_secret}\n")
    print("open this URL to authorize Fidelity (read-only):\n")
    print(portal_url)


def _account_type(raw: str | None) -> str:
    n = (raw or "").lower()
    if "roth" in n:
        return "roth_ira"
    if "ira" in n:
        return "traditional_ira"
    if "managed" in n or "sma" in n:
        return "sma"
    return "taxable"


def _extract_symbol(container) -> tuple[str | None, str | None]:
    """walk SnapTrade's nested symbol objects to find (ticker_string, description).

    shapes vary by SDK/endpoint: position['symbol']['symbol']['symbol'] (universal
    symbol) or position['instrument']['symbol']. descend until we hit a string."""
    node, desc = container, None
    for _ in range(4):
        if not hasattr(node, "get"):
            break
        if not desc:
            desc = node.get("description")
        s = node.get("symbol")
        if isinstance(s, str):
            return s, desc
        node = s
    return None, desc


def _parse_position(p) -> tuple:
    """pull (ticker, description, units, price, market_value, cost_basis) from a
    SnapTrade position, tolerant of field-name differences across versions."""
    container = p.get("symbol") or p.get("instrument") or {}
    ticker, desc = _extract_symbol(container)
    ticker = (ticker or "CASH").upper()
    units = p.get("units") or p.get("quantity")
    price = p.get("price") or p.get("market_price")
    market_value = p.get("market_value")
    if market_value is None and units and price:
        market_value = units * price
    avg = p.get("average_purchase_price")
    cost_basis = avg * units if avg and units else None
    return ticker, desc, units, price, market_value, cost_basis


def _account_positions(client, creds: dict, account_id: str):
    """fetch positions for an account, returning a plain list across SDK shapes."""
    resp = client.account_information.get_user_account_positions(
        account_id=account_id, **creds
    )
    body = resp.body
    if hasattr(body, "get") and "results" in body:
        return body["results"]
    return list(body)


def sync(snapshot_date: str) -> int:
    """pull every linked account's positions and write a dated snapshot."""
    client = _client()
    user_id = os.getenv("SNAPTRADE_USER_ID")
    user_secret = os.getenv("SNAPTRADE_USER_SECRET")
    if not user_id or not user_secret:
        raise SystemExit("run once with --connect to create + link your SnapTrade user.")

    creds = {"user_id": user_id, "user_secret": user_secret}
    accounts = client.account_information.list_user_accounts(**creds).body

    conn = get_connection()
    written = 0
    try:
        for acct in accounts:
            account_id = acct["id"]
            name = acct.get("name") or acct.get("number") or account_id
            is_sma = 1 if _account_type(name) == "sma" else 0
            conn.execute(
                """INSERT INTO accounts (account_id, name, type, is_sma)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       name=excluded.name, type=excluded.type, is_sma=excluded.is_sma""",
                (account_id, name, _account_type(name), is_sma),
            )

            for p in _account_positions(client, creds, account_id):
                ticker, desc, units, price, mkt, cost = _parse_position(p)
                conn.execute(
                    """INSERT INTO holdings_snapshots
                           (snapshot_date, account_id, ticker, description,
                            shares, cost_basis, market_value, price, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'snaptrade')
                       ON CONFLICT(snapshot_date, account_id, ticker) DO UPDATE SET
                           shares=excluded.shares, cost_basis=excluded.cost_basis,
                           market_value=excluded.market_value, price=excluded.price""",
                    (snapshot_date, account_id, ticker, desc, units, cost, mkt, price),
                )
                written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="sync Fidelity holdings via SnapTrade")
    parser.add_argument("--connect", action="store_true",
                        help="one-time: register user + print Fidelity connection URL")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="snapshot date (default: today)")
    args = parser.parse_args()

    init_db()
    if args.connect:
        register_and_connect()
        return
    n = sync(args.date)
    print(f"synced {n} positions for snapshot {args.date}")


if __name__ == "__main__":
    main()
