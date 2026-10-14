@echo off
title Karaoke Backend - Windows Setup
echo ======================================================================
echo          KARAOKE ALL-IN-ONE BACKEND SETUP (WINDOWS 10)
echo ======================================================================
echo.
echo Checking WSL2 installation...
wsl --status >nul 2>&1
if errorlevel 1 (
    echo [ERROR] WSL is not enabled on this machine.
    echo Please open PowerShell as Administrator and run:
    echo     wsl --install --no-distribution
    echo and restart your computer.
    pause
    exit /b 1
)

set WSL_DIR=C:\WSL\KaraokeBackend
if not exist "%WSL_DIR%" mkdir "%WSL_DIR%"

echo Importing Karaoke Backend appliance into WSL2...
echo Target Directory: %WSL_DIR%
echo This may take 1-2 minutes...
echo.

wsl --import KaraokeBackend "%WSL_DIR%" karaoke-backend-wsl2.tar.gz --version 2

if errorlevel 1 (
    echo [ERROR] Failed to import WSL image.
    pause
    exit /b 1
)

echo.
echo ======================================================================
echo [SUCCESS] Karaoke Backend imported successfully!
echo.
echo Starting backend services now...
start "" wsl -d KaraokeBackend -u root /entrypoint.sh
timeout /t 5 >nul

echo.
echo ======================================================================
echo   BACKEND SERVICES ARE NOW LIVE ON WINDOWS:
echo ======================================================================
echo   - Karaoke REST API:      http://localhost:8000
echo   - Interactive API Docs:  http://localhost:8000/docs
echo   - Similarity Graph UI:   http://localhost:8888/graph
echo   - AI DJ MCP Server:      http://localhost:8888/sse
echo   - PostgreSQL (Pre-seeded): localhost:5432 (user: karaoke, db: karaoke)
echo   - OpenSearch (Vectors):    http://localhost:9200
echo ======================================================================
echo.
echo You can now develop your frontend on Windows pointing to http://localhost:8000!
echo To start the backend in the future, run start_backend.bat.
echo.
pause
