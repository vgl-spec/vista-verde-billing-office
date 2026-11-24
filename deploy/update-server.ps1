# Gets the newest version from GitHub. Backs up the database first. Records are never touched by git.

# -Unattended: never wait for Enter (automated tests). Exit code 0 = updated, 1 = nothing changed.
param([switch]$Unattended)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

function Pause-Window([string]$message) {
    if (-not $Unattended) { Read-Host $message | Out-Null }
}
Set-Location -LiteralPath $script:Root

Write-Host ''
Write-Host '  Vista Verde billing: update' -ForegroundColor Cyan

if (Test-ServerRunning) {
    Write-Host '  The billing server is running. Ask everyone to save their work, close the server window,' -ForegroundColor Yellow
    Write-Host '  then run this update again.'
    Pause-Window '  Press Enter to close'
    exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host '  Git is not installed. Install it from https://git-scm.com/download/win and try again.' -ForegroundColor Red
    Pause-Window '  Press Enter to close'
    exit 1
}
$python = Find-Python
if (-not $python) {
    Write-Host '  Python was not found. Double-click SETUP.bat first.' -ForegroundColor Red
    Pause-Window '  Press Enter to close'
    exit 1
}

Write-Host '  1. Backing up the database...'
foreach ($name in @('bms.sqlite3', 'demo.sqlite3')) {
    $database = Join-Path $script:Root "app\data\$name"
    if (Test-Path -LiteralPath $database) { Invoke-Python $python @('tools\backup_db.py', $database, '--keep', '30') }
}

Write-Host '  2. Downloading the newest version...'
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host '  The update could not be applied (files on this computer were changed by hand?).' -ForegroundColor Red
    Write-Host '  Nothing was changed. Send a photo of this window to the developer.'
    Pause-Window '  Press Enter to close'
    exit 1
}

Write-Host '  3. Updating the Excel import library...'
Invoke-Python $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', 'requirements-lan.txt')

Write-Host ''
Write-Host '  Updated. Start the server again with START.bat (it upgrades the database by itself).' -ForegroundColor Green
Pause-Window '  Press Enter to close'
