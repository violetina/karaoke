@echo off
title Karaoke Backend - Starter
echo Starting Karaoke Backend services in WSL2...
start "" wsl -d KaraokeBackend -u root /entrypoint.sh
timeout /t 3 >nul
echo Backend is running on http://localhost:8000 and http://localhost:8888!
