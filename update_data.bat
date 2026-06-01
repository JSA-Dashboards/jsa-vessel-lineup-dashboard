@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: update_data.bat  —  Copy latest Excel file into repo and push to GitHub
:: Double-click this file whenever you save new data to the spreadsheet.
:: ─────────────────────────────────────────────────────────────────────────────

set SOURCE="C:\Users\KoltenPostin\John Stewart and Associates\JSA - Documents\Research Analyst\Misc\Boat Lineup\Vessel Lineup - US.xlsx"
set DEST=%~dp0"Vessel Lineup - US.xlsx"

echo Copying latest data file...
copy /Y %SOURCE% %DEST%
if errorlevel 1 (
    echo ERROR: Could not copy file. Make sure the source file is not open in Excel.
    pause
    exit /b 1
)

echo Pushing to GitHub...
cd /d %~dp0
git add "Vessel Lineup - US.xlsx"
git commit -m "Update vessel lineup data"
git push

if errorlevel 1 (
    echo ERROR: Git push failed. Check your connection or GitHub credentials.
    pause
    exit /b 1
)

echo.
echo Done! Streamlit Cloud will refresh automatically in ~1 minute.
pause
