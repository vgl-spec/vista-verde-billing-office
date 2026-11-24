@echo off
rem Backs up the database, then downloads the newest version from GitHub. Close the server window first.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\update-server.ps1"
