# mc-spawn agent installer for Windows (Phase 6). Run on YOUR OWN machine — the bot
# shows the full line:
#   $env:CONTROL_URL='<control-url>'; $env:TOKEN='<token>'; `
#     irm https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.ps1 | iex
#
# Mirrors install.sh: outbound-only (opens NO inbound ports), registers autostart that
# runs as YOU (never SYSTEM — see below). Works without admin. Inspect this and agent.py
# before running (open source).
#
# What it sets up: Python 3 (per-user via winget) to run the agent, and — for HOSTING —
# a dedicated **WSL2 Linux distro** (`mc-spawn`) with Docker inside, where servers run "as
# on Ubuntu". This is the single Windows hosting path (no Docker Desktop, no Podman, no
# Git-Bash). The ONLY admin step, and only on a box without WSL, is enabling WSL once
# (`wsl --install` + reboot) — a Windows security boundary we can't bypass.

$ErrorActionPreference = 'Stop'

function Log  ($m) { Write-Host    "[mc-spawn-agent] $m" }
function Warn ($m) { Write-Warning "[mc-spawn-agent] $m" }
function Die  ($m) { Write-Error   "[mc-spawn-agent] $m"; exit 1 }

$ControlUrl = $env:CONTROL_URL
$Token      = $env:TOKEN
$AgentRaw   = if ($env:AGENT_RAW) { $env:AGENT_RAW } `
             else { 'https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main' }

if (-not $ControlUrl) { Die 'CONTROL_URL env is required' }
if (-not $Token)      { Die 'TOKEN env is required' }

# ---- privilege: per-user install dir; admin only changes WHERE the task runs ----
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# Always install under the invoking user's profile — the task runs AS this user (not
# SYSTEM), so it must see the same Python/PATH the user has. Using ProgramData with a
# SYSTEM task was the old bug: winget installs Python per-user, so SYSTEM couldn't find
# it and the agent died silently.
$Dir   = Join-Path $env:LOCALAPPDATA 'mc-spawn-agent'
$State = $Dir
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

function Have ($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Refresh-Path {
    # winget doesn't update THIS session's PATH; pull machine+user PATH so a freshly
    # installed exe is visible to the checks below without reopening the shell.
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path','User')
}

function Winget-Install ($id) {
    if (-not (Have winget)) { return $false }
    Log "installing $id via winget (per-user — no admin prompt)"
    try {
        # Per-user scope on purpose: the Scheduled Task runs AS THIS USER, so a user-scope
        # install is what it needs — and it avoids the UAC prompt that --scope machine
        # triggers (which made the installer appear to hang behind a modal dialog).
        winget install --id $id --scope user --silent --accept-source-agreements `
            --accept-package-agreements --disable-interactivity 2>$null | Out-Null
    } catch { }
    Refresh-Path
    return $true
}

# ---- resolve a REAL python.exe (absolute path) ----------------------------------
# Two Windows gotchas this avoids: (1) the Microsoft Store alias %LOCALAPPDATA%\
# Microsoft\WindowsApps\python.exe is a no-op stub that just opens the Store — running
# it never starts the agent; (2) PATH may not yet reflect a fresh winget install. We
# return an ABSOLUTE path and bake it into the launcher so nothing depends on PATH.
function Resolve-Python {
    foreach ($name in 'python','python3') {
        foreach ($g in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($g.Source -and (Test-Path $g.Source) -and ($g.Source -notmatch 'WindowsApps')) {
                return $g.Source
            }
        }
    }
    # The `py` launcher resolves the genuine interpreter even when only the Store stub
    # is on PATH.
    if (Have py) {
        try {
            $exe = (& py -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1)
            if ($exe -and (Test-Path $exe)) { return $exe }
        } catch { }
    }
    # Last resort: scan the usual winget/python.org install roots.
    foreach ($root in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python'),
        'C:\')) {
        if (Test-Path $root) {
            $hit = Get-ChildItem -Path $root -Filter python.exe -Recurse -Depth 2 `
                       -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch 'WindowsApps' } |
                   Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

$Python = Resolve-Python
if (-not $Python) {
    Winget-Install 'Python.Python.3.12' | Out-Null
    $Python = Resolve-Python
}
if (-not $Python) {
    Die 'Python 3 not found and could not be installed — install it from https://www.python.org/downloads/ (tick "Add to PATH") and re-run'
}
Log "using Python at $Python"

# ---- fetch agent.py ----
Log 'fetching agent.py'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri "$AgentRaw/agent.py" -OutFile (Join-Path $Dir 'agent.py')

# ---- launcher carrying the config (incl. the one-time TOKEN) ----
# Bakes the ABSOLUTE python path so the task never depends on PATH. ACL'd to the
# current user (the Windows analogue of install.sh's chmod 600 on run.sh).
$Run = Join-Path $Dir 'run.cmd'
$CredPath = Join-Path $State 'cred.json'
$DebugFlag = $env:MCSPAWN_DEBUG   # set MCSPAWN_DEBUG=1 before running to get verbose logs
$WslDistro = if ($env:MCSPAWN_WSL_DISTRO) { $env:MCSPAWN_WSL_DISTRO } else { 'mc-spawn' }
@"
@echo off
set "CONTROL_URL=$ControlUrl"
set "TOKEN=$Token"
set "AGENT_STATE=$CredPath"
set "MCSPAWN_DEBUG=$DebugFlag"
set "MCSPAWN_WSL_DISTRO=$WslDistro"
rem Self-heal (Phase 6.5): re-fetch agent.py if the user deleted it by hand.
if not exist "$($Dir)\agent.py" powershell -NoProfile -Command "irm '$AgentRaw/agent.py' -OutFile '$($Dir)\agent.py'"
"$Python" "$($Dir)\agent.py"
"@ | Set-Content -Path $Run -Encoding ASCII

function Lock-ToCurrentUser ($path) {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    icacls $path /inheritance:r /grant:r "${me}:(R,W)" "SYSTEM:(F)" 2>$null | Out-Null
}
Lock-ToCurrentUser $Run

# ---- service registration: Scheduled Task running AS THE CURRENT USER ----
# Never SYSTEM: the agent must see the user's Python and the user's WSL (the `mc-spawn`
# distro is per-user). Admin lets us use S4U so it also starts at boot without anyone
# logging in; without admin it starts at logon.
$TaskName = 'mc-spawn-agent'
$UserId   = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action   = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
    -Argument "/c `"$Run`"" -WorkingDirectory $Dir
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Fallback autostart for when the Task Scheduler is off-limits (many standard-user /
# locked-down boxes deny registering a task in the root folder — that's the "Access is
# denied" some users hit). The HKCU Run key needs NO admin and NO Task Scheduler access;
# it starts the agent at every logon. A tiny .vbs launches run.cmd hidden (no console
# flash). Trade-off vs the task: logon-only (not pre-login boot) and no restart-on-crash,
# but the agent already self-recovers its connection, so this is a fine degradation.
function Install-RunKeyAutostart {
    # .vbs launches run.cmd hidden (window mode 0) — no console flash at logon. In a
    # double-quoted here-string `"` is literal, so `""` is VBS's escaped quote.
    $vbs = Join-Path $Dir 'launch.vbs'
    @"
CreateObject("WScript.Shell").Run "cmd /c ""$Run""", 0, False
"@ | Set-Content -Path $vbs -Encoding ASCII
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-ItemProperty -Path $runKey -Name $TaskName -Value "wscript.exe `"$vbs`"" `
        -PropertyType String -Force | Out-Null
    Start-Process wscript.exe -ArgumentList "`"$vbs`""   # start now, hidden
    Log 'installed via per-user startup (Run key) — starts at logon. The bot will see it shortly.'
}

# Best-effort: clear any task a previous run left (old versions registered one under
# SYSTEM). If we can't, no problem — we just won't reuse the task path.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false } catch { }
}

$registered = $false
try {
    if ($IsAdmin) {
        # S4U: runs as the current user at startup, no stored password, has internet
        # access (outbound HTTPS only). Starts whether or not the user is logged in.
        $triggers  = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Principal $principal -Settings $settings | Out-Null
    } else {
        # No explicit principal: defaults to the creating user, runs only while logged on.
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings | Out-Null
    }
    Start-ScheduledTask -TaskName $TaskName
    $registered = $true
    Log "installed as a Scheduled Task (runs as $UserId) — the bot will see it shortly."
} catch {
    # Most common: standard user denied the root task folder. Degrade, don't die.
    Warn "Task Scheduler unavailable ($($_.Exception.Message)) — falling back to per-user startup."
}
if (-not $registered) {
    try { Install-RunKeyAutostart }
    catch { Die "could not set up autostart (Task Scheduler and Run key both failed): $_" }
}

# ---- hosting backend: a dedicated WSL2 Linux distro with Docker inside --------------
# THE single Windows hosting path: servers run "as on Ubuntu" inside an isolated `mc-spawn`
# WSL distro. Only needed to HOST (not to enroll/monitor/RCON), and slow (imports a rootfs +
# installs Docker), so it runs LAST — Ctrl-C here leaves a working, enrolled agent. No admin,
# EXCEPT the one Windows step we can't bypass: enabling WSL itself (once) on a box without it.
$env:WSL_UTF8 = '1'   # make `wsl -l -q` emit UTF-8, not UTF-16 (which breaks -match)

function Wsl-Enabled {
    & wsl --status 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Setup-WslHosting {
    if (-not (Wsl-Enabled)) { & wsl --update 2>&1 | Out-Null }   # best-effort kernel update
    if (-not (Wsl-Enabled)) {
        Warn 'WSL is not enabled. This is the ONLY step that needs admin, and only once:'
        Warn '  1) open PowerShell as Administrator   2) run:  wsl --install   3) REBOOT'
        Warn '  4) re-run this installer (no admin needed after that). Monitoring/RCON already work.'
        return
    }

    $have = @(& wsl -l -q | ForEach-Object { $_.Trim() }) -contains $WslDistro
    if ($have) {
        & wsl -d $WslDistro -- sh -lc 'service docker start >/dev/null 2>&1; docker info >/dev/null 2>&1'
        if ($LASTEXITCODE -eq 0) { Log "WSL hosting ready (distro '$WslDistro')"; return }
    } else {
        Log "creating the '$WslDistro' WSL distro (downloads a small Ubuntu rootfs)…"
        $rootfsUrl = if ($env:MCSPAWN_WSL_ROOTFS_URL) { $env:MCSPAWN_WSL_ROOTFS_URL } `
                     else { 'https://cloud-images.ubuntu.com/wsl/noble/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz' }
        $tar = Join-Path $Dir 'distro-rootfs.tar.gz'
        $distroDir = Join-Path $Dir 'wsl'
        New-Item -ItemType Directory -Force -Path $distroDir | Out-Null
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $rootfsUrl -OutFile $tar
            & wsl --import $WslDistro $distroDir $tar --version 2
            if ($LASTEXITCODE -ne 0) { throw 'wsl --import failed' }
        } catch {
            Warn "could not create the WSL distro: $_"
            Warn 'Set MCSPAWN_WSL_ROOTFS_URL to a valid Ubuntu WSL rootfs and re-run. Monitoring/RCON still work.'
            return
        } finally {
            Remove-Item $tar -ErrorAction SilentlyContinue
        }
    }

    # Install Docker inside the distro (root → no admin, no password) and make it start on
    # every distro boot via /etc/wsl.conf (no systemd dependency). Then restart + verify.
    Log 'installing Docker inside the distro (one-time)…'
    $setup = 'export DEBIAN_FRONTEND=noninteractive; ' +
             'command -v docker >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq docker.io; }; ' +
             'printf ''[boot]\ncommand=service docker start\n'' > /etc/wsl.conf; ' +
             'service docker start >/dev/null 2>&1 || true'
    & wsl -d $WslDistro -- sh -lc $setup 2>&1 | Out-Null
    & wsl --terminate $WslDistro 2>&1 | Out-Null   # apply /etc/wsl.conf boot command
    & wsl -d $WslDistro -- sh -lc 'service docker start >/dev/null 2>&1; docker info >/dev/null 2>&1'
    if ($LASTEXITCODE -eq 0) { Log "WSL hosting ready (distro '$WslDistro')" }
    else { Warn "Docker did not come up in '$WslDistro' — re-run the installer, or check: wsl -d $WslDistro -- docker info" }
}

try { Setup-WslHosting } catch { Warn "WSL hosting setup error: $($_.Exception.Message)" }

Log "done. Управление — в Telegram-боте. Лог агента: $Dir\agent.log"
Log "посмотреть лог:   Get-Content '$Dir\agent.log' -Wait"
Log 'подробный лог:    перед запуском установщика задай  $env:MCSPAWN_DEBUG=1'
