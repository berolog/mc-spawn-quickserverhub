# Audit log

The agent records **every command decision** to an append-only audit log so you can see exactly
what the backend asked for and what the agent allowed or refused.

## Location & viewing

`~/.mc-spawn/logs/audit.log` (under your workspace root). View the recent tail:

```bash
python3 agent.py audit          # last ~50 decisions
tail -f ~/.mc-spawn/logs/audit.log
```

The file is size-capped and reset when it grows too large (best-effort; logging never blocks the
agent).

## Format

```
2026-06-13T12:00:00Z OK      agent.health request_id=01JZ...
2026-06-13T12:01:00Z DENIED  shell request_id=01JZ... reason=unknown_action
2026-06-13T12:02:00Z INVALID minecraft.server.create request_id=01JZ... reason=unknown_field:extra
2026-06-13T12:02:30Z DENIED  minecraft.config.set_memory request_id=01JZ... reason=max_ram_exceeded
2026-06-13T12:03:00Z OK      minecraft.server.start request_id=01JZ...
2026-06-13T12:04:00Z DENIED  minecraft.server.console_exec request_id=01JZ... reason=raw_console_disabled
```

Each line: timestamp, **decision**, action name, `request_id`, and a short reason for non-OK
outcomes.

Decisions:

| Decision | Meaning |
|----------|---------|
| `OK` | Allowed and executed successfully. |
| `DENIED` | Refused (unknown action, policy off, expired/replayed, scope/path violation). |
| `INVALID` | Bad envelope or params (unknown field, wrong type, value out of range). |
| `FAILED` | Allowed, but the capability errored (engine down, server not ready, …). |

## What is NOT logged

Secrets are never written: no agent secret, no RCON/playit passwords, no command "scripts"
(there are none in v2). The audit log is about *decisions*, not payloads — so it's safe to share
when reporting an issue.
