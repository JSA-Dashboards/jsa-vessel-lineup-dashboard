#!/usr/bin/env bash
# run_data_age_check.sh - heartbeat on the vessel line-up data: is GitHub's copy current and is the droplet publishing it?
# Cron (weekdays 11:15 CT) via cron-alert: see deploy/data-age.cron. Exit 2 = a problem (cron-alert emails it).
set -uo pipefail

APP_DIR="/opt/vessel-lineup-dashboard"
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/data_age_$(date +%Y%m%d_%H%M%S).log"
cd "$APP_DIR" || exit 1

"$APP_DIR/.venv/bin/python" data_age_check.py >"$LOG" 2>&1
rc=$?
cat "$LOG"
find "$LOG_DIR" -name 'data_age_*.log' -mtime +30 -delete 2>/dev/null || true
exit $rc
