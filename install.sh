#!/bin/sh
# mc-spawn agent installer. Run on YOUR OWN machine (the bot shows the full line):
#   curl -fsSL https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.sh \
#     | CONTROL_URL=<control-url> TOKEN=<token> sh
#
# Portable across Ubuntu/Debian, Arch, Alpine, Fedora/RHEL, openSUSE (apt/dnf/
# yum/pacman/apk/zypper). Installs missing prerequisites (python3, bash, docker)
# and registers a service via whatever init exists (systemd system or --user, or
# OpenRC, else a nohup launcher). Escalates with sudo ONLY when a missing package
# actually needs root — if everything is present and you run as a normal user, no
# admin rights are requested.
#
# Outbound only — opens NO inbound ports. Inspect this script and agent.py before
# running (open source: https://github.com/berolog/mc-spawn-quickserverhub).
#
# POSIX sh on purpose: Alpine ships busybox ash, not bash, so the installer must
# not depend on bash (the agent itself does — we install it below).
set -eu

CONTROL_URL="${CONTROL_URL:-}"
TOKEN="${TOKEN:-}"
# Where to fetch agent.py from (override for forks / pinned commits).
AGENT_RAW="${AGENT_RAW:-https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main}"

log()  { printf '[mc-spawn-agent] %s\n' "$*"; }
warn() { printf '[mc-spawn-agent] WARN: %s\n' "$*" >&2; }
die()  { printf '[mc-spawn-agent] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "$CONTROL_URL" ] || die "CONTROL_URL env is required"
[ -n "$TOKEN" ]       || die "TOKEN env is required"

# ---- privilege: prefer rootless, escalate only when a package needs it ----
if [ "$(id -u)" = 0 ]; then
  ROOT=1; SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  ROOT=0; SUDO="sudo"
else
  ROOT=0; SUDO=""
fi
can_escalate() { [ "$ROOT" = 1 ] || [ -n "$SUDO" ]; }

# ---- package manager abstraction ----
PM=""
for _pm in apt-get dnf yum pacman apk zypper; do
  if command -v "$_pm" >/dev/null 2>&1; then PM="$_pm"; break; fi
done

# Map a logical package name to the distro's real one (most match; these don't).
pkgname() {
  case "$1" in
    python3) [ "$PM" = pacman ] && echo python || echo python3 ;;
    docker)  [ "$PM" = apt-get ] && echo docker.io || echo docker ;;
    *)       echo "$1" ;;
  esac
}

_apt_updated=0
pm_install() {
  [ -n "$PM" ] || die "no supported package manager found; install $* manually"
  can_escalate || die "$* missing and no root/sudo to install it; install it manually and re-run"
  log "installing: $*"
  case "$PM" in
    apt-get)
      [ "$_apt_updated" = 1 ] || { $SUDO apt-get update -qq; _apt_updated=1; }
      DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y "$@" ;;
    dnf)    $SUDO dnf install -y "$@" ;;
    yum)    $SUDO yum install -y "$@" ;;
    pacman) $SUDO pacman -S --needed --noconfirm "$@" ;;
    apk)    $SUDO apk add --no-cache "$@" ;;
    zypper) $SUDO zypper --non-interactive install "$@" ;;
  esac
}

# Ensure a command exists, installing its package if missing.
ensure_cmd() {
  cmd="$1"; logical="${2:-$1}"
  command -v "$cmd" >/dev/null 2>&1 && return 0
  pm_install "$(pkgname "$logical")"
}

# ---- prerequisites ----
# A fetch tool for agent.py (curl preferred; wget fallback).
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  ensure_cmd curl
fi
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  else wget -qO "$2" "$1"; fi
}

ensure_cmd python3 python3
# The agent runs bot scripts via `bash -c`, so bash is a runtime requirement
# (Alpine has none by default).
ensure_cmd bash bash

# Container engine — needed to provision the Minecraft server. docker/podman/nerdctl
# all work (the agent auto-detects via $MCSPAWN_RT): if ANY is present we install
# nothing; otherwise install docker (the most universal default). Best-effort: prefer
# the distro package; fall back to get.docker.com only if that fails and we can root.
setup_docker() {
  if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1 \
     || command -v nerdctl >/dev/null 2>&1; then
    log "container engine present"
  elif [ -n "$PM" ] && can_escalate; then
    if ! pm_install "$(pkgname docker)"; then
      warn "distro docker package failed; trying get.docker.com"
      $SUDO sh -c 'curl -fsSL https://get.docker.com | sh' || warn "docker install failed"
    fi
  elif can_escalate; then
    $SUDO sh -c 'curl -fsSL https://get.docker.com | sh' || warn "docker install failed"
  else
    warn "docker missing and no root/sudo to install it — provisioning a server will fail until docker is available"
    return 0
  fi
  # Start the daemon and make the invoking user able to reach it without root.
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
  elif command -v rc-update >/dev/null 2>&1; then
    $SUDO rc-update add docker default >/dev/null 2>&1 || true
    $SUDO rc-service docker start >/dev/null 2>&1 || true
  fi
  if [ "$ROOT" = 0 ] && command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1 && [ -n "$SUDO" ]; then
      $SUDO usermod -aG docker "$(id -un)" 2>/dev/null \
        && warn "added $(id -un) to the docker group — log out/in (or run 'newgrp docker') for it to take effect"
    fi
  fi
}
setup_docker

# ---- install location (system when root, per-user otherwise) ----
if [ "$ROOT" = 1 ]; then
  DIR=/opt/mc-spawn-agent
  STATE=/etc/mc-spawn-agent
else
  DIR="${XDG_DATA_HOME:-$HOME/.local/share}/mc-spawn-agent"
  STATE="${XDG_CONFIG_HOME:-$HOME/.config}/mc-spawn-agent"
fi
mkdir -p "$DIR" "$STATE"

log "fetching agent.py"
fetch "$AGENT_RAW/agent.py" "$DIR/agent.py"
chmod 0644 "$DIR/agent.py"

# Launcher carries the config (incl. the one-time TOKEN), kept 0600 so secrets
# never land in a world-readable unit file or in `ps`. Every backend execs this.
RUN="$DIR/run.sh"
cat > "$RUN" <<EOF
#!/bin/sh
export CONTROL_URL="$CONTROL_URL"
export TOKEN="$TOKEN"
export AGENT_STATE="$STATE/cred.json"
export MCSPAWN_DEBUG="${MCSPAWN_DEBUG:-}"
# Self-heal (Phase 6.5): if the user deleted agent.py by hand, re-fetch it before
# launch so the service recovers on its next restart instead of crash-looping.
if [ ! -f "$DIR/agent.py" ]; then
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$AGENT_RAW/agent.py" -o "$DIR/agent.py"
  else wget -qO "$DIR/agent.py" "$AGENT_RAW/agent.py"; fi
fi
exec python3 "$DIR/agent.py"
EOF
chmod 0700 "$RUN"

# ---- service registration: pick the available init ----
systemd_user_ok() {
  [ -n "${XDG_RUNTIME_DIR:-}" ] && command -v systemctl >/dev/null 2>&1 \
    && systemctl --user show-environment >/dev/null 2>&1
}

install_systemd_system() {
  cat > /etc/systemd/system/mc-spawn-agent.service <<EOF
[Unit]
Description=mc-spawn agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=$RUN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now mc-spawn-agent.service
  log "installed as a systemd system service (systemctl status mc-spawn-agent)"
}

install_systemd_user() {
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/mc-spawn-agent.service" <<EOF
[Unit]
Description=mc-spawn agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$RUN
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now mc-spawn-agent.service
  # Without lingering a user service dies on logout; needs root to enable.
  if [ -n "$SUDO" ]; then
    $SUDO loginctl enable-linger "$(id -un)" >/dev/null 2>&1 \
      && log "lingering enabled — agent survives logout" \
      || warn "could not enable lingering; agent may stop when you log out"
  else
    warn "no root to enable lingering — the agent may stop when you log out (run: loginctl enable-linger $(id -un))"
  fi
  log "installed as a systemd --user service (systemctl --user status mc-spawn-agent)"
}

install_openrc() {
  cat > /etc/init.d/mc-spawn-agent <<EOF
#!/sbin/openrc-run
name="mc-spawn-agent"
description="mc-spawn agent"
command="$RUN"
command_background=true
pidfile="/run/mc-spawn-agent.pid"
output_log="/var/log/mc-spawn-agent.log"
error_log="/var/log/mc-spawn-agent.log"

depend() {
  need net
  after docker
}
EOF
  chmod 0755 /etc/init.d/mc-spawn-agent
  rc-update add mc-spawn-agent default
  rc-service mc-spawn-agent restart
  log "installed as an OpenRC service (rc-service mc-spawn-agent status)"
}

install_fallback() {
  # No service manager we can use — run detached and re-launch at boot via cron.
  pidfile="$STATE/agent.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
  fi
  # Redirect raw stdout/stderr to agent.out (crash-capture). The agent also keeps its
  # own structured log at $STATE/agent.log — that's the one to read.
  nohup "$RUN" >"$STATE/agent.out" 2>&1 &
  echo $! > "$pidfile"
  if command -v crontab >/dev/null 2>&1; then
    cron_line="@reboot $RUN >>$STATE/agent.out 2>&1"
    ( crontab -l 2>/dev/null | grep -vF "$RUN"; echo "$cron_line" ) | crontab - 2>/dev/null \
      && log "added @reboot crontab entry for restart-on-boot" \
      || warn "could not install crontab entry — agent won't auto-start after reboot"
  else
    warn "no service manager and no crontab — agent runs now but won't auto-start after reboot"
  fi
  log "running via nohup (log: $STATE/agent.log)"
}

if [ "$ROOT" = 1 ] && command -v systemctl >/dev/null 2>&1; then
  install_systemd_system
elif [ "$ROOT" = 1 ] && command -v rc-update >/dev/null 2>&1; then
  install_openrc
elif systemd_user_ok; then
  install_systemd_user
else
  install_fallback
fi

log "mc-spawn agent installed and started. Управление — в Telegram-боте."
log "agent log: $STATE/agent.log   (tail -f $STATE/agent.log)"
log "verbose:   re-run the installer with  MCSPAWN_DEBUG=1  prepended"
