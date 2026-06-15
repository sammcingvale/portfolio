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


PERSONAL_KEY_HELP = (
    "you have a PERSONAL SnapTrade key — its user is auto-provisioned at signup, so\n"
    "registration is not available AND there is no separate userSecret to look up\n"
    "(the dashboard never shows one). the clientId + consumerKey signature plus your\n"
    "auto-provisioned userId is all the API needs. so:\n"
    "  1. set SNAPTRADE_USER_ID in .env to your auto-provisioned user id\n"
    "     (leave SNAPTRADE_USER_SECRET blank — personal keys don't use it)\n"
    "  2. you've already connected Fidelity, so skip --connect and run:\n"
    "       python ingest/snaptrade_holdings.py --list   # verify the connection\n"
    "       python ingest/snaptrade_holdings.py          # write today's snapshot"
)


def register_and_connect() -> None:
    """one-time: register a SnapTrade user and print the Fidelity connection portal URL.
    (developer keys only — personal keys are auto-provisioned, see PERSONAL_KEY_HELP)."""
    client = _client()
    user_id = os.getenv("SNAPTRADE_USER_ID") or "portfolio-owner"
    try:
        reg = client.authentication.register_snap_trade_user(user_id=user_id)
    except Exception as e:  # noqa: BLE001 — surface the personal-key case clearly
        msg = str(e)
        if "1012" in msg or "personal" in msg.lower():
            raise SystemExit(PERSONAL_KEY_HELP)
        raise
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


def _user_creds() -> dict:
    """read the provisioned user identity from .env, or explain how to get it.

    personal keys have no userSecret — the request signature (clientId + consumerKey)
    plus the auto-provisioned userId is sufficient, so an empty secret is expected and
    correct. developer keys supply a real secret saved by --connect."""
    user_id = os.getenv("SNAPTRADE_USER_ID")
    if not user_id:
        raise SystemExit("missing SNAPTRADE_USER_ID in .env.\n\n" + PERSONAL_KEY_HELP)
    return {"user_id": user_id, "user_secret": os.getenv("SNAPTRADE_USER_SECRET") or ""}


def list_accounts() -> None:
    """verify the connection: list linked accounts and their position counts (no DB write)."""
    client = _client()
    creds = _user_creds()
    accounts = client.account_information.list_user_accounts(**creds).body
    if not accounts:
        raise SystemExit("connected, but SnapTrade returned 0 accounts — is Fidelity linked "
                         "and finished its initial sync in the SnapTrade portal?")
    print(f"connected — {len(accounts)} account(s):\n")
    for acct in accounts:
        n = len(_account_positions(client, creds, acct["id"]))
        name = acct.get("name") or acct.get("number") or acct["id"]
        print(f"  {name:36}  type={acct.get('account_type') or acct.get('type','?'):10}  {n} positions")
    print("\nlooks good — run without --list to write today's snapshot.")


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


def _num(v):
    """coerce SnapTrade's stringified numerics to float (v11 returns units/price/
    cost_basis as strings); tolerate None, empty, and already-numeric values."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_position(p) -> tuple:
    """pull (ticker, description, units, price, market_value, cost_basis) from a
    SnapTrade position, tolerant of field-name and type differences across versions.

    v11 shape: symbol + description live under `instrument`; units/price/cost_basis
    arrive as strings; there is no market_value (compute it); and `cost_basis` is the
    PER-UNIT average price, so total cost = cost_basis * units (matches the old
    average_purchase_price semantics)."""
    container = p.get("symbol") or p.get("instrument") or {}
    ticker, desc = _extract_symbol(container)
    ticker = (ticker or "CASH").upper()
    units = _num(p.get("units") or p.get("quantity"))
    price = _num(p.get("price") or p.get("market_price"))
    market_value = _num(p.get("market_value"))
    if market_value is None and units is not None and price is not None:
        market_value = units * price
    per_unit_cost = _num(p.get("cost_basis") or p.get("average_purchase_price"))
    cost_basis = per_unit_cost * units if per_unit_cost is not None and units is not None else None
    return ticker, desc, units, price, market_value, cost_basis


def _account_positions(client, creds: dict, account_id: str):
    """fetch positions for an account, returning a plain list across SDK shapes."""
    resp = client.account_information.get_all_account_positions(
        account_id=account_id, **creds
    )
    body = resp.body
    if hasattr(body, "get") and "results" in body:
        return body["results"]
    return list(body)


def sync(snapshot_date: str) -> int:
    """pull every linked account's positions and write a dated snapshot."""
    client = _client()
    creds = _user_creds()
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
                        help="(developer keys) register user + print Fidelity connection URL")
    parser.add_argument("--list", action="store_true",
                        help="verify the connection: list accounts + position counts, no write")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="snapshot date (default: today)")
    args = parser.parse_args()

    init_db()
    if args.connect:
        register_and_connect()
        return
    if args.list:
        list_accounts()
        return
    n = sync(args.date)
    print(f"synced {n} positions for snapshot {args.date}")


if __name__ == "__main__":
    main()
