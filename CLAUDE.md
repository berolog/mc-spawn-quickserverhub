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

- `agent.py` — **stdlib only** (urllib/json/socket/struct/subprocess + platform/shutil). The
  whole client: `_enroll` (one-time `TOKEN` → long-lived secret, persisted `chmod 600`), `main`
  poll loop with backoff (re-enrolls once on 401 if a fresh `TOKEN` is present), `_execute`
  dispatch → `_run_shell` (subprocess via `_shell_argv` — `bash -c`, or `cmd /c` on Windows
  with no bash; `SHELL_TIMEOUT=600`), `_rcon` (tiny Source-RCON client to `127.0.0.1`),
  `_playit` (granular per-port ops; tunnels named per local port via `_tunnel_name`). **Cross-
  platform (Phase 6):** `IS_WINDOWS`, `_default_state_path` (Win → `%ProgramData%`), `_playit_net_args`/
  `_playit_local_ip` (host-net+127.0.0.1 on Linux; `host.docker.internal` on Win/mac Docker
  Desktop). **Phase 6.5:** `_runtime()` (engine auto-detect → `$MCSPAWN_RT`), 5xx backoff in the
  poll loop, and `_uninstall`/`_spawn_self_cleanup` (full self-removal). Talks to `CONTROL_URL` via
  `_http`, which stamps `X-MC-Spawn-Protocol` (`PROTOCOL_VERSION`) + `X-MC-Spawn-Platform` on every
  call. No third-party deps — a Go rewrite is a drop-in behind the same versioned HTTP protocol.
- `install.sh` — **portable POSIX-`sh`** one-liner installer (runs under Alpine's
  busybox ash, not just bash). Reads `CONTROL_URL`/`TOKEN` from env, **detects the
  distro package manager** (apt/dnf/yum/pacman/apk/zypper) and installs whatever is
  missing (`python3`, `bash`, a container engine — skips if docker/podman/nerdctl already
  exists), fetches `agent.py` from `AGENT_RAW`, writes a 0600 `run.sh` launcher (carries the
  env so secrets stay out of unit files/`ps`; **re-fetches `agent.py` if deleted** — inv. 13),
  and registers a service via the available init: **systemd system** (root), **OpenRC**
  (root, Alpine), **systemd --user** (rootless), else a **nohup + `@reboot` crontab**
  fallback. Escalates with `sudo` ONLY when a missing package needs root — present
  prereqs ⇒ a normal user installs rootless into `~/.local`+`~/.config`. The bot
  renders the full command (no forced `sudo`, piped to `sh`).
- `install.ps1` — **Windows** PowerShell installer (Phase 6, parallel to `install.sh`). Reads
  `CONTROL_URL`/`TOKEN` from env (`irm … | iex`), best-effort installs Python 3 + Git-for-Windows
  (for `bash`) via **winget**, verifies **Docker Desktop** is present/running (can't auto-install —
  WSL2+reboot; links it), fetches `agent.py`, writes a user-ACL'd `run.cmd` launcher (carries the
  env so the token isn't world-readable — the Win analogue of `chmod 600`), and registers a
  **Scheduled Task** (no NSSM dep): SYSTEM @startup if admin, else per-user @logon; restart-on-fail.
- `mc-spawn-agent.service` — reference systemd unit (install.sh generates the real
  one per backend; all exec `run.sh`).
- `tests/test_agent.py` — pure: shell executor, `_execute` dispatch, RCON soft-error path,
  401 re-enroll, cross-platform (`_shell_argv`/`_default_state_path`/playit-net per OS), protocol
  headers + enroll body.

## Target / roadmap (canonical — see mc-spawn-bot PLAN.md §12–13)

The agent is part of the BYOS goal: run on the user's **own hardware, Linux AND
Windows**. Status:

- **Windows support (PLAN Phase 6) — ✅ DONE.** `install.ps1` (PowerShell, parallel to
  `install.sh`), run as a **Scheduled Task** (no NSSM dep) instead of systemd/OpenRC, verifies
  **Docker Desktop / WSL2**, OS-aware paths/shell/playit-networking. Same outbound-only protocol —
  no inbound ports on the box either OS. **Live-verify on a real Windows box:** the Scheduled-Task
  registration and the playit `host.docker.internal` path are the unproven bits (the playit local
  origin may need an IP, not a hostname → `PLAYIT_LOCAL_IP` override exists).
- **Versioned protocol → drop-in compiled binary.** `agent.py` stays **stdlib-only** so a
  Go/compiled rewrite is drop-in behind the **same HTTP protocol**. The agent stamps
  `PROTOCOL_VERSION` (currently **1**) + platform on every call (header; also enroll body) so
  bot/agent can negotiate; the table below is a **frozen contract** — bump the version and update
  both repos together when it changes.

## Protocol (must match mc-spawn-bot's `control_api.py`)

The agent is a client of these endpoints; **changing them is a cross-repo contract**.
**Every call** also carries `X-MC-Spawn-Protocol: <PROTOCOL_VERSION>` + `X-MC-Spawn-Platform:
<os/arch>` headers (Phase 6); the control plane records them on the machine row
(`platform`, `protocol_version`) and could negotiate on them. Bump `PROTOCOL_VERSION` only on a
breaking wire change and update both repos together.

| Call | Auth | Body / Result |
|------|------|---------------|
| `POST /enroll` | one-time token in body | `{token, protocol_version, platform}` → `{machine_id, secret}` |
| `GET /poll` | `Authorization: Bearer <secret>` | → `{id, kind, payload}` or `204` (long-poll ~25s) |
| `POST /result` | Bearer | `{id, status, result}` |
| `POST /heartbeat` | Bearer | — |

Command `kind`s: `shell` `{script}` → `{exit, stdout, stderr}` (the engine is exposed to
the script as `$MCSPAWN_RT`, see inv. 12); `rcon` `{rcon_port, password, command}` →
`{ok, text}`; `playit` `{op, local_port, ...}` → `{status, ...}`; `uninstall` `{containers}`
→ `{status:"ok"}` then the agent self-removes and exits (inv. 13).

### `playit` ops (public play address)

Each hosted server has its **own** tunnel keyed by `{local_port}` (named
`mc-spawn-<local_port>`), so several servers on one box don't collide on a shared
address; the agent **auto-creates** it (the user never touches playit's English
dashboard). The ops are deliberately **small and quick** — the bot loops `claim_poll`/
`status` itself and shows live progress between calls, instead of one op blocking for
minutes. `{op}` ∈
- `claim_begin` `{local_port}` → `{status:"begin", code, url}` (mint claim code) or `{status:"linked"}` (already linked).
- `claim_poll` `{code}` → one quick approval check; on accept it exchanges + saves the secret → `{status:"accepted"|"rejected"}` or `{status:"waiting", stage:"visit"|"approve"}` (`visit` = link not opened yet; `approve` = opened, the Add/Next/Allow click still pending — the bot shows the matching hint).
- `playit_start` `{local_port}` → just ensure the playit **container** is running (may pull the image first run) → `{status:"ok"|"unlinked"|"error"}`. Does NOT touch tunnels.
- `ensure_tunnel` `{local_port}` → ONE create-or-detect attempt for this port's tunnel (the bot loops it): `{status:"created"|"exists"(+address?)|"connecting"|"error"|"unlinked"}`. Idempotent — never creates if the port already has a tunnel (in `tunnels` or `pending`). `connecting` = not ready yet (playit unreachable / no `agent_id` / transient `AgentVersionTooOld` while the container connects) → the bot retries; so the first create failing is normal and self-heals (no manual "try again").
- `status` `{local_port}` → **READ-ONLY** read of this port's address (never creates) → `{status:"ok", address}` / `no_tunnel` / `unlinked`.
- `remove_tunnel` `{local_port}` → delete just this port's tunnel → `{status:"ok"}` (one of several servers deleted; playit keeps running for the rest).
- `teardown` `{}` → delete ALL our tunnels, stop+remove the playit container, drop the secret → `{status:"ok"}` (last server / whole machine deleted). Both delete ops are best-effort, idempotent, and touch only tunnels we created.

**playit.gg API** (`https://api.playit.gg`, JSON, enveloped `{"status":"success","data":..}`,
verified against playit-agent v1.0.9):
`POST /claim/setup {code, agent_type:"self-managed", version}` → `data` ∈ `"WaitingForUserVisit"`
(link not opened), `"WaitingForUser"` (opened, awaiting the approve/Next click — **claim is TWO
site steps, not one**), `"UserAccepted"`, `"UserRejected"`. **`version` MUST be `"playit <semver>"`**
(the real client sends `format!("playit {}", CARGO_PKG_VERSION)` → e.g. `"playit 1.0.9"`); a
bad/old value (e.g. `"mc-spawn-agent 1"`) is stored as the agent's version and later makes
`POST /tunnels/create` fail with **`AgentVersionTooOld`**. We send `PLAYIT_VERSION` (env-overridable,
default `"playit 1.0.9"` — bump as playit's minimum rises);
`POST /claim/exchange {code}` → `{secret_key}` (after UserAccepted);
`POST /v1/agents/rundata` (auth `Authorization: Agent-Key <secret>`) → `{agent_id, tunnels:[{display_address}], pending:[]}`;
`POST /tunnels/create` (same Agent-Key auth) `{name, tunnel_type:"minecraft-java", port_type:"tcp", port_count:1, origin:{type:"agent", data:{agent_id, local_ip:<_playit_local_ip()>, local_port}}, enabled:true, alloc:null, firewall_id:null, proxy_protocol:null}` → `{id}` (errors are bare enum strings, e.g. `"RequiresVerifiedAccount"`). `local_ip` is `127.0.0.1` on Linux (playit runs `--network host`) and `host.docker.internal` on Win/mac Docker Desktop (override `PLAYIT_LOCAL_IP`). Wire format verified against playit-agent's `api_client` crate.
`POST /tunnels/delete` (same Agent-Key auth) `{tunnel_id}` → used by `teardown`; the agent deletes only tunnels named `mc-spawn` (the ones it created), never the user's own.
The user links their **own** playit account (claim flow); the secret is stored
`chmod 600` on the box and **never** sent to the control plane. playit runs as a
docker container (`ghcr.io/playit-cloud/playit-agent`) — **host network on Linux**, or
bridge + `--add-host host.docker.internal:host-gateway` on Win/mac (`_playit_net_args`).

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
   stored secret is authoritative. Never print either. A `401` on `/poll` means the stored
   secret is no longer recognised (e.g. the control plane's DB was reset). `main` then makes
   **one** re-enroll attempt: `_enroll` returns the new secret only if a fresh, unused `TOKEN`
   is present (the token is single-use, consumed server-side), in which case we adopt it and
   carry on; otherwise we `exit(1)` so the operator re-pairs — never crash-loop a poll the
   secret can't satisfy. `_enroll` returns `None` (not `sys.exit`) so callers own that policy.
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
8. **Per-port tunnels; create is retried but never duplicated.** Each server's tunnel is
   named `mc-spawn-<local_port>` and routed to `127.0.0.1:<local_port>`, so multiple servers
   on one box each get their own address (no shared-tunnel collision). **Only `ensure_tunnel`
   creates**, and only when the port has no tunnel yet (dedup matches by name in both
   `tunnels` and `pending` — rundata `AgentTunnelV1`/`...PendingV1` both carry `name`). The
   bot loops `ensure_tunnel` until `created`/`exists` (the first `POST /tunnels/create` often
   fails `AgentVersionTooOld`/`connecting` while the container connects — that's retryable, not
   fatal), then switches to read-only `status` for the address — so the create is robust AND
   can't spawn duplicates. `_playit_run` never raises (docker may be absent ⇒ returns False).
9. **Cleanup of tunnels, but the agent record can't be API-deleted.** `remove_tunnel
   {local_port}` deletes one server's tunnel; `teardown` deletes ALL of ours + stops the
   container + drops the secret. Both filter to tunnels WE created (name `mc-spawn` or
   `mc-spawn-*`) — a user's hand-made tunnels are never touched — best-effort + idempotent.
   **playit has NO API to delete the agent registration itself** (the agent api_client has
   no `/agents/delete`; it's a dashboard/account-session action, and we only hold the agent
   *secret*). So after teardown the (now offline, tunnel-less) agent entry remains in the
   user's playit account — harmless; the bot tells the user they can remove it manually.
10. **Cross-platform (Linux + Windows), one codebase.** OS differences are isolated to small
    helpers — `_default_state_path` (paths), `_shell_argv` (shell: `bash -c`, or `cmd /c` on
    Windows lacking bash; the bot's scripts are POSIX so **bash is required for hosting** on
    Windows — `install.ps1` pulls Git-for-Windows), `_playit_net_args`/`_playit_local_ip` (Linux
    host-net + `127.0.0.1`; Win/mac Docker-Desktop bridge + `host.docker.internal`). **MC hosting
    + RCON are already OS-agnostic** because provisioning publishes ports (`-p`), which Docker
    Desktop maps to the host's localhost. Don't sprinkle `if IS_WINDOWS` through logic — add a
    helper. `install.sh` (POSIX) and `install.ps1` (Scheduled Task) are the two installers.
11. **Versioned protocol for a drop-in binary.** Every `_http` call stamps `X-MC-Spawn-Protocol`
    (`PROTOCOL_VERSION`, currently 1) + `X-MC-Spawn-Platform`; enroll also sends them in the body.
    The control plane stores `platform`/`protocol_version` on the machine row. This freezes the
    contract so a future Go/compiled agent is a drop-in and the bot can branch on platform if ever
    needed. **Bump `PROTOCOL_VERSION` only on a breaking wire change, and update both repos together.**
12. **Engine-agnostic; idempotent; auto-reconnect (Phase 6.5).** `_runtime()` auto-detects the
    container engine (`docker → podman → nerdctl`, `CONTAINER_RUNTIME` overrides) and is exported to
    every bot script as `$MCSPAWN_RT` (so the bot's `provisioner.py` commands stay engine-agnostic);
    the agent's own playit commands call `_runtime()` directly. The poll loop now **backs off on 5xx/
    unexpected** statuses too (not just network errors), so a control-plane restart or CF blip is
    ridden out instead of hammered. **The agent stays a dumb executor** — it does NOT decide what
    should run; the bot's reconciler pushes `start`/recreate/`playit_start` to close drift, and the
    recreate reattaches the `<container>_data` volume so the world survives a manual `docker rm`.
13. **Full self-uninstall on machine delete.** The `uninstall` `{containers}` command purges the
    listed MC containers + their `_data` volumes and tears down playit **synchronously**, then spawns
    a **detached** cleanup (systemd-run / new-session `sh`, or PowerShell on Windows) that removes the
    service/Scheduled-Task + the agent's own files a few seconds later — surviving the agent's death
    when its service is stopped (and the agent.py file-lock on Windows). The agent reports the result
    then `sys.exit(0)` so the (being-removed) service doesn't relaunch it. Best-effort + idempotent.
    The launchers (`run.sh`/`run.cmd`) **re-fetch `agent.py` from `AGENT_RAW` if it was deleted**, so
    a manually-removed binary self-heals on the next service restart. **Live-verify:** service removal
    + Windows Scheduled-Task delete need a real box (the cgroup/file-lock timing is the unproven bit).

> **Live-verify note:** the playit claim handshake + tunnel create/delete/address read can
> only be fully confirmed on a real box with a real playit account — not in CI. Verified
> against playit-agent **v1.0.9** source: claim is **two site steps** (`WaitingForUserVisit`
> → `WaitingForUser` → `UserAccepted`) and the `/claim/setup` `version` must be `"playit
> <semver>"` or `/tunnels/create` returns `AgentVersionTooOld` (we send `PLAYIT_VERSION`,
> default `"playit 1.0.9"`, and treat a transient `AgentVersionTooOld` as retryable). The
> pure dispatch/parse paths are unit-tested; the end-to-end browser flow needs one hands-on pass.

> **Windows live-verify note (Phase 6):** the OS-aware helpers are unit-tested (by patching
> `IS_WINDOWS`), but a real Windows box must confirm: (1) `install.ps1` winget installs + the
> Scheduled-Task registration/restart behaviour; (2) the agent reaching `docker.exe` and running
> the bot's POSIX scripts via Git-Bash; (3) playit forwarding to `host.docker.internal:<mc_port>` —
> if playit's `local_ip` rejects a hostname, set `PLAYIT_LOCAL_IP` to the Docker-Desktop host IP.

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
