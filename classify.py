"""shared asset classification — one definition of equity vs bond vs cash, used by
both the scoreboard and the returns engine so they never disagree."""

from __future__ import annotations

import re

# cash / money-market symbols
CASH_SYMBOLS = {"CASH", "SPAXX", "FDRXX", "FZFXX", "FCASH", "FNSXX", "FGXX"}
# an exchange-listed equity/ETF symbol; anything else non-cash is a bond CUSIP / SMA line
EXCHANGE_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def classify(ticker: str | None, description: str | None = None) -> str:
    """bucket a holding or transaction symbol into 'cash' | 'bond' | 'equity'."""
    t = (ticker or "").upper()
    desc = (description or "").upper()
    if not t:
        return "cash"
    if t in CASH_SYMBOLS or "MONEY MARKET" in desc:
        return "cash"
    if not EXCHANGE_TICKER.match(t):
        return "bond"          # CUSIP-identified individual bonds (common in SMAs)
    return "equity"
