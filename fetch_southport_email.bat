@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: fetch_southport_email.bat
:: Double-click when you receive a new Southport vessel lineup email.
:: Pulls the Excel attachment from Outlook and pushes it to GitHub automatically.
:: ─────────────────────────────────────────────────────────────────────────────

cd /d %~dp0
python fetch_southport_email.py

if errorlevel 1 (
    echo.
    echo Something went wrong — see message above.
    pause
)
