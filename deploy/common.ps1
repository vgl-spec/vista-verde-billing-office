# Shared by the install, start and update scripts. Windows PowerShell 5.1 compatible (office PCs).
#
# Reading the network (addresses, "is the server already running") uses .NET, not the NetTCPIP/NetSecurity
# cmdlets: those go through Windows' management service (WMI/CIM), which can refuse access for a while after
# sign-in or on a PC where it is broken. Proved in Windows Sandbox: Get-NetConnectionProfile -> "Cannot connect
# to CIM server. Access is denied" stopped SETUP half-way.

$script:Root = Split-Path -Parent $PSScriptRoot   # the BMS folder (this file lives in BMS\deploy)
$script:Port = if ($env:BMS_PORT) { [int]$env:BMS_PORT } else { 8765 }
$script:FirewallRule = 'Vista Verde billing'
$script:VirtualAdapters = 'vEthernet|WSL|VirtualBox|VMware|Loopback|Bluetooth'

function Find-Python {
    # The py launcher (python.org installer) first, then python on PATH. Returns @(exe, args...) or $null.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python') }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $rest = @($candidate | Select-Object -Skip 1)
        $version = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version) {
            $parts = $version.Trim().Split('.')
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) { return , $candidate }
        }
    }
    return $null
}

function Invoke-Python {
    param([object[]]$Python, [string[]]$Arguments)
    $exe = $Python[0]
    $rest = @($Python | Select-Object -Skip 1)
    & $exe @rest @Arguments
}

function Invoke-Cim {
    # For the few steps that need Windows' management service: it can refuse access for a while right after
    # sign-in, so wait and retry (up to about a minute) before giving up.
    param([scriptblock]$Action, [int]$Tries = 12)
    for ($attempt = 1; $attempt -le $Tries; $attempt++) {
        try { return & $Action }
        catch {
            if ($attempt -eq $Tries) { throw }
            Start-Sleep -Seconds 5
        }
    }
}

function Get-LanAddresses {
    # IPv4 addresses the other computers can reach. Adapters with a router (default gateway) first: virtual
    # adapters (WSL, Hyper-V switch, VirtualBox) have none and would only confuse staff. Without any router
    # (PCs joined by a bare switch), every real-looking adapter is listed.
    $found = foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($nic.OperationalStatus -ne 'Up' -or $nic.NetworkInterfaceType -eq 'Loopback' -or $nic.Name -match $script:VirtualAdapters) { continue }
        $properties = $nic.GetIPProperties()
        $routed = @($properties.GatewayAddresses | Where-Object {
            $_.Address.AddressFamily -eq 'InterNetwork' -and $_.Address.ToString() -ne '0.0.0.0' }).Count -gt 0
        foreach ($unicast in $properties.UnicastAddresses) {
            $ip = $unicast.Address.ToString()
            if ($unicast.Address.AddressFamily -ne 'InterNetwork' -or $ip -like '169.254.*' -or $ip -like '127.*') { continue }
            [pscustomobject]@{ Address = $ip; Adapter = $nic.Name; Routed = $routed }
        }
    }
    $withRouter = @($found | Where-Object { $_.Routed })
    $chosen = if ($withRouter.Count) { $withRouter } else { @($found) }
    return @($chosen | Sort-Object Adapter | ForEach-Object { [pscustomobject]@{ Address = $_.Address; Adapter = $_.Adapter } })
}

function Get-PythonBlockRules([string]$PythonExe) {
    # Enabled inbound rules that BLOCK this python.exe on private or domain networks. Windows adds one when someone
    # presses Cancel on its "allow Python?" popup, and a block rule beats every allow rule. Read through the
    # firewall's COM interface: no management service needed and the same on every Windows language.
    try {
        $policy = New-Object -ComObject HNetCfg.FwPolicy2
        return @($policy.Rules | Where-Object {
            $_.Direction -eq 1 -and $_.Action -eq 0 -and $_.Enabled -and $_.ApplicationName -and
            $_.ApplicationName -ieq $PythonExe -and ($_.Profiles -band 3) })
    } catch {
        return @()
    }
}

function Test-ServerRunning {
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return [bool](@($listeners | Where-Object { $_.Port -eq $script:Port }).Count)
}

function Write-Addresses {
    Write-Host ''
    Write-Host '  On THIS computer open:        ' -NoNewline; Write-Host "http://127.0.0.1:$($script:Port)" -ForegroundColor Green
    $addresses = @(Get-LanAddresses)
    if ($addresses.Count -eq 0) {
        Write-Host '  No network connection found. Plug in the network cable or join the office Wi-Fi.' -ForegroundColor Yellow
    } else {
        Write-Host '  On the OTHER computers open one of these (same network):'
        foreach ($item in $addresses) {
            Write-Host "     http://$($item.Address):$($script:Port)" -ForegroundColor Green -NoNewline
            Write-Host "    ($($item.Adapter))"
        }
        Write-Host "     http://$($env:COMPUTERNAME):$($script:Port)" -ForegroundColor Green -NoNewline
        Write-Host '    (computer name; works on most office networks)'
    }
    Write-Host ''
}
