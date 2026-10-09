#!/usr/bin/env bash
# run_brazil.sh — APS Brazil line-up import + facts refresh + git push, invoked by cron
# (08:00, 11:00 and 17:30 CT weekdays; the APS PDF lands around 16:30-16:45 CT).
set -uo pipefail

APP_DIR="/opt/vessel-lineup-dashboard"
PYTHON="$APP_DIR/.venv/bin/python"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/brazil_import_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "$APP_DIR missing" >&2; exit 1; }

# One run at a time, and never alongside the Southport poller's facts.db write + git push.
exec 9>"$LOG_DIR/.brazil_import.lock"
if ! flock -n 9; then
    echo "$(date -Is) brazil import already running - skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== brazil import start $(date -Is) ==="
    "$PYTHON" brazil_lineup_import.py
    rc=$?
    echo "=== brazil import finished $(date -Is) rc=$rc ==="

    if [ $rc -eq 0 ]; then
        (
            flock 8
            "$PYTHON" refresh_facts.py --brazil || echo "WARNING: facts refresh failed (DB push continues)"
            git add brazil_lineup.db facts.db
            if git diff --cached --quiet; then
                echo "No DB changes to commit."
            else
                git commit -m "Brazil lineup + facts: $(date +%Y-%m-%d)" \
                    --author "Droplet Cron <275148418+koltenpostin93-blip@users.noreply.github.com>"
                for attempt in 1 2; do
                    git push && break
                    [ $attempt -eq 1 ] && git pull --rebase
                done
            fi
        ) 8>"$LOG_DIR/.facts.lock"
    fi
} >>"$LOG" 2>&1

find "$LOG_DIR" -name 'brazil_import_*.log' -mtime +30 -delete 2>/dev/null || true
exit "$rc"
