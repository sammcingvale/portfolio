#!/bin/zsh
# daily portfolio refresh — pulls current holdings (SnapTrade) then prices (yfinance).
#
# run ORDER matters: holdings first, so any ticker you bought today lands in today's
# snapshot before yf_prices picks the ticker list to fetch.
#
# scheduled by ~/Library/LaunchAgents/com.sam.portfolio.daily.plist (weekdays 16:00
# local / 18:00 ET, after the close settles). if the Mac is asleep/off at 16:00,
# launchd coalesces the missed run(s) into ONE run shortly after the next wake:
#   - prices backfill the whole window (yf_prices pulls ~2y every run)
#   - holdings capture that day only (SnapTrade has no historical-positions API)
#
# both scripts always run even if one fails; exit non-zero if either did.

set -uo pipefail

REPO="/Users/sam/Gits/portfolio"
PY="$REPO/.venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/daily_refresh.log"

mkdir -p "$LOG_DIR"
cd "$REPO" || exit 1

log() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG"; }

# hard wall-clock cap (macOS has no GNU `timeout`): run "$@" and SIGTERM it after
# $1 seconds, SIGKILL if it ignores that — a wedged run can never block the
# schedule. the script-level SIGALRM timeouts are the first line of defense; this
# is the backstop for anything that hangs outside a wrapped API call (e.g. yfinance).
run_capped() {
    local secs=$1; shift
    "$@" &
    local pid=$!
    ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null; sleep 5; kill -KILL "$pid" 2>/dev/null ) &
    local wd=$!
    wait "$pid"; local rc=$?
    kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
    return $rc
}

log "=== daily refresh start ==="

log "-> holdings (snaptrade_holdings.py)"
run_capped 1200 "$PY" ingest/snaptrade_holdings.py >> "$LOG" 2>&1
h=$?
log "holdings exit=$h"

log "-> prices (yf_prices.py)"
run_capped 900 "$PY" ingest/yf_prices.py >> "$LOG" 2>&1
p=$?
log "prices exit=$p"

log "=== daily refresh done (holdings=$h prices=$p) ==="
[[ $h -eq 0 && $p -eq 0 ]]
