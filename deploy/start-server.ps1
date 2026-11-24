# Starts the billing server for the whole office network. Keep the window open while people work.
#   -Demo   practice copy with invented homeowners (app\data\demo.sqlite3); never touches real records.
param([switch]$Demo)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Set-Location -LiteralPath $script:Root
$Host.UI.RawUI.WindowTitle = if ($Demo) { 'Vista Verde billing server (DEMO)' } else { 'Vista Verde billing server' }

Write-Host ''
Write-Host '  Vista Verde HOA Billing' -ForegroundColor Cyan
if ($Demo) { Write-Host '  DEMO copy: invented homeowners, safe to practise on.' -ForegroundColor Yellow }

if (Test-ServerRunning) {
    Write-Host "  The billing server is already running on this computer (port $($script:Port))." -ForegroundColor Yellow
    Write-Host '  Use that window, or close it first if you want to restart.'
    Write-Addresses
    Read-Host '  Press Enter to close this window'
    exit 0
}

$python = Find-Python
if (-not $python) {
    Write-Host '  Python 3.11 or newer was not found. Double-click SETUP.bat first.' -ForegroundColor Red
    Read-Host '  Press Enter to close this window'
    exit 1
}

$dataDir = Join-Path $script:Root 'app\data'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$database = if ($Demo) { Join-Path $dataDir 'demo.sqlite3' } else { Join-Path $dataDir 'bms.sqlite3' }

$env:BMS_HOST = '0.0.0.0'          # listen on the office network, not only this computer
$env:BMS_PORT = [string]$script:Port
$env:BMS_DB = $database

if ($Demo -and -not (Test-Path -LiteralPath $database)) {
    Write-Host '  Making the demo homeowners (first run only)...'
    Invoke-Python $python @('tools\seed_demo.py')
}

# A copy of the database at every start; the newest 30 are kept in app\data\backups. If the office wrote a folder
# path into app\data\backup-copy-folder.txt (e.g. its Google Drive folder), a second copy goes there too.
$copyFile = Join-Path $dataDir 'backup-copy-folder.txt'
$copyFolder = if (Test-Path -LiteralPath $copyFile) { (Get-Content -LiteralPath $copyFile -TotalCount 1).Trim() } else { '' }
$backupArgs = @('tools\backup_db.py', $database, '--keep', '30')
if ($copyFolder) { $backupArgs += @('--copy-to', $copyFolder) }   # PowerShell 5.1 drops empty arguments, so add it only when set
Invoke-Python $python $backupArgs

Write-Addresses
Write-Host '  Keep this window open while the office works. Closing it stops the system for everyone.' -ForegroundColor Yellow
Write-Host '  The very first time, a one-time setup code is printed below: open the address above, go to /setup,'
Write-Host '  type the code and create the administrator account.'
Write-Host ''
Invoke-Python $python @('app\server.py')
Write-Host ''
Write-Host '  The billing server has stopped.' -ForegroundColor Yellow
Read-Host '  Press Enter to close this window'
