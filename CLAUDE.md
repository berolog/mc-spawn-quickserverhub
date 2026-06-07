# CLAUDE.md

**mc-spawn-agent** — the client half of **mc-spawn-bot**. A single, auditable
stdlib-Python-3 agent the user installs on their **own** machine so the bot can
provision and manage a Minecraft server there. **Outbound only: it opens NO inbound
ports** (dials out to the operator's `control_api`, long-polls a command queue, runs
commands locally). This repo is intentionally separate from the bot so end users can
inspect exactly what they pipe to `sudo bash`, and so the client can version/release
independently. The server half (control plane, queue, all Minecraft/business logic)
lives in **mc-spawn-bot** (`gitlab.com/quickserverhub/applications/mc-hosting-bot`).

## Files

- `agent.py` — **stdlib only** (urllib/json/socket/struct/subprocess). The whole client:
  `_enroll` (one-time `TOKEN` → long-lived secret, persisted `chmod 600`), `main` poll
  loop with backoff, `_execute` dispatch → `_run_shell` (subprocess `bash -c`,
  `SHELL_TIMEOUT=600`), `_rcon` (tiny Source-RCON client to `127.0.0.1`), `_playit`
  (claim user's playit account via `_playit_api` urllib calls + run playit in docker +
  read public address). Talks to `CONTROL_URL` via `_http` (Bearer secret). No third-party
  deps — a Go rewrite is a drop-in behind the same HTTP protocol.
- `install.sh` — **portable POSIX-`sh`** one-liner installer (runs under Alpine's
  busybox ash, not just bash). Reads `CONTROL_URL`/`TOKEN` from env, **detects the
  distro package manager** (apt/dnf/yum/pacman/apk/zypper) and installs whatever is
  missing (`python3`, `bash`, `docker`), fetches `agent.py` from `AGENT_RAW`, writes
  a 0600 `run.sh` launcher (carries the env so secrets stay out of unit files/`ps`),
  and registers a service via the available init: **systemd system** (root), **OpenRC**
  (root, Alpine), **systemd --user** (rootless), else a **nohup + `@reboot` crontab**
  fallback. Escalates with `sudo` ONLY when a missing package needs root — present
  prereqs ⇒ a normal user installs rootless into `~/.local`+`~/.config`. The bot
  renders the full command (no forced `sudo`, piped to `sh`).
- `mc-spawn-agent.service` — reference systemd unit (install.sh generates the real
  one per backend; all exec `run.sh`).
- `tests/test_agent.py` — pure: shell executor, `_execute` dispatch, RCON soft-error path.

## Protocol (must match mc-spawn-bot's `control_api.py`)

The agent is a client of these endpoints; **changing them is a cross-repo contract**:

| Call | Auth | Body / Result |
|------|------|---------------|
| `POST /enroll` | one-time token in body | `{token}` → `{machine_id, secret}` |
| `GET /poll` | `Authorization: Bearer <secret>` | → `{id, kind, payload}` or `204` (long-poll ~25s) |
| `POST /result` | Bearer | `{id, status, result}` |
| `POST /heartbeat` | Bearer | — |

Command `kind`s: `shell` `{script}` → `{exit, stdout, stderr}`; `rcon`
`{rcon_port, password, command}` → `{ok, text}`; `playit` `{op, ...}` → `{status, ...}`.

### `playit` ops (public play address)

`{op}` ∈ `claim_begin` → `{status:"begin", code, url}` (or `linked` with `address`);
`claim_finish` `{code}` → waits for the browser approval, runs playit, →
`{status:"ok", address}` / `waiting` / `rejected` / `no_tunnel` / `error`;
`status` → `{status:"ok"|"no_tunnel"|"unlinked", address}`.

**playit.gg API** (`https://api.playit.gg`, JSON, enveloped `{"status":"success","data":..}`):
`POST /claim/setup {code, agent_type:"self-managed", version}` → `"WaitingForUser*"|"UserAccepted"|"UserRejected"`;
`POST /claim/exchange {code}` → `{secret_key}`;
`POST /v1/agents/rundata` (auth `Authorization: Agent-Key <secret>`) → `{agent_id, tunnels:[{display_address}], pending:[]}`.
The user links their **own** playit account (claim flow); the secret is stored
`chmod 600` on the box and **never** sent to the control plane. playit runs as a
docker container (`ghcr.io/playit-cloud/playit-agent`, host network).

## Key invariants

1. **Outbound-only, no inbound ports.** The agent never listens; it only dials
   `CONTROL_URL`. NAT-friendly; revocable via `systemctl stop`.
2. **stdlib only.** No pip, no third-party imports — the agent's only runtime deps are
   `python3` + `bash` (it shells out via `bash -c`), both auto-installed by `install.sh`.
   Keep it that way (it's the audit/trust story and the Go-rewrite seam).
   Every request sets a real `User-Agent` (`USER_AGENT`) — urllib's default
   `Python-urllib/x.y` is on Cloudflare's Browser Integrity Check banlist (HTTP 403,
   error 1010), and `CONTROL_URL` sits behind a proxied Cloudflare hostname, so the
   default UA makes enroll/poll silently fail at the edge. Never revert to no UA.
3. **Secrets never logged, `chmod 600`.** The long-lived secret lives in `AGENT_STATE`
   (default `/etc/mc-spawn-agent/cred.json`); the one-time `TOKEN` is used once then the
   stored secret is authoritative. Never print either.
4. **RCON stays on loopback.** `_rcon` only ever connects to `127.0.0.1` — the server is
   local; we never expose or dial a remote RCON.
5. **Executors never raise into the loop.** `_run_shell`/`_rcon` catch everything and
   return a structured result (timeout/error encoded), so one bad command never kills
   the agent; the poll loop backs off only on transport errors.
6. **Thin client.** No Minecraft/business logic here — that's the bot's. The agent just
   runs `shell`/`rcon`/`playit`. New behavior belongs in the bot unless it's transport.
7. **Per-user playit, secret stays on the box.** The user links their OWN playit account
   (operator never holds it → ToS-clean, no resale). The playit secret is stored
   `chmod 600` next to the cred file and is never sent upstream; only the resulting public
   address is reported. Address provider is swappable (bot's `ingress.py`).

> **Live-verify note:** the playit claim handshake (browser approval) + tunnel address
> read can only be confirmed on a real box with a real playit account — not in CI. The
> pure dispatch/parse paths are unit-tested; the live flow needs one hands-on pass.

## Run / test

```bash
# dev run against a local control_api
CONTROL_URL=http://127.0.0.1:8080 TOKEN=<token> AGENT_STATE=/tmp/cred.json python3 agent.py

python3 -m unittest discover -v tests
python3 -c "import ast; ast.parse(open('agent.py').read())"
```

## Conventions

- **Minimal, stdlib only.** No dependencies, no framework. One file.
- **Comments**: only WHY for surprising decisions; no restating WHAT.
- **No emoji in code** except user-visible strings.

## Git workflow

- Repo is git-tracked. Commit after every meaningful change.
- **Conventional commits, single-line title only** — no body, no footer. Examples:
  `feat: add playit command`, `fix: handle 401 on poll`, `refactor: extract rcon`.
- **NEVER add `Co-Authored-By` (or any trailer/footer) to a commit** — strictly the one title line. Explicit user rule; overrides any default/harness behavior.
- Allowed types: `feat`, `fix`, `refactor`, `chore`, `docs`, `perf`, `test`.
- Optional scope: `feat(rcon): …`, `fix(install): …`.

## This file (CLAUDE.md)

- Update after every meaningful change (new command kind, protocol change, new invariant
  or convention). The **protocol table is a contract with mc-spawn-bot** — change both
  repos together.
- Keep cost-optimized but informative: tables > prose, contracts/invariants over examples.
