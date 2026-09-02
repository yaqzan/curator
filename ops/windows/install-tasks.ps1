# Registers the "Curator Watchdog" scheduled task (every 5 min).
# Uses the shared hidden_run.vbs wrapper so the 5-minute ticks never flash a
# console window (same form as the live GameNight/Scribe watchdog tasks).
#
# Register-ScheduledTask needs elevation on this machine; the schtasks fallback
# below creates the same 5-minute task as the current user without elevation.
$ErrorActionPreference = 'Stop'

$watchdog = Join-Path $PSScriptRoot 'watchdog.ps1'
$vbs = 'C:\Development\Trader\scripts\hidden_run.vbs'
if (-not (Test-Path $vbs)) { throw "shared hidden_run.vbs not found at $vbs" }

$argString = "//B //Nologo `"$vbs`" `"powershell.exe`" `"-NoProfile`" `"-ExecutionPolicy`" `"Bypass`" `"-File`" `"$watchdog`""

try {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $argString
    $triggers = @(
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes 5) `
            -RepetitionDuration (New-TimeSpan -Days 3650)),
        (New-ScheduledTaskTrigger -AtLogOn)
    )
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName 'Curator Watchdog' -Action $action `
        -Trigger $triggers -Settings $settings -Force -ErrorAction Stop | Out-Null
    Write-Host 'Registered task: Curator Watchdog (every 5 min + at logon)'
} catch {
    Write-Host "Register-ScheduledTask failed ($($_.Exception.Message.Trim())) - falling back to schtasks"
    schtasks /create /tn "Curator Watchdog" /sc minute /mo 5 /tr "wscript.exe $argString" /f
    if ($LASTEXITCODE -ne 0) { throw "schtasks fallback failed with exit code $LASTEXITCODE" }
    Write-Host 'Registered task: Curator Watchdog (every 5 min, current user)'
}
