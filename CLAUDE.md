# CLAUDE.md

**mc-spawn-agent** — the client half of **mc-spawn-bot**, installed by the user on their **own**
machine so the bot can provision and manage a Minecraft server there. **Outbound only: it opens
NO inbound ports** (dials out to the operator's `control_api`, long-polls a command queue, runs
allowed operations locally). Separate repo so users can inspect exactly what they install and the
client can version/release independently. The server half (control plane, queue, bot UI) lives in
**mc-spawn-bot** (`gitlab.com/quickserverhub/applications/mc-hosting-bot`).

## Security posture — the agent is the security boundary (protocol v2)

**The backend (Telegram bot / control plane) is UNTRUSTED.** It can only send documented
`action + params` commands; it can NEVER send code to run. The agent parses, schema-validates,
checks a local owner-controlled policy, and only then maps each command to a **hardcoded
capability** that runs a fixed `docker`/`wsl` argv — **never a shell**. This *reverses* the old
"thin executor" model: the Minecraft/docker/RCON logic now lives IN THE AGENT (the security
boundary), and the bot is a thin requester. See `SECURITY.md`, `THREAT_MODEL.md`,
`docs/PROTOCOL_V2.md`, `docs/LOCAL_POLICY.md`, `docs/AUDIT_LOG.md`.

Core guarantee: *a malicious backend's worst case is invoking allowed Minecraft capabilities
within local policy — never arbitrary OS commands or files.*

## Files

- `agent.py` — **stdlib only** (urllib/json/subprocess/re/datetime + platform/shutil). The whole
  client. Sections: enroll/poll loop (`main`, re-enrolls once on 401; `_post_result` retries
  result delivery until the control plane accepts it); **v2 dispatch**
  (`process_command`: envelope parse → expiry/replay guard → registry → policy → schema →
  capability → result envelope); **policy** (`_load_policy`, `_DEFAULT_POLICY`, deny-by-default);
  **schema** (`SCHEMAS`, `validate_params`, unknown-field reject); **path jail** (`safe_join`,
  `SecurityError`); **safe subprocess** (`run_allowed` — engine allowlist, argv only, never shell;
  Windows routes via `wsl -d <distro> -- <exe> ...`); **capabilities** (`act_*` handlers +
  `ACTION_REGISTRY`); **semantic RCON** (`_rcon_cli` = `docker exec <container> rcon-cli <args>`,
  no password handled); **playit** (`act_playit_*` + API/container helpers); **self-uninstall**
  (`act_uninstall` scoped to `mcspawn-server-*` + `_cleanup_main` detached, argv/winreg only);
  **audit** (`_audit`); **CLI** (`audit|policy|capabilities|wipe-creds|approve`). `PROTOCOL_VERSION
  = 2`. No third-party deps — a Go rewrite stays a drop-in behind the same protocol.
- `install.sh` / `install.ps1` — portable installers. Set up the container engine **once** (the
  agent never installs packages at runtime), write a conservative default `policy.json` (owner-
  editable), register a per-user/rootless-preferring autostart, and print created files + the
  network endpoints contacted. Linux installer handles the fresh-Docker group-membership gap:
  if the invoking user cannot reach `/var/run/docker.sock` until a new login session, it installs
  a systemd system unit that still runs as that user but with `SupplementaryGroups=docker`, so no
  server reboot is needed. Installer-generated launchers/units set a known system `PATH`
  (including `/snap/bin`) so the agent can find Docker from systemd's sparse environment; the
  installer verifies Docker exists after install, writes the resolved absolute path as
  `DOCKER_BIN`, and stops with `container_engine_missing` instead of enrolling a broken agent.
  Pin `AGENT_RAW` to a release tag for reproducibility.
- `mc-spawn-agent.service` — reference systemd unit (installers generate the real one).
- `tests/test_agent.py` — security tests: rejection matrix (shell/unknown-field/traversal/ram/
  raw-rcon/update-url all denied/invalid), policy gating, replay/expiry, path jail, capability
  argv shape (RCON loopback, name derived from server_id, semantic rcon-cli).
- `tests/test_no_shell.py` — CI static check: fails if `agent.py` grows any shell-execution
  pattern (`shell=True`, `os.system`, `bash -c`, `sh -c`, `Invoke-Expression`, `iex`,
  `payload["script"]`, …). Exceptions only via an explicit `# shell-audit: ok` tag.
- `SECURITY.md` / `THREAT_MODEL.md` / `docs/*` — security docs (protocol, policy, install,
  uninstall, audit).

## Protocol (cross-repo contract with mc-spawn-bot's `control_api.py` + `agent_client.py`)

HTTP endpoints are unchanged (`/enroll` · `/poll` · `/result` · `/heartbeat`); **v2 rides in the
payload**, so the control plane stays a dumb pass-through (no schema change). Bump
`PROTOCOL_VERSION` only on a breaking wire change; update both repos together.

| Call | Auth | Body / Result |
|------|------|---------------|
| `POST /enroll` | one-time token | `{token, protocol_version:2, platform}` → `{machine_id, secret}` |
| `GET /poll` | `Bearer <secret>` | → `{id, kind:"action", payload:<v2 envelope>}` or `204` |
| `POST /result` | Bearer | `{id, status:"done", result:<v2 result envelope>}` |
| `POST /heartbeat` | Bearer | — |

**Command envelope** (in `payload`): `{protocol_version:2, request_id, action, params, issued_at,
expires_at}`. **Result envelope** (in `result`): `{request_id, status, action, result,
agent_policy_version}`, `status ∈ ok|denied|invalid|failed|pending_local_approval` (queue row is
always `done`; the bot reads `result.status`). Full action table: `docs/PROTOCOL_V2.md`.
Result delivery is retried by the agent until `/result` returns 200; a transient network/control
plane blip after a long Docker command must not leave the queue row stuck in `running`.

## Key invariants

1. **Outbound-only, no inbound ports.** The agent never listens; it only dials `CONTROL_URL`
   (HTTPS — non-HTTPS refused unless `MCSPAWN_DEV=1`). Revocable via `systemctl stop`.
2. **stdlib only.** No pip/third-party imports (audit/trust story + Go-rewrite seam). Real
   `User-Agent` on every request (Cloudflare BIC bans the default urllib UA). Keep it that way.
3. **The agent is the security boundary; the backend is untrusted.** Every command is parsed,
   schema-validated, policy-checked, and mapped to a hardcoded capability. Deny-by-default:
   unknown action → `denied`; unknown field/bad type/out-of-range → `invalid`; never fail open.
4. **No shell, ever.** No `_run_shell`/`_shell_argv`/`AGENT_SHELL`. Capabilities run fixed Docker
   argv via `run_allowed` (Windows via `wsl -d <distro> -- docker ...`).
   `test_no_shell.py` enforces this in CI. Agent-internal maintenance uses `_run_internal` (argv).
5. **Resource names are agent-derived.** Container = `mcspawn-server-<validated server_id>`,
   volume = `<container>_data`, playit container = `mc-spawn-playit`, tunnels = `mc-spawn-<port>`.
   The backend NEVER supplies a container/volume/path name → it can only ever touch agent-created
   resources ("delete only what it created").
6. **Workspace jail.** All mutable files live under `workspace_root` (`~/.mc-spawn`). `safe_join`
   rejects `..`/absolute/symlink/drive/UNC escapes. Home dirs, SSH keys, browser profiles, etc.
   are never read.
7. **Local policy is owner-controlled and never loosened remotely.** `policy.json` gates
   `allowed_actions`, `max_ram_mb`, `allowed_port_range`, and the dangerous flags
   (`allow_server_delete`/`allow_agent_uninstall` default on but scoped + bot-confirmed;
   `allow_raw_rcon`/`allow_backup_restore` default OFF). The agent reads it but offers no action
   to change it. Missing/invalid file → built-in conservative defaults (never fail open).
8. **Semantic RCON, no raw by default.** Player/server commands run as `docker exec <container>
   rcon-cli <fixed args>` (no password handled by the agent, no shell). Free-form
   `console_exec` is gated by `allow_raw_rcon` (off) — the WebApp live-console hook.
9. **Replay/expiry protection.** Each command carries `request_id`/`issued_at`/`expires_at`;
   expired, far-future (±120 s skew), and duplicate commands are rejected (duplicates return the
   cached result). Secret stays `chmod 600`, never logged.
10. **Per-user playit, secret stays on the box.** User links their OWN playit account; the secret
    is `chmod 600` next to the cred file, never sent upstream. Only agent-named tunnels are
    touched. (playit has no API to delete the agent registration itself — dashboard-only.)
11. **Cross-platform, one codebase — Windows hosting in WSL.** Linux runs the engine directly;
    Windows routes engine argv through `wsl -d <distro>` in `run_allowed` (`_ensure_engine_ready`
    starts dockerd argv-only). The engine is set up ONCE by the installer; the agent never
    installs packages at runtime (a missing engine → structured `failed` with
    `container_engine_unavailable:<runtime>`).
12. **Audit + transparency.** Runtime logs and audit decisions use one key-value format
    (`ts=… level=… event=…`, plus fields), never secrets. Every decision is appended to
    `~/.mc-spawn/logs/audit.log` (action, request_id, OK/DENIED/INVALID/FAILED, reason).
    CLI: `agent.py audit|policy|capabilities|wipe-creds`.
13. **Scoped self-uninstall.** `agent.uninstall` (policy `allow_agent_uninstall`) removes only
    containers matching its own `mcspawn-server-*` prefix + their volumes + playit, then spawns a
    detached Python `_cleanup` (argv/winreg only, no shell) that removes the service/autostart +
    files (Windows also `wsl --unregister`s the distro), then exits.

> **Live-verify note:** the playit claim handshake + tunnel create/delete and the Windows WSL
> path (engine bootstrap, `wsl --unregister`, autostart) need a real box — they can't run in CI.
> The pure dispatch/validation/policy/replay paths are unit-tested.

## Run / test

```bash
# dev run against a local control_api (http allowed only with MCSPAWN_DEV=1)
CONTROL_URL=https://agent.example.com TOKEN=<token> AGENT_STATE=/tmp/cred.json python3 agent.py

python3 -m unittest discover -v tests
python3 -c "import ast; ast.parse(open('agent.py').read())"
python3 agent.py capabilities      # what this agent permits
```

## Conventions

- **Minimal, stdlib only.** One file, no framework.
- **Comments**: only WHY for surprising/security-relevant decisions; no restating WHAT.
- **No emoji in code** except user-visible strings.
- **Never reintroduce a shell-execution path.** Go through `run_allowed` (capabilities) or
  `_run_internal` (fixed agent maintenance); both are argv-only. `test_no_shell.py` is the gate.

## Git workflow

- Conventional commits, **single-line title only** — no body, no footer.
- **NEVER add `Co-Authored-By` (or any trailer/footer).** Explicit user rule; overrides defaults.
- Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `perf`, `test`. Optional scope.

## This file

- Update after every meaningful change (new action, schema/policy field, protocol or invariant
  shift). The protocol table + action registry are a **contract with mc-spawn-bot** — change both
  repos together. Tables > prose; contracts/invariants over examples.
