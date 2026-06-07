# mc-spawn-agent

The **client half** of [mc-spawn-bot](https://gitlab.com/quickserverhub/applications/mc-hosting-bot)
— a tiny, auditable agent you run on **your own machine** so the Telegram bot can
spin up and manage a Minecraft server there.

**Outbound only. It opens NO inbound ports.** The agent dials *out* to the bot's
control endpoint, so it works behind NAT / a home router with nothing exposed and
nothing to port-forward. Stop it any time with `systemctl stop mc-spawn-agent`.

> Single file, **standard-library Python 3 only** — no pip installs. Read
> [`agent.py`](agent.py) before you run it; that's the whole client.

## How it works

```
agent (your box, OUTBOUND only) ──HTTPS──▶ control_api (operator)
   │  runs docker + local RCON                 │
   ▼                                           ▼
 127.0.0.1:25565 / :25575                  Postgres command queue ◀── mc-spawn-bot
```

1. **Enroll** — on first start the agent redeems a one-time `TOKEN` (issued by the
   bot) at `CONTROL_URL/enroll` and gets a long-lived secret, saved to
   `/etc/mc-spawn-agent/cred.json` (`chmod 600`).
2. **Poll** — it long-polls `CONTROL_URL/poll` for commands (Bearer secret).
3. **Execute locally** —
   - `shell` — runs a script (the bot provisions Docker + `itzg/minecraft-server`);
   - `rcon` — speaks Source RCON to `127.0.0.1:<port>` (the server is local, no tunnel);
   - `playit` — links the user's own playit.gg account (claim flow) and reports the
     public address friends connect to — no inbound port opened on the box.
4. **Report** — posts the result back to `CONTROL_URL/result`.

The bot never connects *to* the agent; everything is the agent reaching out. All
Minecraft logic lives in the bot — the agent is a thin executor (so a Go rewrite is
a drop-in behind the same HTTP protocol).

## Install

The bot gives you the exact one-liner (with `CONTROL_URL` and a fresh `TOKEN`
filled in). It looks like:

```bash
curl -fsSL https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.sh \
  | sudo CONTROL_URL=https://agent.quickserverhub.com TOKEN=<one-time-token> bash
```

This installs `agent.py` to `/opt/mc-spawn-agent/`, writes a systemd unit, and
starts it. Requirement on the box: **`python3`** (Docker is installed by the bot's
provision step when you create a server).

Override `AGENT_RAW` to install from a fork or a pinned commit.

### Manual run (dev)

```bash
CONTROL_URL=http://127.0.0.1:8080 TOKEN=<token> AGENT_STATE=/tmp/cred.json python3 agent.py
```

## Environment

| Var | Required | Default | Meaning |
|-----|----------|---------|---------|
| `CONTROL_URL` | yes | — | Operator control endpoint the agent dials out to. |
| `TOKEN` | first run only | — | One-time enroll token from the bot (ignored once enrolled). |
| `AGENT_STATE` | no | `/etc/mc-spawn-agent/cred.json` | Where the long-lived secret is stored. |

## Manage

```bash
systemctl status  mc-spawn-agent
systemctl stop    mc-spawn-agent      # pause: bot can no longer reach this box
systemctl disable --now mc-spawn-agent
rm -rf /opt/mc-spawn-agent /etc/mc-spawn-agent   # full uninstall
```

## Tests

```bash
python3 -m unittest discover -v tests
```

Pure, no network: shell executor, command dispatch, RCON soft-error path.

## Security posture

- **No inbound ports**, NAT-friendly; only outbound HTTPS to `CONTROL_URL`.
- The long-lived secret is `chmod 600` and never logged.
- Provisioned servers bind **RCON to `127.0.0.1` only** — never the internet.
- One file, stdlib only — auditable before you pipe it to `sudo bash`.
