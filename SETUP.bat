@echo off
rem One-time setup of the office server: Python check, firewall, shortcuts, auto-start. Asks for administrator once.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\install-server.ps1"
