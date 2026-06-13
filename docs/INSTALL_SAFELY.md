# Installing safely

This agent is open source and single-file — **read it before you run it.** The bot hands you a
one-liner for convenience, but you don't have to pipe a script straight into a shell.

## Inspect-then-run (recommended over `curl | sh`)

Linux/macOS:

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/<TAG>/install.sh
less install.sh                 # read it
sha256sum install.sh            # compare against the release checksum
CONTROL_URL=... TOKEN=... sh install.sh
```

Windows (PowerShell):

```powershell
Invoke-WebRequest -OutFile install.ps1 https://raw.githubusercontent.com/berolog/mc-spawn-quickserverhub/<TAG>/install.ps1
Get-Content .\install.ps1       # read it
Get-FileHash .\install.ps1 -Algorithm SHA256
$env:CONTROL_URL='...'; $env:TOKEN='...'; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Prefer a **release tag** over `main` (`AGENT_RAW=.../<TAG>`), and verify the published
`SHA256SUMS` for that release. Releases include a changelog.

## What the installer creates

- `agent.py` + `run.sh`/`run.cmd` in the install dir (`/opt/mc-spawn-agent`, `~/.local/share/
  mc-spawn-agent`, or `%LOCALAPPDATA%\mc-spawn-agent`).
- Credentials + policy + log in the state dir (`/etc/mc-spawn-agent`, `~/.config/mc-spawn-agent`,
  or `%LOCALAPPDATA%\mc-spawn-agent`): `cred.json` (0600), `policy.json`, `agent.log`.
- A workspace at `~/.mc-spawn/` (worlds, backups, logs, tmp — everything the agent writes).
- One **autostart** entry: a systemd unit (system or `--user`), an OpenRC service, or a
  `@reboot` crontab on Linux; an HKCU `…\Run` key (per-user, not SYSTEM) on Windows.

## Network

**Outbound only — no inbound ports are opened on your box.** The agent dials:

- your `CONTROL_URL` (the operator's control plane), over HTTPS; and
- `https://api.playit.gg` *only if* you choose to link a public play address.

## Reducing Docker risk

Provisioning needs a container engine. To minimize blast radius:

- Prefer **rootless Docker** or **Podman** (the agent auto-detects `docker → podman → nerdctl`).
- Prefer a **dedicated OS user** (e.g. `mcspawn`) rather than adding your primary account to the
  `docker` group (docker-group membership is root-equivalent). On Windows, hosting is confined to
  a dedicated WSL2 distro.
- The agent runs containers unprivileged, with named volumes / workspace-scoped mounts only, and
  never mounts `/`, `/home`, `/etc`, the Docker socket, or your profile directory.

## Verify before trusting

```bash
python3 agent.py capabilities    # exactly what this agent will permit
python3 agent.py policy          # your local policy
python3 agent.py audit           # decisions it has made
```

See [UNINSTALL.md](UNINSTALL.md) to remove everything.
