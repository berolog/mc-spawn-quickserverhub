# mc-spawn agent installer for Windows (Phase 6). Run on YOUR OWN machine — the bot
# shows the full line:
#   $env:CONTROL_URL='<control-url>'; $env:TOKEN='<token>'; `
#     irm https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.ps1 | iex
#
# Mirrors install.sh: outbound-only (opens NO inbound ports), registers a Scheduled
# Task that runs as YOU (never SYSTEM — see below) and restarts on failure. Works with
# or without admin. Inspect this script and agent.py before running (open source).
#
# Prereqs (best-effort via winget): Python 3, Git (for bash — the bot's provisioning
# scripts are POSIX and run via `bash -c`), and a container engine. We prefer **Podman**
# (free, CLI-only, no Docker Desktop / no license) and only fall back to linking Docker.

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
    Log "installing $id via winget"
    try {
        # --scope machine where possible so the exe lands on a PATH the task user shares;
        # winget falls back to user scope if machine isn't supported for the package.
        winget install --id $id --scope machine --silent --accept-source-agreements `
            --accept-package-agreements --disable-interactivity 2>$null | Out-Null
    } catch { }
    if ($LASTEXITCODE -ne 0) {
        try {
            winget install --id $id --silent --accept-source-agreements `
                --accept-package-agreements --disable-interactivity 2>$null | Out-Null
        } catch { }
    }
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

# ---- bash (Git for Windows) — needed to host a server, not to run the agent ----
if (-not (Have bash)) { Winget-Install 'Git.Git' | Out-Null }
if (-not (Have bash)) {
    Warn 'bash not found — monitoring/RCON will work, but hosting a server needs bash (install Git for Windows or enable WSL).'
}

# ---- container engine: prefer the lightweight, license-free Podman -------------
# All of docker/podman/nerdctl work (the agent auto-detects via $MCSPAWN_RT). On
# Windows we DEFAULT to Podman: it's free, CLI-only, and doesn't require Docker
# Desktop. (Containers still run in a small WSL2 VM that `podman machine` sets up.)
function Ensure-Engine {
    if (Have docker)  { Log 'docker present'; return }
    if (Have nerdctl) { Log 'nerdctl present'; return }
    if (-not (Have podman)) {
        Log 'no container engine — installing Podman (lightweight, no Docker Desktop)'
        Winget-Install 'RedHat.Podman' | Out-Null
    }
    if (-not (Have podman)) {
        Warn 'No container engine installed. To host a server, install Podman (`winget install RedHat.Podman`) or Docker Desktop. Monitoring/RCON work without one.'
        return
    }
    # podman needs a one-time WSL2-backed machine; best-effort, non-fatal.
    try {
        $machines = (& podman machine list --format '{{.Name}}' 2>$null)
        if (-not $machines) {
            Log 'initialising the Podman machine (one-time, uses WSL2)…'
            & podman machine init 2>$null | Out-Null
        }
        & podman machine start 2>$null | Out-Null
        Log 'podman ready'
    } catch {
        Warn 'Podman is installed but its machine could not start — run `podman machine init; podman machine start` once (needs WSL2; enable it with `wsl --install` and reboot if missing).'
    }
}
try { Ensure-Engine } catch { Warn "container-engine setup skipped: $_" }

# ---- fetch agent.py ----
Log 'fetching agent.py'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri "$AgentRaw/agent.py" -OutFile (Join-Path $Dir 'agent.py')

# ---- launcher carrying the config (incl. the one-time TOKEN) ----
# Bakes the ABSOLUTE python path so the task never depends on PATH. ACL'd to the
# current user (the Windows analogue of install.sh's chmod 600 on run.sh).
$Run = Join-Path $Dir 'run.cmd'
$CredPath = Join-Path $State 'cred.json'
@"
@echo off
set "CONTROL_URL=$ControlUrl"
set "TOKEN=$Token"
set "AGENT_STATE=$CredPath"
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
# Never SYSTEM: the agent must see the user's Python and (for hosting) the user's
# docker/podman, which live in the user profile. Admin lets us use S4U so it also
# starts at boot without anyone logging in; without admin it starts at logon.
$TaskName = 'mc-spawn-agent'
$UserId   = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action   = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
    -Argument "/c `"$Run`"" -WorkingDirectory $Dir
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

try {
    if ($IsAdmin) {
        # S4U: runs as the current user at startup, no stored password, has internet
        # access (outbound HTTPS only). Starts whether or not the user is logged in.
        $triggers  = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Principal $principal -Settings $settings -Force | Out-Null
        Log "installed as a Scheduled Task running at startup as $UserId"
    } else {
        $trigger   = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Log "installed as a per-user Scheduled Task running at logon as $UserId"
    }
    Start-ScheduledTask -TaskName $TaskName
    Log 'mc-spawn agent installed and started. Управление — в Telegram-боте.'
} catch {
    Die "could not register the Scheduled Task: $_"
}
