# Loads a prepared records file (for example Phase 1D, from the developer, on a USB drive) into this server.
# Started by "LOAD RECORDS.bat". The checks and the copy are in tools\load_records.py; nothing is changed if any
# check fails (server running, damaged file, newer version, server already holding homeowners).

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Set-Location -LiteralPath $script:Root

Write-Host ''
Write-Host '  Vista Verde billing: load records' -ForegroundColor Cyan
Write-Host ''
$python = Find-Python
if (-not $python) {
    Write-Host '  Python was not found. Double-click SETUP.bat first.' -ForegroundColor Red
    Read-Host '  Press Enter to close' | Out-Null
    exit 1
}

# The records file: the one .sqlite3 file copied into this folder, or a path dragged into this window.
$candidates = @(Get-ChildItem -LiteralPath $script:Root -Filter '*.sqlite3' -File)
if ($candidates.Count -eq 1) {
    $records = $candidates[0].FullName
    Write-Host "  Records file: $records"
} else {
    Write-Host '  Copy the records file from the USB drive into this folder, or drag it into this window,'
    $records = (Read-Host '  then press Enter').Trim().Trim('"')
}
if (-not $records) {
    Write-Host '  No file given. Nothing was changed.'
    Read-Host '  Press Enter to close' | Out-Null
    exit 1
}

Invoke-Python $python @('tools\load_records.py', $records)
$loaded = $LASTEXITCODE -eq 0
if ($loaded -and ((Split-Path -Parent $records) -eq $script:Root)) {
    # The records now live in app\data (backed up at every start). A second loose copy of resident data in this
    # folder helps nobody; the USB drive keeps its own copy.
    $answer = Read-Host "  Delete the loose copy $(Split-Path -Leaf $records) from this folder now? [Y/N]"
    if ($answer -match '^(y|yes)$') { Remove-Item -LiteralPath $records; Write-Host '  Deleted.' -ForegroundColor Green }
}
if ($loaded) { Write-Host '  Done. Start the server with START.bat (or Start Billing Server) and sign in.' -ForegroundColor Green }
Read-Host '  Press Enter to close' | Out-Null
