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
    reg = client.authentication.register_snap_trade_user(body={"userId": user_id})
    user_secret = reg.body["userSecret"]
    login = client.authentication.login_snap_trade_user(
        query_params={"userId": user_id, "userSecret": user_secret}
    )
    print("save these two lines into your .env, then re-run without --connect:\n")
    print(f"SNAPTRADE_USER_ID={user_id}")
    print(f"SNAPTRADE_USER_SECRET={user_secret}\n")
    print("open this URL to authorize Fidelity (read-only):\n")
    print(login.body["redirectURI"])


def _account_type(raw: str | None) -> str:
    n = (raw or "").lower()
    if "roth" in n:
        return "roth_ira"
    if "ira" in n:
        return "traditional_ira"
    if "managed" in n or "sma" in n:
        return "sma"
    return "taxable"


def sync(snapshot_date: str) -> int:
    """pull every linked account's positions and write today's snapshot."""
    client = _client()
    user_id = os.getenv("SNAPTRADE_USER_ID")
    user_secret = os.getenv("SNAPTRADE_USER_SECRET")
    if not user_id or not user_secret:
        raise SystemExit("run once with --connect to create + link your SnapTrade user.")

    creds = {"user_id": user_id, "user_secret": user_secret}
    accounts = client.account_information.list_user_accounts(query_params=creds).body

    conn = get_connection()
    written = 0
    try:
        for acct in accounts:
            account_id = acct["id"]
            name = acct.get("name") or acct.get("number")
            is_sma = 1 if _account_type(name) == "sma" else 0
            conn.execute(
                """INSERT INTO accounts (account_id, name, type, is_sma)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       name=excluded.name, type=excluded.type, is_sma=excluded.is_sma""",
                (account_id, name, _account_type(name), is_sma),
            )

            positions = client.account_information.get_user_account_positions(
                query_params={**creds, "account_id": account_id}
            ).body
            for p in positions:
                sym = (p.get("symbol", {}) or {}).get("symbol", {}) or {}
                ticker = (sym.get("symbol") or "CASH").upper()
                units = p.get("units")
                price = p.get("price")
                mkt = (units or 0) * (price or 0) if units and price else p.get("market_value")
                conn.execute(
                    """INSERT INTO holdings_snapshots
                           (snapshot_date, account_id, ticker, description,
                            shares, cost_basis, market_value, price, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'snaptrade')
                       ON CONFLICT(snapshot_date, account_id, ticker) DO UPDATE SET
                           shares=excluded.shares, cost_basis=excluded.cost_basis,
                           market_value=excluded.market_value, price=excluded.price""",
                    (
                        snapshot_date, account_id, ticker,
                        sym.get("description"), units,
                        p.get("average_purchase_price") and units and
                        p["average_purchase_price"] * units,
                        mkt, price,
                    ),
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
