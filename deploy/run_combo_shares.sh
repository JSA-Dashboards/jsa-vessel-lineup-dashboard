#!/usr/bin/env bash
# run_combo_shares.sh - rebuild and vet the combo-boat calibration in facts.db from USDA FGIS vessel inspections, then commit + push.
# Cron (weekdays 18:00 CT; FGIS publishes its week on Thursdays) via cron-alert: see deploy/combo-shares.cron
# Exit code 2 = the health check failed (cron-alert emails it); the refreshed tables are still committed so the dashboard shows the warning.
set -uo pipefail

APP_DIR="/opt/vessel-lineup-dashboard"
PYTHON="$APP_DIR/.venv/bin/python"
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/combo_shares_$(date +%Y%m%d_%H%M%S).log"
cd "$APP_DIR" || exit 1

rc=0
{
    echo "=== combo shares start $(date -Is) ==="
    (
        flock 8                                  # same lock as the pollers: one writer of facts.db + git at a time
        "$PYTHON" refresh_great_lakes.py || echo "great lakes refresh failed (continuing)"   # USDA lake-port loadings, adds a week on Thursdays
        "$PYTHON" refresh_combo_shares.py
        rc=$?
        echo "refresh rc=$rc"
        git add facts.db
        if git diff --cached --quiet; then
            echo "No changes to commit."
        else
            git commit -m "Combo shares: $(date +%Y-%m-%d)" \
                --author "Droplet Cron <275148418+koltenpostin93-blip@users.noreply.github.com>"
            pushed=
            for attempt in 1 2; do
                git push && { pushed=1; break; }
                # facts.db is binary: a rebase conflict can't be merged, and a half-done rebase leaves a detached HEAD
                # where every later push fails. Back out so the next run starts from master.
                [ $attempt -eq 1 ] && { git pull --rebase || git rebase --abort; }
            done
            [ -n "$pushed" ] || echo "PUSH FAILED: committed on the droplet but not on GitHub"
        fi
        exit $rc
    ) 8>"$LOG_DIR/.facts.lock"
    echo "=== combo shares finished $(date -Is) rc=$? ==="
} >>"$LOG" 2>&1
rc=$(grep -o 'refresh rc=[0-9]*' "$LOG" | tail -1 | cut -d= -f2)
grep -q 'PUSH FAILED' "$LOG" && rc=4          # 4 = push failed -> cron-alert emails
find "$LOG_DIR" -name 'combo_shares_*.log' -mtime +30 -delete 2>/dev/null || true
exit "${rc:-1}"
