# Security

The mc-spawn agent runs on **your** machine so a Telegram bot can manage a Minecraft server
for you. The bot's backend is **closed-source and treated as untrusted**. This agent is the
**security boundary**: it only ever performs a small, fixed set of documented Minecraft
operations, and it decides locally whether each one is allowed.

## The core guarantee

> A compromised or malicious backend **cannot run code on your machine.** It can only request
> documented Minecraft operations (by name, with typed parameters), and this open-source agent
> decides whether each request is valid, allowed by your local policy, and safe to run.

If that statement is ever false, it's a security bug — please report it (see below).

## What the agent CAN do

Only the actions in its hardcoded registry, and only those your local policy allows:

- create / start / stop / restart / delete a Minecraft container (fixed `docker run
  itzg/minecraft-server` argv — RCON bound to `127.0.0.1` only);
- read its status and bounded logs;
- safe semantic server commands via the container's `rcon-cli`: list players, say a message,
  save-all, whitelist add/remove/list, kick, set difficulty/gamemode;
- create/list/delete world backups (inside the workspace);
- link **your own** playit.gg account and manage tunnels for the public play address;
- uninstall itself (remove only the containers/volumes it created, plus its own files).

## What the agent CANNOT do

- **No arbitrary shell.** There is no `shell`/`exec`/`run`/`script` action; nothing the backend
  sends is ever executed as an OS command. (`bash -c`, `sh -c`, `Invoke-Expression`,
  `shell=True`, etc. do not exist in the agent — enforced by a CI test.)
- **No raw RCON by default.** The backend cannot send arbitrary console commands. A free-form
  console exists only if **you** opt in (`allow_raw_rcon` in your policy) — it's off by default.
- **No arbitrary files or paths.** All mutable files live under one workspace root
  (`~/.mc-spawn`); path traversal, absolute paths, and symlink escapes are rejected. Your home
  directory, SSH keys, browser profiles, etc. are never read.
- **No backend-named resources.** Container/volume/tunnel names are computed by the agent from a
  validated `server_id`; the backend can't point an operation at an arbitrary container.
- **No backend-driven updates.** The backend cannot tell the agent to fetch and run new code.
- **No policy loosening over the wire.** The backend cannot change your local policy.

## Local controls

- **`policy.json`** (owner-only) — the allowlist of actions, resource limits, and the
  dangerous-action flags. See [docs/LOCAL_POLICY.md](docs/LOCAL_POLICY.md).
- **Audit log** — every allow/deny decision is recorded. See [docs/AUDIT_LOG.md](docs/AUDIT_LOG.md).
  View recent decisions: `python3 agent.py audit`.
- **Inspect what's permitted:** `python3 agent.py capabilities` / `python3 agent.py policy`.
- **Revoke:** stop the service any time; `python3 agent.py wipe-creds` removes the agent secret.

## Transport hardening

- Outbound HTTPS only (a non-HTTPS `CONTROL_URL` is refused unless `MCSPAWN_DEV=1`).
- Each command carries `request_id` + `issued_at` + `expires_at`; the agent rejects **expired**,
  **far-future** (clock skew), and **replayed** commands.
- The agent secret is stored with owner-only permissions (`chmod 600`) and never logged.

## Reporting a vulnerability

Please open a private report at the public repo
(`https://github.com/berolog/mc-spawn-quickserverhub`) or email the maintainer. Include the
agent version (`python3 agent.py capabilities`), your OS, and reproduction steps. Do not file
public exploit details before a fix is available.
