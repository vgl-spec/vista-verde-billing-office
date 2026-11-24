# Takes back what SETUP.bat changed on this computer. Started by "UNDO SETUP.bat".
# Uses the record SETUP keeps in app\data\setup\changes.json, so only SETUP's own changes are reversed.
# Never touches the records (app\data), Python, or this folder: those are listed at the end for removing by hand.
#
# -DryRun: only list what would be undone (changes nothing, needs no administrator rights).
# -Unattended: answer every question with Y and never wait for Enter (automated tests).
param([switch]$DryRun, [switch]$Unattended)

$ErrorActionPreference = 'Stop'

if (-not $DryRun) {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
        if ($Unattended) { $arguments += '-Unattended' }
        Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
        exit
    }
}

. (Join-Path $PSScriptRoot 'common.ps1')
Set-Location -LiteralPath $script:Root

function Ask([string]$question) {
    if ($Unattended -or $DryRun) { return $true }
    $answer = Read-Host "$question [Y/N]"
    return $answer -match '^(y|yes)$'
}

function Pause-Window([string]$message) {
    if (-not $Unattended) { Read-Host $message | Out-Null }
}

function Undo([string]$what, [scriptblock]$action) {
    if ($DryRun) { Write-Host "  Would undo: $what"; return }
    & $action
    Write-Host "  Undone: $what" -ForegroundColor Green
}

Write-Host ''
Write-Host '  Vista Verde billing: undo SETUP on this computer' -ForegroundColor Cyan
if ($DryRun) { Write-Host '  (Dry run: nothing is changed.)' -ForegroundColor Yellow }
Write-Host ''
if (-not $DryRun -and (Test-ServerRunning)) {
    Write-Host '  The billing server is running. Close its window first (the laptops lose the system after this).' -ForegroundColor Yellow
    Pause-Window '  Press Enter to close'
    exit 1
}
if (-not $DryRun -and -not (Ask '  Take back the firewall openings, start-up task and shortcuts SETUP made?')) {
    Write-Host '  Nothing was changed.'
    Pause-Window '  Press Enter to close'
    exit 1
}

$changes = Read-SetupChanges
$found = 0

# 1. The two firewall openings SETUP added (by their exact names; no other rule is touched).
foreach ($name in @($script:FirewallRule, "$($script:FirewallRule) (Python)")) {
    if (Test-FirewallRule $name) {
        $found++
        Undo "firewall opening '$name'" { netsh advfirewall firewall delete rule name="$name" | Out-Null }
    }
}

# 2. Python block rules SETUP switched off: switch them back on.
if (@($changes.python_blocks_disabled).Count) {
    $policy = New-Object -ComObject HNetCfg.FwPolicy2
    foreach ($saved in @($changes.python_blocks_disabled)) {
        $rules = @($policy.Rules | Where-Object { $_.Name -eq $saved.name -and $_.ApplicationName -ieq $saved.program -and $_.Action -eq 0 -and -not $_.Enabled })
        foreach ($rule in $rules) {
            $found++
            Undo "firewall block for Python switched back on ('$($saved.name)')" { $rule.Enabled = $true }
        }
    }
}

# 3. Networks SETUP switched from Public to Private: only if the office agrees (the office keeps its PCs Private).
foreach ($name in @($changes.networks_set_private)) {
    $found++
    Write-Host "  SETUP set the network '$name' to Private." -ForegroundColor Yellow
    if ($DryRun) { Write-Host "  Would ask: set '$name' back to Public?"; continue }
    if (Ask "  Set '$name' back to Public? (Answer N if the office wants it Private for file sharing.)") {
        try {
            $profiles = @(Invoke-Cim { Get-NetConnectionProfile -ErrorAction Stop } | Where-Object { $_.Name -eq $name -and $_.NetworkCategory -eq 'Private' })
            foreach ($connection in $profiles) {
                Invoke-Cim { Set-NetConnectionProfile -InterfaceIndex $connection.InterfaceIndex -NetworkCategory Public -ErrorAction Stop }
            }
            Write-Host "  Undone: '$name' is Public again." -ForegroundColor Green
        } catch {
            Write-Host "  Could not change it ($($_.Exception.Message)). Use Settings > Network & internet instead." -ForegroundColor Yellow
        }
    }
}

# 4. The automatic start at sign-in.
if (Test-ScheduledTask $script:TaskName) {
    $found++
    Undo "automatic start '$($script:TaskName)'" { schtasks.exe /Delete /TN $script:TaskName /F | Out-Null }
}

# 5. The two desktop shortcuts.
$desktop = [Environment]::GetFolderPath('Desktop')
foreach ($shortcut in $script:Shortcuts) {
    $path = Join-Path $desktop $shortcut
    if (Test-Path -LiteralPath $path) {
        $found++
        Undo "desktop shortcut '$shortcut'" { Remove-Item -LiteralPath $path }
    }
}

if (-not $DryRun -and (Test-Path -LiteralPath $script:SetupRecord)) {
    # Kept, renamed, as a note of what was undone and when.
    Rename-Item -LiteralPath $script:SetupRecord -NewName "changes-undone-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"
}

Write-Host ''
if (-not $found) { Write-Host '  Nothing from SETUP was found on this computer.' -ForegroundColor Green }
Write-Host '  Left as they are (remove by hand only if this computer will never run the system again):'
Write-Host "    - the records and their backups: $($script:Root)\app\data  (copy them to a USB drive first)"
Write-Host '    - Python and Git: Settings > Apps'
Write-Host "    - this folder: $($script:Root)"
if ($changes.firewall_backup -and (Test-Path -LiteralPath $changes.firewall_backup)) {
    Write-Host ''
    Write-Host '  Last resort only: the whole firewall as it was before the first SETUP is saved in' -ForegroundColor Yellow
    Write-Host "    $($changes.firewall_backup)"
    Write-Host '  Restoring it also removes any firewall change made AFTER that day (by anyone). As administrator:'
    Write-Host "    netsh advfirewall import `"$($changes.firewall_backup)`""
}
Pause-Window '  Press Enter to close'
