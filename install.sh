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
DEFAULT_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
export PATH="${PATH:-$DEFAULT_PATH}"

log()  { printf 'level=info event=%s%s\n' "$1" "${2:+ $2}"; }
warn() { printf 'level=warning event=%s%s\n' "$1" "${2:+ $2}" >&2; }
die()  { printf 'level=error event=%s%s\n' "$1" "${2:+ $2}" >&2; exit 1; }

[ -n "$CONTROL_URL" ] || die "config_missing" "key=CONTROL_URL"
[ -n "$TOKEN" ]       || die "config_missing" "key=TOKEN"

# ---- privilege: prefer rootless, escalate only when a package needs it ----
if [ "$(id -u)" = 0 ]; then
  ROOT=1; SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  ROOT=0; SUDO="sudo"
else
  ROOT=0; SUDO=""
fi
can_escalate() { [ "$ROOT" = 1 ] || [ -n "$SUDO" ]; }
INSTALL_USER="$(id -un)"
DOCKER_NEEDS_SYSTEMD_GROUP=0

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
  [ -n "$PM" ] || die "package_manager_missing" "packages=$*"
  can_escalate || die "package_install_denied" "packages=$*"
  log "package_install" "packages=$*"
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

# Docker is needed to provision the Minecraft server. Best-effort: prefer the distro package;
# fall back to get.docker.com only if that fails and we can root.
runtime_path() {
  p="$(command -v docker 2>/dev/null || true)"
  if [ -n "$p" ]; then printf '%s\n' "$p"; return 0; fi
  old_ifs=$IFS; IFS=:
  for d in $DEFAULT_PATH; do
    if [ -x "$d/docker" ]; then IFS=$old_ifs; printf '%s\n' "$d/docker"; return 0; fi
  done
  IFS=$old_ifs
  return 1
}

install_docker_upstream() {
  can_escalate || return 1
  tmp="${TMPDIR:-/tmp}/mc-spawn-get-docker.sh"
  fetch "https://get.docker.com" "$tmp" || return 1
  $SUDO sh "$tmp"
}

setup_docker() {
  if engine="$(runtime_path)"; then
    log "container_engine_present" "path=$engine"
  elif [ -n "$PM" ] && can_escalate; then
    if ! pm_install "$(pkgname docker)"; then
      warn "docker_package_failed"
      install_docker_upstream || warn "docker_install_failed"
    fi
  elif can_escalate; then
    install_docker_upstream || warn "docker_install_failed"
  else
    die "container_engine_missing"
  fi
  if ! engine="$(runtime_path)"; then
    if can_escalate; then
      install_docker_upstream || true
    fi
  fi
  engine="$(runtime_path)" || die "container_engine_missing"
  log "container_engine_ready" "path=$engine"
  # Start the daemon and make the invoking user able to reach it without root.
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
  elif command -v rc-update >/dev/null 2>&1; then
    $SUDO rc-update add docker default >/dev/null 2>&1 || true
    $SUDO rc-service docker start >/dev/null 2>&1 || true
  fi
  if [ "$ROOT" = 0 ] && command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1 && [ -n "$SUDO" ]; then
      if $SUDO docker info >/dev/null 2>&1; then
        if $SUDO usermod -aG docker "$INSTALL_USER" 2>/dev/null; then
          if command -v systemctl >/dev/null 2>&1 && getent group docker >/dev/null 2>&1; then
            DOCKER_NEEDS_SYSTEMD_GROUP=1
            log "docker_group_added" "user=$INSTALL_USER mode=systemd"
          else
            warn "docker_group_added" "user=$INSTALL_USER mode=new_session_required"
          fi
        fi
      fi
    fi
  fi
}
setup_docker
DOCKER_BIN="$(runtime_path)" || die "container_engine_missing"

# ---- install location (system when root, per-user otherwise) ----
if [ "$ROOT" = 1 ]; then
  DIR=/opt/mc-spawn-agent
  STATE=/etc/mc-spawn-agent
else
  DIR="${XDG_DATA_HOME:-$HOME/.local/share}/mc-spawn-agent"
  STATE="${XDG_CONFIG_HOME:-$HOME/.config}/mc-spawn-agent"
fi
mkdir -p "$DIR" "$STATE"

# ---- local policy (owner-controlled; the backend can NEVER change it) ----
# Write a conservative default the machine owner can edit. The agent reads this file
# ($STATE/policy.json by default) and enforces it locally; the raw console and backup-restore
# are OFF until the owner opts in. Never overwrite an existing policy (the owner may have edited it).
POLICY="$STATE/policy.json"
if [ ! -f "$POLICY" ]; then
  cat > "$POLICY" <<'EOF'
{
  "policy_version": 1,
  "workspace_root": "~/.mc-spawn",
  "allowed_actions": [
    "agent.health", "agent.capabilities", "agent.uninstall",
    "minecraft.server.create", "minecraft.server.start", "minecraft.server.stop",
    "minecraft.server.restart", "minecraft.server.status", "minecraft.server.logs",
    "minecraft.server.delete", "minecraft.server.reconcile_status",
    "minecraft.server.say", "minecraft.server.save_all",
    "minecraft.server.console_tail", "minecraft.server.console_exec",
    "minecraft.config.set_difficulty", "minecraft.config.set_gamemode",
    "minecraft.player.list", "minecraft.player.whitelist_add",
    "minecraft.player.whitelist_remove", "minecraft.player.whitelist_list",
    "minecraft.player.kick",
    "minecraft.backup.create", "minecraft.backup.list",
    "minecraft.backup.delete", "minecraft.backup.restore",
    "playit.claim_begin", "playit.claim_poll", "playit.playit_start",
    "playit.ensure_tunnel", "playit.status", "playit.remove_tunnel", "playit.teardown"
  ],
  "max_ram_mb": 8192,
  "allowed_port_range": [25565, 25700],
  "allow_server_delete": true,
  "allow_agent_uninstall": true,
  "allow_raw_rcon": false,
  "allow_backup_restore": false,
  "allow_plugins": false,
  "allow_mods": false,
  "allow_agent_auto_update": false
}
EOF
  chmod 0644 "$POLICY"
  log "policy_written" "path=$POLICY"
fi

log "agent_fetch" "url=$AGENT_RAW/agent.py"
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
export PATH="$DEFAULT_PATH"
export DOCKER_BIN="$DOCKER_BIN"
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
Environment=PATH=$DEFAULT_PATH
Environment=DOCKER_BIN=$DOCKER_BIN
ExecStart=$RUN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable mc-spawn-agent.service
  systemctl restart mc-spawn-agent.service
  log "service_installed" "manager=systemd scope=system"
}

install_systemd_system_user() {
  unit="${TMPDIR:-/tmp}/mc-spawn-agent.service.$$"
  cat > "$unit" <<EOF
[Unit]
Description=mc-spawn agent
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
User=$INSTALL_USER
SupplementaryGroups=docker
Environment=PATH=$DEFAULT_PATH
Environment=DOCKER_BIN=$DOCKER_BIN
ExecStart=$RUN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl --user disable --now mc-spawn-agent.service >/dev/null 2>&1 || true
  $SUDO mv "$unit" /etc/systemd/system/mc-spawn-agent.service
  $SUDO chmod 0644 /etc/systemd/system/mc-spawn-agent.service
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable mc-spawn-agent.service
  $SUDO systemctl restart mc-spawn-agent.service
  log "service_installed" "manager=systemd scope=system user=$INSTALL_USER docker_group=true"
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
Environment=PATH=$DEFAULT_PATH
Environment=DOCKER_BIN=$DOCKER_BIN
ExecStart=$RUN
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable mc-spawn-agent.service
  systemctl --user restart mc-spawn-agent.service
  # Without lingering a user service dies on logout; needs root to enable.
  if [ -n "$SUDO" ]; then
    $SUDO loginctl enable-linger "$(id -un)" >/dev/null 2>&1 \
      && log "linger_enabled" "user=$INSTALL_USER" \
      || warn "linger_failed" "user=$INSTALL_USER"
  else
    warn "linger_unavailable" "user=$INSTALL_USER"
  fi
  log "service_installed" "manager=systemd scope=user"
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
  log "service_installed" "manager=openrc"
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
      && log "crontab_installed" \
      || warn "crontab_failed"
  else
    warn "autostart_unavailable"
  fi
  log "service_installed" "manager=nohup log=$STATE/agent.log"
}

if [ "$ROOT" = 1 ] && command -v systemctl >/dev/null 2>&1; then
  install_systemd_system
elif [ "$ROOT" = 1 ] && command -v rc-update >/dev/null 2>&1; then
  install_openrc
elif [ "$DOCKER_NEEDS_SYSTEMD_GROUP" = 1 ] && [ -n "$SUDO" ]; then
  install_systemd_system_user
elif systemd_user_ok; then
  install_systemd_user
else
  install_fallback
fi

log "install_complete"
# Transparency (spec §14): show exactly what was created + what the agent talks to, so the
# owner can audit and remove it. No inbound ports are opened on this box.
log "paths" "install_dir=$DIR state_dir=$STATE policy=$POLICY"
log "workspace" "path=$HOME/.mc-spawn"
log "network" "control_url=$CONTROL_URL playit_url=https://api.playit.gg"
log "logs" "agent=$STATE/agent.log audit=$HOME/.mc-spawn/logs/audit.log"
