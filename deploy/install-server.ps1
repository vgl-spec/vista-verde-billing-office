# One-time setup of the office server computer. Started by SETUP.bat; asks Windows for administrator rights itself.
# Safe to run again: every step checks what is already done.

# -Unattended: answer every question with the recommended choice and never wait for Enter (automated tests).
param([switch]$Unattended)

$ErrorActionPreference = 'Stop'

# Opening the firewall and creating the start-up task need administrator rights: ask Windows once (UAC).
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Unattended) { $arguments += '-Unattended' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
    exit
}

. (Join-Path $PSScriptRoot 'common.ps1')
Set-Location -LiteralPath $script:Root

function Ask([string]$question) {
    if ($Unattended) { Write-Host "$question [Y/N] Y (unattended)"; return $true }
    $answer = Read-Host "$question [Y/N]"
    return $answer -match '^(y|yes)$'
}

function Pause-Window([string]$message) {
    if (-not $Unattended) { Read-Host $message | Out-Null }
}

Write-Host ''
Write-Host '  Vista Verde billing: set up this computer as the office server' -ForegroundColor Cyan
Write-Host ''

# 1. Python
Write-Host '  Step 1 of 5: Python' -ForegroundColor Cyan
$python = Find-Python
if (-not $python) {
    Write-Host '  Python 3.11 or newer is not installed.' -ForegroundColor Yellow
    if (-not $Unattended -and (Get-Command winget -ErrorAction SilentlyContinue) -and (Ask '  Install Python 3.12 now (needs internet, about 30 MB)?')) {
        winget install --exact --id Python.Python.3.12 --scope machine --accept-package-agreements
        Write-Host '  Python installed. Close this window and double-click SETUP.bat again so Windows sees it.' -ForegroundColor Green
    } else {
        Write-Host '  Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
        Write-Host '  then double-click SETUP.bat again.'
    }
    Pause-Window '  Press Enter to close'
    exit 1
}
Write-Host "  Found: $((Invoke-Python $python @('--version')) 2>&1)" -ForegroundColor Green

# 2. Excel import library (only Import Excel needs it)
Write-Host '  Step 2 of 5: Excel import support' -ForegroundColor Cyan
Invoke-Python $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', 'requirements-lan.txt')
if ($LASTEXITCODE -ne 0) { Write-Host '  Could not install it (no internet?). Everything except Import Excel still works.' -ForegroundColor Yellow }
else { Write-Host '  Done.' -ForegroundColor Green }

# 3. Network: private profile + firewall opening for the billing port only.
# Nothing here touches the workgroup, addresses, adapters, file sharing or printers. Every change is written to
# app\data\setup\changes.json so UNDO SETUP.bat can reverse exactly it.
Write-Host '  Step 3 of 5: let the other office computers in' -ForegroundColor Cyan
$changes = Read-SetupChanges
if (-not $changes.firewall_backup) {
    # The whole firewall as it is before the first setup: a last-resort way back (see UNDO SETUP.bat).
    New-Item -ItemType Directory -Force -Path $script:SetupDir | Out-Null
    $firewallBackup = Join-Path $script:SetupDir 'firewall-before-setup.wfw'
    netsh advfirewall export "$firewallBackup" | Out-Null
    if ($LASTEXITCODE -eq 0) { $changes.firewall_backup = $firewallBackup; Save-SetupChanges $changes }
    else { Write-Host '  Could not save a copy of the firewall settings first (continuing).' -ForegroundColor Yellow }
}
try {
    $public = @(Invoke-Cim { Get-NetConnectionProfile -ErrorAction Stop } | Where-Object { $_.NetworkCategory -eq 'Public' })
    if (-not $public.Count) { Write-Host '  Network is already Private: nothing to change.' -ForegroundColor Green }
    foreach ($connection in $public) {
        Write-Host "  The network '$($connection.Name)' is set to Public, which hides this computer from the others." -ForegroundColor Yellow
        if (Ask '  Is this the office (or home) network you trust? Set it to Private?') {
            Invoke-Cim { Set-NetConnectionProfile -InterfaceIndex $connection.InterfaceIndex -NetworkCategory Private -ErrorAction Stop }
            $changes.networks_set_private = @($changes.networks_set_private | Where-Object { $_ -ne $connection.Name }) + $connection.Name
            Save-SetupChanges $changes
            Write-Host '  Set to Private.' -ForegroundColor Green
        }
    }
} catch {
    Write-Host "  Could not check the network type ($($_.Exception.Message))." -ForegroundColor Yellow
    Write-Host '  In Settings > Network & internet, make sure this network is set to Private, then run SETUP.bat again.' -ForegroundColor Yellow
}
# netsh, not New-NetFirewallRule: it works even when Windows' management service is unavailable.
netsh advfirewall firewall delete rule name="$($script:FirewallRule)" | Out-Null
netsh advfirewall firewall add rule name="$($script:FirewallRule)" dir=in action=allow protocol=TCP `
    localport=$($script:Port) profile=private,domain | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not open the firewall for port $($script:Port) (netsh exit $LASTEXITCODE)." }
# The server is python.exe. Without a rule for the program itself, Windows asks "allow Python?" the first time it
# starts; pressing Cancel there adds a BLOCK rule, and a block rule beats the port opening above (Windows 10 and 11).
$pythonExe = "$(Invoke-Python $python @('-c', 'import sys; print(sys.executable)'))".Trim()
$pythonRule = "$($script:FirewallRule) (Python)"
netsh advfirewall firewall delete rule name="$pythonRule" | Out-Null
netsh advfirewall firewall add rule name="$pythonRule" dir=in action=allow program="$pythonExe" profile=private,domain | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host '  Could not allow Python itself. If Windows asks "allow Python?", tick Private and press Allow.' -ForegroundColor Yellow }
$blocking = @(Get-PythonBlockRules $pythonExe)
if ($blocking.Count) {
    Write-Host '  Windows has a rule BLOCKING Python on this network (someone pressed Cancel on the "allow Python?"' -ForegroundColor Yellow
    Write-Host '  question). While it is on, the laptops cannot reach the billing server.' -ForegroundColor Yellow
    if (Ask '  Switch that block off for private networks?') {
        foreach ($rule in $blocking) {
            $rule.Enabled = $false
            $changes.python_blocks_disabled = @($changes.python_blocks_disabled) + [pscustomobject]@{ name = $rule.Name; program = $rule.ApplicationName }
        }
        Save-SetupChanges $changes
        Write-Host '  Switched off.' -ForegroundColor Green
    }
}
Write-Host "  Firewall opened for port $($script:Port) on private networks only (never on public Wi-Fi)." -ForegroundColor Green

# 4. Desktop shortcuts
Write-Host '  Step 4 of 5: desktop shortcuts' -ForegroundColor Cyan
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $desktop 'Start Billing Server.lnk'))
$link.TargetPath = Join-Path $script:Root 'START.bat'
$link.WorkingDirectory = $script:Root
$link.IconLocation = 'shell32.dll,13'
$link.Save()
Set-Content -LiteralPath (Join-Path $desktop 'Open Billing.url') -Encoding ASCII -Value @(
    '[InternetShortcut]', "URL=http://127.0.0.1:$($script:Port)/")
Write-Host '  "Start Billing Server" and "Open Billing" are on the desktop.' -ForegroundColor Green

# 5. Start automatically when this computer's user signs in
Write-Host '  Step 5 of 5: start automatically' -ForegroundColor Cyan
$taskName = $script:TaskName
if (Ask '  Start the billing server automatically when this computer signs in?') {
    $startScript = Join-Path $PSScriptRoot 'start-server.ps1'
    # schtasks, not Register-ScheduledTask: no dependency on Windows' management service. /IT = runs only while
    # this user is signed in (no stored password), in their own session so the server window is visible.
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$startScript`""
    schtasks.exe /Create /TN $taskName /SC ONLOGON /RU "$env:USERDOMAIN\$env:USERNAME" /IT /TR $command /F | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host '  It will start by itself after sign-in (minimised window on the taskbar).' -ForegroundColor Green }
    else { Write-Host '  Could not set the automatic start. Double-click "Start Billing Server" each morning instead.' -ForegroundColor Yellow }
} else {
    Write-Host '  Skipped. Double-click "Start Billing Server" each morning instead.'
}

# Addresses for the other computers, also saved to a text file.
$lines = @("Vista Verde billing: addresses for the other office computers (open in Chrome or Edge)", '')
foreach ($item in @(Get-LanAddresses)) { $lines += "  http://$($item.Address):$($script:Port)    ($($item.Adapter))" }
$lines += "  http://$($env:COMPUTERNAME):$($script:Port)    (computer name)"
$lines += ''
$lines += 'If the number-address changes after a router restart, reserve it in the router (DHCP reservation)'
$lines += 'or use the computer-name address.'
$addressFile = Join-Path $script:Root 'ADDRESS FOR OTHER COMPUTERS.txt'
Set-Content -LiteralPath $addressFile -Value $lines -Encoding UTF8

Write-Host ''
Write-Host '  Setup finished.' -ForegroundColor Green
Write-Host "  Addresses for the laptops are saved in: $addressFile"
Write-Host '  To take back everything this setup changed on this computer: UNDO SETUP.bat (records are kept).'
if (Test-ServerRunning) {
    Write-Host '  The billing server is already running.' -ForegroundColor Green
} else {
    # Started through Explorer so the server runs as the normal user, not with this window's administrator rights.
    Write-Host '  Starting the billing server in its own window...' -ForegroundColor Green
    Start-Process explorer.exe -ArgumentList "`"$(Join-Path $script:Root 'START.bat')`""
}
Pause-Window '  Press Enter to close this setup window'
