#!/usr/bin/env bash
# mc-watch agent installer. Run on YOUR OWN machine (the bot shows the full line):
#   curl -fsSL https://raw.githubusercontent.com/quickserverhub/mc-watch-agent/main/install.sh \
#     | sudo CONTROL_URL=<control-url> TOKEN=<token> bash
#
# Outbound only — opens NO inbound ports. Installs a tiny python3 agent as a
# systemd service. Inspect this script and agent.py before running (open source:
# https://github.com/quickserverhub/mc-watch-agent).
set -euo pipefail

CONTROL_URL="${CONTROL_URL:-}"
TOKEN="${TOKEN:-}"
# Where to fetch agent.py from (override for forks / pinned commits).
AGENT_RAW="${AGENT_RAW:-https://raw.githubusercontent.com/quickserverhub/mc-watch-agent/main}"
DIR=/opt/mc-watch-agent
STATE=/etc/mc-watch-agent

if [ -z "$CONTROL_URL" ]; then echo "CONTROL_URL env is required" >&2; exit 1; fi
if [ -z "$TOKEN" ]; then echo "TOKEN env is required" >&2; exit 1; fi
if ! command -v python3 >/dev/null 2>&1; then echo "python3 is required" >&2; exit 1; fi

mkdir -p "$DIR" "$STATE"
# Fetch the agent (stdlib-only single file).
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$AGENT_RAW/agent.py" -o "$DIR/agent.py"
else
  wget -qO "$DIR/agent.py" "$AGENT_RAW/agent.py"
fi
chmod 0644 "$DIR/agent.py"

cat > /etc/systemd/system/mc-watch-agent.service <<EOF
[Unit]
Description=mc-watch agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Environment=CONTROL_URL=$CONTROL_URL
Environment=TOKEN=$TOKEN
Environment=AGENT_STATE=$STATE/cred.json
ExecStart=/usr/bin/env python3 $DIR/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mc-watch-agent.service
echo "mc-watch agent installed and started. Управление — в Telegram-боте."
