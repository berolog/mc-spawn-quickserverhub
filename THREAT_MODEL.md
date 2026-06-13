# Threat model

The agent's job is to let a **closed-source, untrusted backend** manage Minecraft on your box
without that backend being able to harm you. This document states what we defend against, where
the boundaries are, and what is explicitly out of scope.

## Trust assumptions

- The **backend / Telegram bot is untrusted.** Assume it can be compromised, can send malformed
  or malicious command payloads, can include unexpected fields, and can replay old commands.
- The **user can be socially engineered** into pressing buttons in the bot.
- The **local machine and OS are trusted** up to the point of agent installation (we cannot
  defend a box that is already compromised).
- **Docker is trusted** to provide normal container isolation.

## Threats and mitigations

| Threat | Mitigation |
|--------|------------|
| Compromised backend sends a shell script / arbitrary command | No `shell`/`exec` action exists; commands are `action + params` validated against a hardcoded registry + schema. Unknown action → `denied`; unknown field → `invalid`. CI test forbids shell patterns. |
| Backend tries to name an arbitrary container / file / volume | Resource names are computed by the agent from a regex-validated `server_id`; the backend never supplies a name/path. |
| Backend tries to read/write arbitrary files | Workspace jail (`safe_join`): `..`, absolute paths, symlink escapes, drive/UNC paths are rejected. Only `~/.mc-spawn/**` is touched. |
| Backend sends a destructive op (delete/restore/uninstall) | Scoped to agent-created resources only, gated by a local policy flag, and the bot shows an itemized confirmation of exactly what is removed. |
| Backend injects a raw RCON/console command (`op attacker`) | No raw RCON by default; semantic actions map to fixed `rcon-cli` argv. Free-form `console_exec` is policy-gated (`allow_raw_rcon`), off by default. |
| Backend tells the agent to update itself from a URL | No update-from-URL action; the agent never fetches+runs code on backend instruction. `allow_agent_auto_update` is off by default. |
| Backend loosens local policy remotely | The agent reads `policy.json` but offers no action to modify it. |
| Replayed / stale command | Each command has `request_id` + `issued_at` + `expires_at`; expired/far-future/duplicate commands are rejected (duplicates return the prior result). |
| Resource exhaustion (huge RAM, odd ports) | Schema bounds (`ram_mb` 512–16384, port range) + policy caps (`max_ram_mb`, `allowed_port_range`). |
| Secret leakage via logs | Secrets (agent secret, RCON/playit keys) are never logged; the audit log records decisions only. |
| Compromised playit account | The user links their **own** playit account; the secret stays on the box (`chmod 600`) and is never sent to the backend. Only tunnels named by the agent are deleted. |
| Container escape from a malicious plugin/mod | Containers run unprivileged, no host PID/IPC, named volumes / workspace-scoped mounts only, sensitive host paths never mounted. (`allow_plugins`/`allow_mods` off by default.) |

## Security boundaries

1. **Local agent validation** — envelope parse, schema, deny-by-default registry.
2. **Local policy** — owner-controlled allowlist, limits, dangerous-action flags.
3. **Workspace jail** — all mutable state under one root; no arbitrary paths.
4. **No shell** — capabilities run fixed Docker argv.
5. **No raw RCON by default** — semantic actions only unless the owner opts in.
6. **Itemized confirmation** for destructive actions (bot side) + resource-scoping (agent side).

## Out of scope

- Vulnerabilities **inside the Minecraft server** itself, or inside Java/plugins/mods the user
  chooses to install.
- **OS-level compromise that predates** agent installation.
- **Docker daemon compromise** on a host where Docker is configured root-equivalent. (We
  recommend rootless Docker and a dedicated OS user — see
  [docs/INSTALL_SAFELY.md](docs/INSTALL_SAFELY.md).)
- Denial of service from the user's own actions (filling their disk with worlds/backups).
