"""shared database helpers — every script and notebook reads/writes through here."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# the database lives at the repo root and is gitignored (never committed)
REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "portfolio.db"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """open a connection with sensible defaults and rows accessible by column name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    """create the schema if it doesn't exist yet (safe to run repeatedly)."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"initialized schema in {DB_PATH}")
