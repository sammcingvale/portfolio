"""
import a Fidelity "Portfolio Positions" CSV export into holdings_snapshots.

this is the bootstrap path: it gets real data into the database TODAY, before the
automated SnapTrade feed is wired up. it's also handy for back-loading history if
you have older exports lying around.

usage:
    python ingest/csv_bootstrap.py data/Portfolio_Positions_Jun-14-2025.csv
    python ingest/csv_bootstrap.py data/export.csv --date 2025-06-14

the snapshot date is taken (in order of preference) from --date, then the filename
(Fidelity names exports like "Portfolio_Positions_Jun-14-2025.csv"), then today.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_db import get_connection, init_db  # noqa: E402

# Fidelity column name -> our field. we match leniently (case/space-insensitive).
COLUMN_ALIASES = {
    "account_number": ["account number", "account"],
    "account_name": ["account name"],
    "ticker": ["symbol"],
    "description": ["description"],
    "shares": ["quantity"],
    "market_value": ["current value"],
    "price": ["last price"],
    "cost_basis": ["cost basis total", "cost basis"],
    "type": ["type"],
}

# rows whose symbol matches these aren't real positions
SKIP_SYMBOL_PATTERNS = re.compile(r"pending activity", re.IGNORECASE)


def _norm(col: str) -> str:
    return re.sub(r"\s+", " ", str(col).strip().lower())


def _resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    """map our field names to the actual columns present in this export."""
    available = {_norm(c): c for c in df.columns}
    resolved: dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in available:
                resolved[field] = available[alias]
                break
    return resolved


def _money(value) -> float | None:
    """parse Fidelity money/number strings: '$1,234.56', '($50.00)', '--', 'n/a'."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "--", "n/a", "N/A", "nan"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "-"):
        return None
    try:
        num = float(s)
    except ValueError:
        return None
    return -num if negative else num


def _clean_symbol(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().rstrip("*").strip()  # Fidelity marks core/cash like 'SPAXX**'
    if not s or s.lower() == "nan":
        return None
    if SKIP_SYMBOL_PATTERNS.search(s):
        return None
    return s.upper()


def _infer_date(csv_path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    m = re.search(r"([A-Z][a-z]{2}-\d{1,2}-\d{4})", csv_path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%b-%d-%Y").date().isoformat()
        except ValueError:
            pass
    return date.today().isoformat()


def _guess_account_type(name: str) -> str:
    n = (name or "").lower()
    if "roth" in n:
        return "roth_ira"
    if "ira" in n or "retirement" in n:
        return "traditional_ira"
    if "401" in n:
        return "401k"
    return "taxable"


def import_csv(csv_path: Path, snapshot_date: str) -> int:
    # Fidelity appends a trailing comma to every data row (one more field than the
    # header) AND appends disclaimer text after a blank line. index_col=False stops
    # pandas from treating that extra leading field as a row index (which would shift
    # every column left by one); on_bad_lines='skip' drops the trailing disclaimer.
    df = pd.read_csv(csv_path, dtype=str, skip_blank_lines=True,
                     on_bad_lines="skip", index_col=False)
    df = df.dropna(how="all")
    cols = _resolve_columns(df)

    missing = [f for f in ("account_number", "ticker", "shares") if f not in cols]
    if missing:
        raise SystemExit(
            f"could not find expected columns {missing} in {csv_path.name}.\n"
            f"columns present: {list(df.columns)}"
        )

    conn = get_connection()
    accounts_seen: set[str] = set()
    rows_written = 0
    try:
        for _, row in df.iterrows():
            ticker = _clean_symbol(row.get(cols["ticker"]))
            if ticker is None:
                continue
            account_id = str(row.get(cols["account_number"], "")).strip()
            if not account_id or account_id.lower() == "nan":
                continue
            account_name = str(row.get(cols.get("account_name", ""), "")).strip()

            if account_id not in accounts_seen:
                conn.execute(
                    """INSERT INTO accounts (account_id, name, type)
                       VALUES (?, ?, ?)
                       ON CONFLICT(account_id) DO UPDATE SET
                           name = COALESCE(excluded.name, accounts.name)""",
                    (account_id, account_name or None, _guess_account_type(account_name)),
                )
                accounts_seen.add(account_id)

            conn.execute(
                """INSERT INTO holdings_snapshots
                       (snapshot_date, account_id, ticker, description,
                        shares, cost_basis, market_value, price, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fidelity_csv')
                   ON CONFLICT(snapshot_date, account_id, ticker) DO UPDATE SET
                       description=excluded.description, shares=excluded.shares,
                       cost_basis=excluded.cost_basis, market_value=excluded.market_value,
                       price=excluded.price, source=excluded.source""",
                (
                    snapshot_date,
                    account_id,
                    ticker,
                    str(row.get(cols.get("description", ""), "")).strip() or None,
                    _money(row.get(cols["shares"])),
                    _money(row.get(cols.get("cost_basis", ""))),
                    _money(row.get(cols.get("market_value", ""))),
                    _money(row.get(cols.get("price", ""))),
                ),
            )
            rows_written += 1
        conn.commit()
    finally:
        conn.close()
    return rows_written


def main() -> None:
    parser = argparse.ArgumentParser(description="import a Fidelity positions CSV")
    parser.add_argument("csv_path", type=Path, help="path to the Fidelity export")
    parser.add_argument("--date", help="snapshot date YYYY-MM-DD (default: from filename/today)")
    args = parser.parse_args()

    if not args.csv_path.exists():
        raise SystemExit(f"file not found: {args.csv_path}")

    init_db()
    snapshot_date = _infer_date(args.csv_path, args.date)
    n = import_csv(args.csv_path, snapshot_date)
    print(f"imported {n} positions for snapshot {snapshot_date} from {args.csv_path.name}")
    print("next: run  python ingest/yf_prices.py  to pull price history for these tickers")


if __name__ == "__main__":
    main()
