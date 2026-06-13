# Protocol v2 (command contract)

The bot (backend) and agent communicate over the control plane's existing HTTP queue
(`/enroll` · `/poll` · `/result` · `/heartbeat`). **v2** changes only the *payload*: instead
of a shell script, the backend sends a typed **command envelope**, and the agent returns a
**result envelope**. The queue itself is a dumb pass-through — it does not interpret either.

`PROTOCOL_VERSION = 2`. Every HTTP call still carries `X-MC-Spawn-Protocol` + `X-MC-Spawn-Platform`.

## Command envelope (backend → agent)

`GET /poll` returns `{"id": <queue id>, "kind": "action", "payload": <envelope>}` where the
envelope is:

```json
{
  "protocol_version": 2,
  "request_id": "01JZ...",
  "action": "minecraft.server.start",
  "params": { "server_id": "7" },
  "issued_at": "2026-06-13T12:00:00Z",
  "expires_at": "2026-06-13T12:02:00Z"
}
```

Validation, in order (any failure stops here):

1. `protocol_version == 2` (else `invalid`).
2. `request_id`, `action` are strings and `params` is an object (else `invalid`).
3. `issued_at`/`expires_at` parse; not expired; not too far in the future (±120 s skew) — else
   `denied`.
4. `request_id` not seen recently (replay window) — a duplicate returns the **previous result**.
5. `action` is in the hardcoded registry (else `denied: unknown_action`).
6. `action` is in the local policy's `allowed_actions` (else `denied: action_disabled_by_policy`).
   `agent.health` / `agent.capabilities` are always allowed.
7. `params` matches the action's exact schema — no unknown fields (else `invalid`).
8. The capability runs; per-action policy flags / limits may still `deny`.

## Result envelope (agent → backend)

Posted to `/result` as `{"id": <queue id>, "status": "done", "result": <envelope>}`. The inner
envelope:

```json
{
  "request_id": "01JZ...",
  "status": "ok",
  "action": "minecraft.server.start",
  "result": { "server_id": "7", "state": "running" },
  "agent_policy_version": 1
}
```

`status` ∈ `ok` · `denied` · `invalid` · `failed` · `pending_local_approval`. The queue row is
always marked `done` so the backend receives the envelope and reads `result.status`. Only safe,
**bounded** structured data and logs are returned — never raw stdout from arbitrary commands.

## Actions

`server_id` matches `^[a-zA-Z0-9_-]{1,32}$`; the agent derives the container name
(`mcspawn-server-<server_id>`) and volume (`<container>_data`) from it. `player` matches
`^[A-Za-z0-9_]{3,16}$`.

| Action | params | result (on ok) |
|--------|--------|----------------|
| `agent.health` | — | `{status, agent_version, platform, engine}` |
| `agent.capabilities` | — | `{protocol_version, agent_version, policy_version, allowed_actions[], limits}` |
| `agent.uninstall` | — | `{status:"ok", removed[]}` then the agent self-removes |
| `minecraft.server.create` | `server_id, kind, version, ram_mb, port, rcon_port, rcon_password` | `{server_id, container, state}` |
| `minecraft.server.start` / `stop` / `restart` | `server_id` | `{server_id, state}` |
| `minecraft.server.status` | `server_id` | `{server_id, status}` |
| `minecraft.server.logs` | `server_id`, `lines?` (1–500) | `{server_id, logs}` (bounded) |
| `minecraft.server.delete` | `server_id` | `{server_id, deleted}` — policy `allow_server_delete` |
| `minecraft.server.reconcile_status` | `server_ids[]` | `{engine: up\|down, servers:{id:status}, playit}` |
| `minecraft.server.say` | `server_id, message` | `{server_id, text}` |
| `minecraft.server.save_all` | `server_id` | `{server_id, text}` |
| `minecraft.server.console_tail` | `server_id`, `lines?` | `{server_id, logs}` (read-only) |
| `minecraft.server.console_exec` | `server_id, command` | `{server_id, text}` — policy `allow_raw_rcon` (off) |
| `minecraft.config.set_difficulty` | `server_id, difficulty` | `{server_id, text}` |
| `minecraft.config.set_gamemode` | `server_id, gamemode` | `{server_id, text}` |
| `minecraft.player.list` | `server_id` | `{server_id, ok, text}` |
| `minecraft.player.whitelist_add` / `_remove` | `server_id, player` | `{server_id, player, text}` |
| `minecraft.player.whitelist_list` | `server_id` | `{server_id, text}` |
| `minecraft.player.kick` | `server_id, player` | `{server_id, player, text}` |
| `minecraft.backup.create` / `list` / `delete` | `server_id` (+`backup_id`) | backup metadata |
| `minecraft.backup.restore` | `server_id, backup_id` | `{restored}` — policy `allow_backup_restore` (off) |
| `playit.claim_begin` / `claim_poll` / `playit_start` / `ensure_tunnel` / `status` / `remove_tunnel` / `teardown` | per op (`local_port`/`code`) | playit op `{status, ...}` |

Forbidden in production (no such action exists): `shell`, `exec`, `run`, `script`,
`minecraft.rcon.raw`, `agent.update_from_url`, arbitrary `file.read`/`file.write`,
`docker.raw`, `wsl.raw`. A request for any of these returns `denied: unknown_action`.

## Semantic RCON, not raw

Player/server semantic actions are executed as `docker exec <container> rcon-cli <fixed args>`
**inside** the container — the agent never handles the RCON password and the args are a fixed
argv, so there is no shell and no injection.
