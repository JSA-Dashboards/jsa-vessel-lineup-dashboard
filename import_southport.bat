@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: import_southport.bat
:: Save the Southport email attachment to your Downloads folder,
:: then double-click this file.  It converts and pushes automatically.
:: ─────────────────────────────────────────────────────────────────────────────

cd /d %~dp0
python convert_southport.py

if errorlevel 1 (
    echo.
    echo Something went wrong — see message above.
    pause
)
