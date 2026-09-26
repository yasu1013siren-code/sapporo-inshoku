@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo Sapporo Inshoku Ver.7.2 - Auto Publish
echo ============================================
echo.

if not exist "collector_ver72.py" (
    echo ERROR: collector_ver72.py not found.
    pause
    exit /b 1
)

if not exist "index.html" (
    echo ERROR: index.html not found.
    pause
    exit /b 1
)

if not exist ".git" (
    echo ERROR: Git repository not found.
    pause
    exit /b 1
)

where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py"
    goto PYTHON_OK
)

where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python"
    goto PYTHON_OK
)

echo ERROR: Python not found.
pause
exit /b 1

:PYTHON_OK

echo [1/6] Checking GitHub...
git fetch origin

if errorlevel 1 (
    echo ERROR: git fetch failed.
    pause
    exit /b 1
)

echo [2/6] Synchronizing with GitHub...
git pull --ff-only origin main

if errorlevel 1 (
    echo.
    echo ERROR: GitHub and this PC have different commits.
    echo Auto publish has been stopped to protect your data.
    echo.
    pause
    exit /b 1
)

echo [3/6] Collecting Ver.7.2 data...
%PYTHON% "collector_ver72.py"

if errorlevel 1 (
    echo ERROR: collector failed.
    pause
    exit /b 1
)

if not exist "news.json" (
    echo ERROR: news.json was not created.
    pause
    exit /b 1
)

echo [4/6] Checking changes...
git add index.html news.json

git diff --cached --quiet
if not errorlevel 1 (
    echo.
    echo ============================================
    echo NO CHANGE - Nothing to publish.
    echo ============================================
    pause
    exit /b 0
)

echo [5/6] Committing...
git commit -m "Ver.7.2 auto update"

if errorlevel 1 (
    echo ERROR: git commit failed.
    pause
    exit /b 1
)

echo [6/6] Pushing to GitHub...
git push origin main

if errorlevel 1 (
    echo.
    echo ERROR: git push failed.
    echo Your local commit has NOT been deleted.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo PUBLISH COMPLETE
echo ============================================
echo.
echo https://yasu1013siren-code.github.io/sapporo-inshoku/
echo.

pause
exit /b 0