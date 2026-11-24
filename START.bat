@echo off
rem Starts the billing server for the whole office network. Keep its window open.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\start-server.ps1"
