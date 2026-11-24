@echo off
rem Loads a records file from the developer (USB drive) into this server. Refuses if the server already has homeowners.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\load-records.ps1"
