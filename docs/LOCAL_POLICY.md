# Local policy (`policy.json`)

The policy file is **yours**. The agent reads it and enforces it locally; the backend can never
change it and there is no action to modify it remotely. It is the second gate after schema
validation: even an allowed, well-formed command is denied if your policy says so.

## Location

- Linux (root install): `/etc/mc-spawn-agent/policy.json`
- Linux (rootless): `~/.config/mc-spawn-agent/policy.json`
- Windows: `%LOCALAPPDATA%\mc-spawn-agent\policy.json`
- Override with the `AGENT_POLICY` environment variable.

The installer writes a conservative default if none exists. If the file is missing or invalid,
the agent falls back to **built-in conservative defaults** (it never fails open to a looser
policy). After editing, restart the agent service.

## Fields

```json
{
  "policy_version": 1,
  "workspace_root": "~/.mc-spawn",
  "allowed_actions": ["agent.health", "minecraft.server.start", "..."],
  "max_ram_mb": 8192,
  "allowed_port_range": [25565, 25700],
  "allow_server_delete": true,
  "allow_agent_uninstall": true,
  "allow_raw_rcon": false,
  "allow_backup_restore": false,
  "allow_plugins": false,
  "allow_mods": false,
  "allow_agent_auto_update": false
}
```

| Field | Meaning |
|-------|---------|
| `workspace_root` | The single directory all mutable files (worlds, backups, logs, tmp) live under. Nothing outside it is touched. |
| `allowed_actions` | The coarse allowlist. An action not listed is **denied** even if implemented. (`agent.health`/`agent.capabilities` are always allowed.) |
| `max_ram_mb` | Upper bound on a server's RAM. A `create` above it is denied. |
| `allowed_port_range` | `[min, max]` inclusive. Both the Minecraft port and the RCON port must fall inside it. |
| `allow_server_delete` | If false, `minecraft.server.delete` is denied. (Default true; the agent only ever deletes resources it created, and the bot shows what will be removed.) |
| `allow_agent_uninstall` | If false, `agent.uninstall` is denied. |
| `allow_raw_rcon` | If false (default), `minecraft.server.console_exec` (free-form console) is denied. Turn on only if you want a live console — it lets the backend relay arbitrary console commands. |
| `allow_backup_restore` | If false (default), restoring a backup over the current world is denied. |
| `allow_plugins` / `allow_mods` | Reserved; off by default. |
| `allow_agent_auto_update` | Reserved; off by default. The agent does not auto-update from backend instruction regardless. |

## Tightening examples

- **Monitoring only, no hosting:** set `allowed_actions` to just
  `["agent.health", "agent.capabilities"]`.
- **Lock to one server port:** `"allowed_port_range": [25565, 25565]`.
- **Forbid deletes from the bot entirely:** `"allow_server_delete": false`,
  `"allow_agent_uninstall": false`.
- **Enable the live WebApp console:** `"allow_raw_rcon": true` (understand the trade-off in
  [THREAT_MODEL.md](../THREAT_MODEL.md)).

## Inspect what's in effect

```bash
python3 agent.py policy          # the loaded policy (defaults + your overrides)
python3 agent.py capabilities    # what the agent currently permits, as the bot sees it
```
