@echo off
rem Practice copy with invented homeowners (app\data\demo.sqlite3). Never touches the real records.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\start-server.ps1" -Demo
