# Curator watchdog - keeps the app + its dedicated tunnel alive on Windows.
# Registered by install-tasks.ps1 as "Curator Watchdog" (every 5 min + at logon).
#
# Created 2026-08-27: curator.yaqzan.dev had been 530 for an unknown stretch
# because nothing restarted the tunnel after the box migration. Design mirrors
# GameNight's watchdog: probe cheaply FIRST, invoke the shared controller
# (C:\Development\server.ps1) only for pieces that are actually down, and don't
# capture controller output (its Start-Process children hold inherited pipes
# open - the known foreground-hang class).
#
# Safe to run by hand:  powershell -ExecutionPolicy Bypass -File watchdog.ps1

$ErrorActionPreference = 'Continue'

$LogDir = Join-Path $PSScriptRoot 'logs'
$LogFile = Join-Path $LogDir 'watchdog.log'
$Controller = 'C:\Development\server.ps1'

function Write-Log {
    param([string]$Message)
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Add-Content -Path $LogFile -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

function Rotate-Log {
    if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 512KB)) {
        $tail = Get-Content $LogFile -Tail 1500
        Set-Content -Path $LogFile -Value $tail
    }
}

function Test-App {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5002/api/health' -TimeoutSec 8
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-Tunnel {
    return [bool](Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'run\s+curator(\s|$)' })
}

Rotate-Log

$down = @()
if (-not (Test-App)) { $down += 'curator-api' }
if (-not (Test-Tunnel)) { $down += 'curator-tunnel' }

if (-not $down) {
    Write-Log 'all up'
} else {
    foreach ($svc in $down) {
        Write-Log "$svc DOWN -> controller start"
        # Start-Process -Wait waits on the controller process itself, never on
        # pipes its persistent children inherit - the '| Out-Null' form hung
        # Scribe's watchdog on 2026-07-28 and wedged the task for 8+ hours.
        Start-Process powershell.exe -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $Controller, 'start', '-Service', $svc -WindowStyle Hidden -Wait
    }
    Start-Sleep -Seconds 20
    Write-Log ("post-start: app={0} tunnel={1}" -f (Test-App), (Test-Tunnel))
}
