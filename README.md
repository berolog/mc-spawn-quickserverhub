# mc-spawn-agent

The **client half** of [mc-spawn-bot](https://gitlab.com/quickserverhub/applications/mc-hosting-bot)
— a tiny, auditable agent you run on **your own machine** so the Telegram bot can
spin up and manage a Minecraft server there.

**Outbound only. It opens NO inbound ports.** The agent dials *out* to the bot's
control endpoint, so it works behind NAT / a home router with nothing exposed and
nothing to port-forward. Stop it any time (`systemctl stop mc-spawn-agent`, or
`systemctl --user stop …` / `rc-service mc-spawn-agent stop` depending on backend).

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
3. **Validate + execute locally** — each command is a documented **`action + params`** (protocol
   v2), never a script. The agent parses it, validates the parameters against an exact schema,
   checks your **local policy**, and only then runs the matching **hardcoded capability** as a
   fixed `docker` argv (no shell): create/start/stop/restart/status/logs/delete a server, safe
   semantic RCON (list/say/whitelist/kick/difficulty/gamemode via the container's `rcon-cli`),
   backups, and linking your own **playit.gg** account for a public play address.
4. **Report** — posts a structured result envelope back to `CONTROL_URL/result`.

The bot never connects *to* the agent; everything is the agent reaching out. **The backend is
treated as untrusted — the agent is the security boundary** that decides what is safe to run on
your box (so a Go rewrite is a drop-in behind the same versioned protocol). See
[`SECURITY.md`](SECURITY.md), [`THREAT_MODEL.md`](THREAT_MODEL.md), and
[`docs/PROTOCOL_V2.md`](docs/PROTOCOL_V2.md).

## Install

The bot gives you the exact one-liner (with `CONTROL_URL` and a fresh `TOKEN`
filled in). It looks like:

```bash
curl -fsSL https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.sh \
  | CONTROL_URL=https://agent.quickserverhub.com TOKEN=<one-time-token> sh
```

The installer is **portable** (Ubuntu/Debian, Arch, Alpine, Fedora/RHEL,
openSUSE — `apt`/`dnf`/`yum`/`pacman`/`apk`/`zypper`) and **self-bootstrapping**:
it installs whatever is missing (`python3`, `bash`, `docker`) using your distro's
package manager, fetches `agent.py`, and registers a service via whatever init
exists:

| You run as | Backend chosen |
|------------|----------------|
| root + systemd | systemd **system** service |
| root + OpenRC (Alpine) | OpenRC service (`/etc/init.d`) |
| non-root + systemd | systemd **--user** service (lingering enabled via sudo if available) |
| anything else | `nohup` launcher + `@reboot` crontab |

**No forced `sudo`.** It escalates only when a missing package actually needs
root — so on a box that already has the prerequisites, a normal user installs
**rootless** (into `~/.local/share` + `~/.config`). Note: provisioning a server
needs Docker, which usually needs root or `docker`-group membership; the installer
sets this up when it can and warns otherwise.

Override `AGENT_RAW` to install from a fork or a pinned commit. Pipe to `bash`
instead of `sh` if you prefer — the script is POSIX `sh` either way.

### Windows

The bot gives you the PowerShell equivalent — run it in **PowerShell**:

```powershell
$env:CONTROL_URL='https://agent.quickserverhub.com'; $env:TOKEN='<one-time-token>'; `
  irm https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/main/install.ps1 | iex
```

On Windows, **hosting runs inside WSL** — there is one path, no Docker Desktop, no
Podman, no Git‑Bash. `install.ps1`:

1. installs **Python 3** per‑user via `winget` to run the agent (it resolves an
   **absolute** `python.exe`, dodging the Microsoft Store `python` stub and unrefreshed
   `PATH`), and registers autostart that runs **as you** (never SYSTEM) — a Scheduled Task
   (S4U at startup if elevated, else at logon), or, if the Task Scheduler is locked down,
   an HKCU `Run` key;
2. for hosting, sets up a dedicated **WSL2 distro `mc-spawn`** (imported from a small
   Ubuntu rootfs — no Microsoft Store) with **Docker** inside it, started on boot via
   `/etc/wsl.conf`. Servers then run exactly like on Ubuntu; WSL2 forwards their ports to
   Windows `localhost`.

**No admin is needed** — with one unavoidable exception: if WSL isn't enabled on the
machine yet, enabling it is a one‑time admin step (open an **admin** PowerShell, run
`wsl --install`, **reboot**, then re‑run this installer). That's a Windows security
boundary, not ours. Monitoring and RCON work even before WSL/hosting is set up.

Override the distro name with `MCSPAWN_WSL_DISTRO` and the rootfs with
`MCSPAWN_WSL_ROOTFS_URL`. Deleting the machine in the bot unregisters the whole
`mc-spawn` distro, so removal is clean.

### Manual run (dev)

```bash
CONTROL_URL=http://127.0.0.1:8080 TOKEN=<token> AGENT_STATE=/tmp/cred.json python3 agent.py
```

## Environment

| Var | Required | Default | Meaning |
|-----|----------|---------|---------|
| `CONTROL_URL` | yes | — | Operator control endpoint the agent dials out to. |
| `TOKEN` | first run only | — | One-time enroll token from the bot (ignored once enrolled). |
| `AGENT_STATE` | no | `/etc/mc-spawn-agent/cred.json` (root) or `~/.config/mc-spawn-agent/cred.json` (rootless) | Where the long-lived secret is stored. |
| `AGENT_RAW` | no | this repo's GitHub raw | Base URL `agent.py` is fetched from (forks / pinned commits). |
| `AGENT_LOG` | no | `agent.log` next to the cred file | Where the agent appends its log (it runs detached, so this file is how you see what it did). |
| `MCSPAWN_DEBUG` | no | off | `1` ⇒ verbose logs (engine detection, per-command output). Secrets/script text are never logged. |
| `CONTAINER_RUNTIME` | no | Linux: auto (`docker`→`podman`→`nerdctl`); Windows: `docker` (in WSL) | Force a specific container engine. |
| `MCSPAWN_WSL_DISTRO` | no | `mc-spawn` | (Windows) name of the dedicated WSL distro hosting runs in. |
| `MCSPAWN_WSL_ROOTFS_URL` | no | Ubuntu WSL rootfs | (Windows) rootfs the installer imports for that distro. |
| `AGENT_SHELL` | no | `bash` (Linux) / `wsl -d <distro>` (Windows) | Override the shell scripts run in (e.g. a non-WSL Windows setup). |

## Manage

Depends on the backend the installer picked (it prints which). Root systemd:

```bash
systemctl status  mc-spawn-agent
systemctl stop    mc-spawn-agent      # pause: bot can no longer reach this box
systemctl disable --now mc-spawn-agent
rm -rf /opt/mc-spawn-agent /etc/mc-spawn-agent   # full uninstall
```

Rootless systemd `--user`: same commands with `--user`; state lives in
`~/.local/share/mc-spawn-agent` + `~/.config/mc-spawn-agent`. Alpine/OpenRC:
`rc-service mc-spawn-agent {status,stop}`, `rc-update del mc-spawn-agent`. Nohup
fallback: `kill "$(cat ~/.config/mc-spawn-agent/agent.pid)"` and remove the
`@reboot` crontab line.

## Logs & debugging

The agent runs detached (systemd / hidden Scheduled-Task / Run-key), so it appends to a
log file — read that to see what it's doing:

```bash
# Linux
tail -f ~/.config/mc-spawn-agent/agent.log      # or /etc/mc-spawn-agent/agent.log (root)
journalctl -u mc-spawn-agent -f                  # systemd backends also log to the journal
```
```powershell
# Windows
Get-Content "$env:LOCALAPPDATA\mc-spawn-agent\agent.log" -Wait
```

The startup line shows the platform, the **chosen container engine**, and all paths.
Failed shell commands log their exit code + the engine's stderr (e.g. *"Cannot connect to
Podman"*), so engine/connection problems are obvious. For verbose output (engine
detection, per-command stdout) set `MCSPAWN_DEBUG=1` — re-run the installer with it
prepended, e.g. `MCSPAWN_DEBUG=1 CONTROL_URL=… TOKEN=… sh` (Linux) or
`$env:MCSPAWN_DEBUG=1` before the PowerShell one-liner. The agent never logs secrets or
the provisioning script (it carries the RCON password).

## Tests

```bash
python3 -m unittest discover -v tests
```

Pure, no network: shell executor, command dispatch, RCON soft-error path.

## Reliability & idempotency

- **Any container engine.** Auto-detects `docker → podman → nerdctl` (override with
  `CONTAINER_RUNTIME`); the installer installs docker only if none is present.
- **Auto-reconnect.** The poll loop backs off and retries through network blips, control-
  plane restarts (5xx) and edge errors; a rejected secret triggers one re-enroll if a
  fresh `TOKEN` is set, else a clean exit.
- **Self-healing servers.** The bot keeps each server at its desired state — a manually
  stopped/removed container is restarted or recreated (the world volume is reattached);
  a removed playit container is brought back. An intentional "stop" in the bot is honoured.
- **Self-heal + clean uninstall.** A deleted `agent.py` is re-fetched on the next restart;
  deleting the machine in the bot fully removes containers, playit, the service, and the
  agent's own files.

## Security posture

- **The backend is untrusted; the agent is the security boundary.** It can only run a fixed set
  of documented Minecraft operations (deny-by-default), never arbitrary code. No `shell`/`exec`
  action exists, and a CI test forbids shell-execution patterns in the source.
- **Owner-controlled local policy** (`policy.json`): the allowlist of actions, resource limits,
  and dangerous-action flags. The backend can't change it. Raw console + backup-restore are OFF
  by default. See [`docs/LOCAL_POLICY.md`](docs/LOCAL_POLICY.md).
- **Workspace jail** — all files live under `~/.mc-spawn`; no path traversal, no reading your home/
  SSH keys/etc. Container/volume/tunnel names are agent-derived, so the backend can't target an
  arbitrary resource ("delete only what it created").
- **No inbound ports**, NAT-friendly; only outbound HTTPS to `CONTROL_URL` (+ playit if you link
  it). Replay/expiry-protected; secret `chmod 600`, never logged.
- **Audit log** of every allow/deny decision (`python3 agent.py audit`).
- One file, stdlib only — auditable before you run it ([`docs/INSTALL_SAFELY.md`](docs/INSTALL_SAFELY.md)).
