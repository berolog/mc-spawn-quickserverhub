# mc-spawn agent installer for Windows (Phase 6). Run on YOUR OWN machine — the bot
# shows the full line:
#   $env:CONTROL_URL='<control-url>'; $env:TOKEN='<token>'; `
#     irm https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.ps1 | iex
#
# Mirrors install.sh: outbound-only (opens NO inbound ports), prefers a per-user
# install (no admin), registers a Scheduled Task to run now + at logon/startup and
# restart on failure. Inspect this script and agent.py before running (open source).
#
# Prereqs it checks/installs best-effort via winget: Python 3, Git (for bash — the
# bot's provisioning scripts are POSIX and run via `bash -c`), and it verifies Docker
# Desktop is present (needed to host a server; it can't be silently auto-installed —
# it requires WSL2 + a reboot, so we link it instead).

$ErrorActionPreference = 'Stop'

function Log  ($m) { Write-Host  "[mc-spawn-agent] $m" }
function Warn ($m) { Write-Warning "[mc-spawn-agent] $m" }
function Die  ($m) { Write-Error  "[mc-spawn-agent] $m"; exit 1 }

$ControlUrl = $env:CONTROL_URL
$Token      = $env:TOKEN
$AgentRaw   = if ($env:AGENT_RAW) { $env:AGENT_RAW } `
             else { 'https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main' }

if (-not $ControlUrl) { Die 'CONTROL_URL env is required' }
if (-not $Token)      { Die 'TOKEN env is required' }

# ---- privilege: per-user by default, system only if already elevated ----
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($IsAdmin) {
    $Dir   = Join-Path $env:ProgramData 'mc-spawn-agent'
    $State = $Dir
} else {
    $Dir   = Join-Path $env:LOCALAPPDATA 'mc-spawn-agent'
    $State = $Dir
}
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# ---- prerequisites (best-effort via winget; never hard-fail the install) ----
function Have ($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Winget-Install ($id) {
    if (-not (Have winget)) { return $false }
    Log "installing $id via winget"
    winget install --id $id --silent --accept-source-agreements --accept-package-agreements `
        --disable-interactivity 2>$null | Out-Null
    # winget doesn't refresh THIS session's PATH; pull machine+user PATH so the new
    # exe is visible to the checks below without a restart.
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path','User')
    return $true
}

# Python 3 — the agent's only real runtime dependency.
if (-not (Have python) -and -not (Have py)) {
    Winget-Install 'Python.Python.3.12' | Out-Null
}
if (-not (Have python) -and -not (Have py)) {
    Die 'Python 3 not found and could not be installed — install it and re-run'
}
$Python = if (Have python) { 'python' } else { 'py' }

# bash — the bot emits POSIX provisioning scripts (docker one-liners with `||`,
# `command -v`, redirects). Git for Windows ships bash.exe on PATH; without it the
# agent still runs (monitoring/RCON), but hosting a server would fail.
if (-not (Have bash)) {
    Winget-Install 'Git.Git' | Out-Null
}
if (-not (Have bash)) {
    Warn 'bash not found — monitoring/RCON will work, but provisioning a server needs bash (install Git for Windows or enable WSL).'
}

# Docker — required to actually host a server. Can't auto-install (WSL2 + reboot),
# so verify and link.
if (-not (Have docker)) {
    Warn 'Docker not found — install Docker Desktop (WSL2 backend) to host a server: https://www.docker.com/products/docker-desktop/'
} elseif (-not (docker info 2>$null)) {
    Warn 'Docker is installed but not running — start Docker Desktop before hosting a server.'
} else {
    Log 'docker present'
}

# ---- fetch agent.py ----
Log 'fetching agent.py'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri "$AgentRaw/agent.py" -OutFile (Join-Path $Dir 'agent.py')

# ---- launcher carrying the config (incl. the one-time TOKEN) ----
# ACL it to the current user only so the secret isn't readable by others (the
# Windows analogue of install.sh's chmod 600 on run.sh).
$Run = Join-Path $Dir 'run.cmd'
$CredPath = Join-Path $State 'cred.json'
@"
@echo off
set "CONTROL_URL=$ControlUrl"
set "TOKEN=$Token"
set "AGENT_STATE=$CredPath"
"$Python" "$($Dir)\agent.py"
"@ | Set-Content -Path $Run -Encoding ASCII

function Lock-ToCurrentUser ($path) {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    icacls $path /inheritance:r /grant:r "${me}:(R,W)" "SYSTEM:(F)" 2>$null | Out-Null
}
Lock-ToCurrentUser $Run

# ---- service registration: Scheduled Task (no extra deps like NSSM) ----
$TaskName = 'mc-spawn-agent'
$action   = New-ScheduledTaskAction -Execute $Run
# Keep it running: restart on failure, no time limit, start even on battery.
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

try {
    if ($IsAdmin) {
        # System-wide: start at boot as SYSTEM, independent of login.
        $trigger   = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Log 'installed as a Scheduled Task running at startup (SYSTEM)'
    } else {
        # Per-user: start at logon for the current user (no admin needed).
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Force | Out-Null
        Log 'installed as a per-user Scheduled Task running at logon'
    }
    Start-ScheduledTask -TaskName $TaskName
    Log 'mc-spawn agent installed and started. Управление — в Telegram-боте.'
} catch {
    Die "could not register the Scheduled Task: $_"
}
