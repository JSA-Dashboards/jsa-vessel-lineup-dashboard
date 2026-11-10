#!/usr/bin/env bash
# run_weekly_email.sh - the weekly vessel line-up email (see weekly_email.py). Cron: Wednesdays 16:00 CT via cron-alert, deploy/weekly-email.cron.
# Sends once per ISO week (a second run is a no-op). A failure exits non-zero so cron-alert emails it.
set -uo pipefail

APP_DIR="/opt/vessel-lineup-dashboard"
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/weekly_email_$(date +%Y%m%d_%H%M%S).log"
cd "$APP_DIR" || exit 1

[ -f "$APP_DIR/.env.weekly" ] && { set -a; . "$APP_DIR/.env.weekly"; set +a; }      # VESSEL_EMAIL_TO / VESSEL_EMAIL_BCC (the distribution list), kept off GitHub

"$APP_DIR/.venv/bin/python" weekly_email.py --send 2>&1 | grep -v 'No runtime found\|missing ScriptRunContext' >"$LOG"
rc=${PIPESTATUS[0]}
cat "$LOG"
find "$LOG_DIR" -name 'weekly_email_*.log' -mtime +60 -delete 2>/dev/null || true
exit $rc
