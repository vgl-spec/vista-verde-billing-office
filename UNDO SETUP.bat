@echo off
rem Takes back what SETUP.bat changed on this computer (firewall openings, start-up task, shortcuts). Records are kept.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\uninstall-server.ps1"
