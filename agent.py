#!/usr/bin/env python3
"""mc-spawn agent — runs on the user's own machine. STDLIB ONLY (python3).

SECURITY MODEL (protocol v2): the backend (Telegram bot / control plane) is treated as
UNTRUSTED. It can only request documented Minecraft operations by name (`action` + typed
`params`); it can NOT send code to run. This agent is the security boundary: every command
is parsed, schema-validated, checked against a LOCAL policy file the machine owner controls,
and only then mapped to a HARDCODED capability that runs a fixed `docker`/`wsl` argv — never
a shell. Anything outside the public protocol or local policy is denied. See SECURITY.md /
THREAT_MODEL.md / docs/PROTOCOL_V2.md.

Outbound only: it dials the control API (no inbound ports on this box), redeems a one-time
enroll token for a long-lived secret, then long-polls for v2 commands and executes the
allowed capability locally. The container engine (docker/podman/nerdctl) is set up ONCE by
the installer; the agent never installs packages or runs arbitrary OS commands at runtime.

Config via env: CONTROL_URL (required, HTTPS — http only with MCSPAWN_DEV=1), TOKEN
(one-time, first run only), AGENT_STATE (cred file). Local policy + workspace + audit log
live under the workspace root (default ~/.mc-spawn) and a policy file the owner edits:
  Linux:   /etc/mc-spawn-agent/policy.json (root) or ~/.config/mc-spawn-agent/policy.json
  Windows: %LOCALAPPDATA%\\mc-spawn-agent\\policy.json

Cross-platform (Linux + Windows): on Linux the agent runs the engine directly; on Windows
hosting runs inside a dedicated WSL2 Linux distro (`mc-spawn`) — `run_allowed` routes every
engine command through `wsl -d <distro> -- <exe> <argv...>` (argv array, never a shell).

CLI:  python3 agent.py [audit|policy|capabilities|wipe-creds|approve <id>]
Single file, stdlib only — a Go rewrite stays a drop-in behind the same versioned protocol.
"""
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

IS_WINDOWS = os.name == "nt"

# Windows hosting runs inside a dedicated WSL2 Linux distro; the agent shells engine commands
# into it via `wsl -d <distro> -- <exe> ...` (argv array). Override with MCSPAWN_WSL_DISTRO.
WSL_DISTRO = os.environ.get("MCSPAWN_WSL_DISTRO", "mc-spawn").strip() or "mc-spawn"

# Versioned control-plane contract. v2 = the capability-based, deny-by-default protocol
# (action + params envelope). Bump ONLY on a breaking wire change; update both repos together.
PROTOCOL_VERSION = 2
AGENT_VERSION = "2.0.0"
PLATFORM = f"{platform.system().lower()}/{(platform.machine() or '').lower()}"

# Local-dev escape hatch: allow a non-HTTPS CONTROL_URL only when explicitly set. NEVER in a
# release/installer-driven run — production talks HTTPS to the control plane only (spec §17).
DEV_MODE = os.environ.get("MCSPAWN_DEV", "").strip().lower() not in ("", "0", "false", "no")


def _default_state_path():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("ProgramData") or os.getcwd()
        return os.path.join(base, "mc-spawn-agent", "cred.json")
    return "/etc/mc-spawn-agent/cred.json"


CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "").strip()
STATE_PATH = os.environ.get("AGENT_STATE", _default_state_path())

POLL_TIMEOUT = 40        # > server long-poll (25s)
ENGINE_TIMEOUT = 600     # container ops (image pull on first create) can be slow
USER_AGENT = f"mc-spawn-agent/{AGENT_VERSION}"
# Accept commands at most this far out of sync with the issuer's clock (spec §17).
CLOCK_SKEW_SEC = 120
# Remember recently-seen request_ids this long to drop replays / return the prior result.
REPLAY_WINDOW_SEC = 600

DEBUG = os.environ.get("MCSPAWN_DEBUG", "").strip().lower() not in ("", "0", "false", "no")
LOG_PATH = os.environ.get("AGENT_LOG") or os.path.join(
    os.path.dirname(STATE_PATH) or ".", "agent.log")
_LOG_CAP = 1_000_000
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _quote_log_value(value):
    return json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))


def _log_line(level, event, fields):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    parts = [f"ts={_quote_log_value(ts)}", f"level={_quote_log_value(level)}",
             f"event={_quote_log_value(event)}"]
    parts.extend(f"{k}={_quote_log_value(v)}" for k, v in fields.items() if v is not None)
    return " ".join(parts)


def _log_to_file(path, line):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            if os.path.getsize(path) > _LOG_CAP:
                open(path, "w").close()
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    except Exception:  # noqa: BLE001 — logging must never break the agent
        pass


def _log(event, level="info", **fields):
    line = _log_line(level, event, fields)
    print(line, flush=True)
    _log_to_file(LOG_PATH, line)


def _debug(event, **fields):
    if DEBUG:
        _log(event, level="debug", **fields)


# ---- local policy (owner-controlled; the backend can NEVER change it) ----
#
# Deny-by-default. Normal Minecraft operations are allowed out of the box so "сервер в пару
# кликов" works; the genuinely dangerous/raw capabilities are off until the owner opts in by
# editing policy.json. The agent reads this file but offers NO action to modify it (spec §7).

POLICY_PATH = os.environ.get("AGENT_POLICY") or os.path.join(
    os.path.dirname(STATE_PATH) or ".", "policy.json")

# Actions allowed by default (the coarse allowlist). Unknown actions are always denied; the
# dangerous ones below are ALSO gated by a per-action flag (allow_*), so listing them here is
# necessary but not sufficient.
_DEFAULT_ALLOWED_ACTIONS = [
    "agent.health", "agent.capabilities", "agent.uninstall",
    "minecraft.server.create", "minecraft.server.start", "minecraft.server.stop",
    "minecraft.server.restart", "minecraft.server.status", "minecraft.server.logs",
    "minecraft.server.delete", "minecraft.server.reconcile_status",
    "minecraft.server.say", "minecraft.server.save_all",
    "minecraft.server.console_tail", "minecraft.server.console_exec",
    "minecraft.config.set_difficulty", "minecraft.config.set_gamemode",
    "minecraft.player.list", "minecraft.player.whitelist_add",
    "minecraft.player.whitelist_remove", "minecraft.player.whitelist_list",
    "minecraft.player.kick",
    "minecraft.backup.create", "minecraft.backup.list",
    "minecraft.backup.delete", "minecraft.backup.restore",
    "playit.claim_begin", "playit.claim_poll", "playit.playit_start",
    "playit.ensure_tunnel", "playit.status", "playit.remove_tunnel", "playit.teardown",
]

_DEFAULT_POLICY = {
    "policy_version": 1,
    "workspace_root": os.path.expanduser(os.path.join("~", ".mc-spawn")),
    "allowed_actions": _DEFAULT_ALLOWED_ACTIONS,
    "max_ram_mb": 8192,
    # The bot allocates one mc + one rcon port per server (max+1 from the itzg defaults), so we
    # bound them to an inclusive range rather than an explicit list. Both mc and rcon ports must
    # fall inside it. Narrow this to lock the agent to specific ports.
    "allowed_port_range": [25565, 25700],
    # Deletion of agent-created resources is allowed (scoped + the bot confirms exactly what is
    # removed), but a cautious owner can switch these off.
    "allow_server_delete": True,
    "allow_agent_uninstall": True,
    # Off by default — opt in to enable. The raw console is the WebApp live-console hook: with
    # it OFF only safe semantic actions reach the server (the untrusted backend cannot send
    # arbitrary console commands); the owner turns it on to drive a live console.
    "allow_raw_rcon": False,
    "allow_backup_restore": False,
    "allow_plugins": False,
    "allow_mods": False,
    "allow_agent_auto_update": False,
}

# Read-only actions that always work regardless of allowed_actions, so the backend can discover
# what this agent permits and the owner can monitor it.
_ALWAYS_ALLOWED = {"agent.health", "agent.capabilities"}

_POLICY_CACHE = None


def _load_policy():
    """Load policy.json over the conservative defaults (unknown keys ignored). Cached; a bad
    file falls back to defaults with a warning — we never fail OPEN to a looser policy."""
    global _POLICY_CACHE
    if _POLICY_CACHE is not None:
        return _POLICY_CACHE
    policy = dict(_DEFAULT_POLICY)
    try:
        with open(POLICY_PATH) as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            for k, v in disk.items():
                if k in _DEFAULT_POLICY:
                    policy[k] = v
        else:
            _log("policy_invalid", level="warning", path=POLICY_PATH)
    except FileNotFoundError:
        _debug("policy_missing", path=POLICY_PATH)
    except (OSError, ValueError) as e:
        _log("policy_load_failed", level="warning", path=POLICY_PATH, error=type(e).__name__)
    _POLICY_CACHE = policy
    return policy


def _policy(key):
    return _load_policy().get(key, _DEFAULT_POLICY.get(key))


def _workspace_root():
    return os.path.abspath(_policy("workspace_root") or _DEFAULT_POLICY["workspace_root"])


# ---- audit log (decisions only; never secrets, never scripts — scripts no longer exist) ----

def _audit_path():
    return os.path.join(_workspace_root(), "logs", "audit.log")


def _audit(decision, action, request_id, reason=""):
    _log_to_file(_audit_path(), _log_line(
        "info", "audit", {
            "decision": decision,
            "action": action,
            "request_id": request_id,
            "reason": reason or None,
        }))


# ---- errors raised by capability handlers (mapped to result statuses by the dispatcher) ----

class SecurityError(Exception):
    """A path/scope violation — treated as a denial."""


class PolicyDenied(Exception):
    """Local policy forbids this action right now."""


class CapabilityError(Exception):
    """The allowed capability ran but failed (engine error, server not ready, ...)."""


# ---- workspace path jail (spec §8) ----

def safe_join(root, *parts):
    """Join under `root` and refuse anything that escapes it (.. / absolute / symlink / drive
    / UNC). Resolves symlinks so a link inside the workspace can't point outside."""
    root = os.path.realpath(str(root))
    candidate = os.path.realpath(os.path.join(root, *[str(p) for p in parts]))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise SecurityError("path escapes workspace")
    return candidate


def _ensure_workspace():
    root = _workspace_root()
    for sub in ("", "backups", "logs", "tmp"):
        try:
            os.makedirs(os.path.join(root, sub) if sub else root, exist_ok=True)
        except OSError:
            pass
    return root


# ---- container engine + safe subprocess wrapper (argv only, NEVER shell) ----

_RUNTIME_CACHE = None


def _runtime():
    """The container engine to use. `CONTAINER_RUNTIME` overrides; cached. Windows = `docker`
    inside the WSL distro. Linux auto-detects docker → podman → nerdctl on PATH."""
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None:
        return _RUNTIME_CACHE
    override = os.environ.get("CONTAINER_RUNTIME", "").strip()
    if override and override in _RUNTIMES:
        _RUNTIME_CACHE = override
    elif IS_WINDOWS:
        _RUNTIME_CACHE = "docker"
    else:
        _RUNTIME_CACHE = next((rt for rt in _RUNTIMES if _find_executable(rt)), "docker")
    return _RUNTIME_CACHE


_RUNTIMES = ("docker", "podman", "nerdctl")


def _find_executable(name):
    found = shutil.which(name)
    if found:
        return found
    for directory in DEFAULT_PATH.split(":"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def minimal_env(rt):
    """A minimal, predictable environment for engine subprocesses (spec §9) — no inherited
    user env leaks into containers. Carry only what docker/wsl actually need to function."""
    keep = ("PATH", "HOME", "SystemRoot", "windir", "USERPROFILE", "LOCALAPPDATA",
            "APPDATA", "TEMP", "TMP", "DOCKER_HOST", "XDG_RUNTIME_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = env.get("PATH") or DEFAULT_PATH
    env["MCSPAWN_RT"] = rt
    return env


_ENGINE_READY = False


def _ensure_engine_ready():
    """On Windows the engine lives in the WSL distro; start dockerd once per process and wait
    for its socket (argv only — no shell). No-op on Linux (systemd runs docker)."""
    global _ENGINE_READY
    if _ENGINE_READY or not IS_WINDOWS:
        _ENGINE_READY = True
        return
    _ENGINE_READY = True
    _run_internal(["wsl", "-d", WSL_DISTRO, "--", "service", "docker", "start"], timeout=60)
    for _ in range(40):
        r = _run_internal(["wsl", "-d", WSL_DISTRO, "--", "docker", "info"], timeout=20)
        if r is not None and r.returncode == 0:
            _debug("wsl_docker_ready", distro=WSL_DISTRO)
            return
        time.sleep(1)
    _log("wsl_docker_not_ready", level="warning", distro=WSL_DISTRO)


def _run_internal(argv, timeout=60):
    """Run an AGENT-CONSTANT argv (engine bootstrap, self-uninstall) — never a backend string,
    never a shell. Returns CompletedProcess or None on failure. Used only for agent self-
    maintenance; capability handlers use run_allowed."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as e:  # noqa: BLE001
        _debug("internal_command_failed", command=" ".join(argv[:2]), error=type(e).__name__)
        return None


def run_allowed(rt, args, timeout=ENGINE_TIMEOUT):
    """Run a container-engine command as a fixed argv (spec §9): engine from the allowlist,
    args are strings only, never through a shell, minimal env, cwd inside the workspace,
    bounded. On Windows the engine runs inside the WSL distro: `wsl -d <distro> -- <rt> ...`."""
    if rt not in _RUNTIMES:
        raise SecurityError(f"engine '{rt}' not allowed")
    if any(not isinstance(a, str) for a in args):
        raise SecurityError("invalid argv (non-string element)")
    root = _ensure_workspace()
    if IS_WINDOWS:
        argv = ["wsl", "-d", WSL_DISTRO, "--", rt, *args]
    else:
        exe = _find_executable(rt)
        if not exe:
            raise CapabilityError(f"container_engine_unavailable:{rt}")
        argv = [exe, *args]
    try:
        return subprocess.run(
            argv, cwd=root, env=minimal_env(rt), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise CapabilityError("engine command timed out")
    except FileNotFoundError:
        raise CapabilityError(f"container_engine_unavailable:{rt}")


def _bounded(text, limit=4000):
    text = text or ""
    return text[-limit:]


# ---- HTTP transport to the control plane (unchanged wire shape; v2 rides in the body) ----

def _http(method, path, body=None, secret=None, timeout=POLL_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CONTROL_URL + path, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("X-MC-Spawn-Protocol", str(PROTOCOL_VERSION))
    req.add_header("X-MC-Spawn-Platform", PLATFORM)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


# ---- credential persistence ----

def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)
    os.chmod(STATE_PATH, 0o600)


def _enroll():
    if not TOKEN:
        _log("enroll_missing_token", level="error")
        return None
    status, data = _http(
        "POST", "/enroll",
        {"token": TOKEN, "protocol_version": PROTOCOL_VERSION, "platform": PLATFORM},
        timeout=20,
    )
    if status != 200 or not data:
        _log("enroll_failed", level="error", http_status=status)
        return None
    _save_state({"machine_id": data["machine_id"], "secret": data["secret"]})
    _log("enroll_ok", machine_id=data["machine_id"])
    return data["secret"]


# ---- schema validation (spec §6): exact, deny unknown fields ----

SERVER_ID_RE = r"^[a-zA-Z0-9_-]{1,32}$"
PLAYER_NAME_RE = r"^[A-Za-z0-9_]{3,16}$"
VERSION_RE = r"^[A-Za-z0-9._-]{1,16}$"
MESSAGE_RE = r"^[^\x00-\x1f]{1,256}$"        # printable, no control chars
BACKUP_ID_RE = r"^[a-zA-Z0-9_-]{1,48}$"
TYPES = ("VANILLA", "PAPER", "FABRIC", "FORGE")
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
GAMEMODES = ("survival", "creative", "adventure", "spectator")

_SERVER_ID = {"type": "string", "regex": SERVER_ID_RE}
_PORT = {"type": "integer", "min": 1, "max": 65535}

SCHEMAS = {
    "agent.health": {"required": {}, "optional": {}},
    "agent.capabilities": {"required": {}, "optional": {}},
    "agent.uninstall": {"required": {}, "optional": {}},

    "minecraft.server.create": {"required": {
        "server_id": _SERVER_ID,
        "kind": {"type": "string", "enum": TYPES},
        "version": {"type": "string", "regex": VERSION_RE},
        "ram_mb": {"type": "integer", "min": 512, "max": 16384},
        "port": _PORT,
        "rcon_port": _PORT,
        "rcon_password": {"type": "string", "regex": r"^[A-Za-z0-9_\-]{8,128}$"},
    }},
    "minecraft.server.start": {"required": {"server_id": _SERVER_ID}},
    "minecraft.server.stop": {"required": {"server_id": _SERVER_ID}},
    "minecraft.server.restart": {"required": {"server_id": _SERVER_ID}},
    "minecraft.server.status": {"required": {"server_id": _SERVER_ID}},
    "minecraft.server.delete": {"required": {"server_id": _SERVER_ID}},
    "minecraft.server.logs": {
        "required": {"server_id": _SERVER_ID},
        "optional": {"lines": {"type": "integer", "min": 1, "max": 500}},
    },
    "minecraft.server.reconcile_status": {"required": {
        "server_ids": {"type": "array", "items": {"type": "string", "regex": SERVER_ID_RE},
                       "max_items": 64},
    }},

    "minecraft.server.say": {"required": {
        "server_id": _SERVER_ID, "message": {"type": "string", "regex": MESSAGE_RE}}},
    "minecraft.server.save_all": {"required": {"server_id": _SERVER_ID}},
    "minecraft.server.console_tail": {
        "required": {"server_id": _SERVER_ID},
        "optional": {"lines": {"type": "integer", "min": 1, "max": 500}}},
    "minecraft.server.console_exec": {"required": {
        "server_id": _SERVER_ID, "command": {"type": "string", "regex": MESSAGE_RE}}},

    "minecraft.config.set_difficulty": {"required": {
        "server_id": _SERVER_ID, "difficulty": {"type": "string", "enum": DIFFICULTIES}}},
    "minecraft.config.set_gamemode": {"required": {
        "server_id": _SERVER_ID, "gamemode": {"type": "string", "enum": GAMEMODES}}},

    "minecraft.player.list": {"required": {"server_id": _SERVER_ID}},
    "minecraft.player.whitelist_add": {"required": {
        "server_id": _SERVER_ID, "player": {"type": "string", "regex": PLAYER_NAME_RE}}},
    "minecraft.player.whitelist_remove": {"required": {
        "server_id": _SERVER_ID, "player": {"type": "string", "regex": PLAYER_NAME_RE}}},
    "minecraft.player.whitelist_list": {"required": {"server_id": _SERVER_ID}},
    "minecraft.player.kick": {"required": {
        "server_id": _SERVER_ID, "player": {"type": "string", "regex": PLAYER_NAME_RE}}},

    "minecraft.backup.create": {"required": {"server_id": _SERVER_ID}},
    "minecraft.backup.list": {"required": {"server_id": _SERVER_ID}},
    "minecraft.backup.delete": {"required": {
        "server_id": _SERVER_ID, "backup_id": {"type": "string", "regex": BACKUP_ID_RE}}},
    "minecraft.backup.restore": {"required": {
        "server_id": _SERVER_ID, "backup_id": {"type": "string", "regex": BACKUP_ID_RE}}},

    "playit.claim_begin": {"required": {}, "optional": {"local_port": _PORT}},
    "playit.claim_poll": {"required": {"code": {"type": "string", "regex": r"^[a-f0-9]{1,32}$"}},
                          "optional": {"local_port": _PORT}},
    "playit.playit_start": {"required": {}, "optional": {"local_port": _PORT}},
    "playit.ensure_tunnel": {"required": {"local_port": _PORT}},
    "playit.status": {"required": {}, "optional": {"local_port": _PORT}},
    "playit.remove_tunnel": {"required": {"local_port": _PORT}},
    "playit.teardown": {"required": {}, "optional": {}},
}


def _check_field(value, spec):
    t = spec.get("type")
    if t == "integer":
        if not (isinstance(value, int) and not isinstance(value, bool)):
            return "not_integer"
        if "min" in spec and value < spec["min"]:
            return "below_min"
        if "max" in spec and value > spec["max"]:
            return "above_max"
    elif t == "boolean":
        if not isinstance(value, bool):
            return "not_boolean"
    elif t == "string":
        if not isinstance(value, str):
            return "not_string"
        if "enum" in spec and value not in spec["enum"]:
            return "not_in_enum"
        if "regex" in spec and not re.fullmatch(spec["regex"], value):
            return "regex_mismatch"
    elif t == "array":
        if not isinstance(value, list):
            return "not_array"
        if "max_items" in spec and len(value) > spec["max_items"]:
            return "too_many_items"
        item_spec = spec.get("items", {})
        for item in value:
            reason = _check_field(item, item_spec)
            if reason:
                return f"item_{reason}"
    else:
        return "unknown_type"
    return None


def validate_params(action, params):
    """(ok, reason). Deny-by-default: unknown field, missing required, wrong type/range, or
    a value outside the allowed set all fail. No additional properties are ever accepted."""
    schema = SCHEMAS.get(action)
    if schema is None:
        return False, "no_schema"
    required = schema.get("required", {})
    optional = schema.get("optional", {})
    known = set(required) | set(optional)
    extra = sorted(set(params) - known)
    if extra:
        return False, f"unknown_field:{extra[0]}"
    for name, spec in required.items():
        if name not in params:
            return False, f"missing:{name}"
        reason = _check_field(params[name], spec)
        if reason:
            return False, f"{name}:{reason}"
    for name, spec in optional.items():
        if name in params:
            reason = _check_field(params[name], spec)
            if reason:
                return False, f"{name}:{reason}"
    return True, None


# ---- resource naming (agent-derived; the backend never names a container/volume/path) ----

MC_IMAGE = os.environ.get("MC_IMAGE", "itzg/minecraft-server")
BACKUP_IMAGE = os.environ.get("BACKUP_IMAGE", "alpine")
ALLOWED_IMAGES = {MC_IMAGE, BACKUP_IMAGE,
                  os.environ.get("PLAYIT_IMAGE", "ghcr.io/playit-cloud/playit-agent:latest")}
DEFAULT_MC_PORT = 25565
CONTAINER_RCON_PORT = 25575
SERVER_PREFIX = "mcspawn-server-"


def _container_name(server_id):
    if not re.fullmatch(SERVER_ID_RE, server_id):
        raise SecurityError("invalid server_id")
    return SERVER_PREFIX + server_id


def _volume_name(server_id):
    return _container_name(server_id) + "_data"


def _enforce_port(port):
    rng = _policy("allowed_port_range") or [0, 0]
    if not (rng[0] <= port <= rng[1]):
        raise PolicyDenied(f"port_not_allowed:{port}")


def _enforce_ram(ram_mb):
    cap = _policy("max_ram_mb") or 0
    if ram_mb > cap:
        raise PolicyDenied(f"max_ram_exceeded:{ram_mb}>{cap}")


def _rcon_cli(server_id, *cmd_args):
    """Run a Minecraft RCON command INSIDE the container via the image's `rcon-cli`. The agent
    never handles the RCON password — rcon-cli reads it from the container's own env — and the
    args are a fixed argv, so there is no shell and no injection (spec §11)."""
    container = _container_name(server_id)
    _ensure_engine_ready()
    r = run_allowed(_runtime(), ["exec", container, "rcon-cli", *cmd_args], timeout=20)
    if r.returncode != 0:
        raise CapabilityError(_bounded(r.stderr or r.stdout, 400).strip() or "rcon failed")
    return _bounded(r.stdout, 1500).strip()


# ---- capability handlers (hardcoded; each returns the result payload dict) ----

def act_health(params):
    return {"status": "ok", "agent_version": AGENT_VERSION,
            "platform": PLATFORM, "engine": _runtime()}


def act_capabilities(params):
    policy = _load_policy()
    allowed = [a for a in ACTION_REGISTRY if a in set(policy["allowed_actions"]) | _ALWAYS_ALLOWED]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "agent_version": AGENT_VERSION,
        "policy_version": policy.get("policy_version", 1),
        "allowed_actions": allowed,
        "limits": {
            "max_ram_mb": policy["max_ram_mb"],
            "allowed_port_range": policy["allowed_port_range"],
            "allow_server_delete": policy["allow_server_delete"],
            "allow_agent_uninstall": policy["allow_agent_uninstall"],
            "allow_raw_rcon": policy["allow_raw_rcon"],
            "allow_backup_restore": policy["allow_backup_restore"],
            "allow_plugins": policy["allow_plugins"],
            "allow_mods": policy["allow_mods"],
        },
    }


def act_server_create(params):
    sid = params["server_id"]
    _enforce_ram(params["ram_mb"])
    _enforce_port(params["port"])
    _enforce_port(params["rcon_port"])
    container = _container_name(sid)
    rt = _runtime()
    _ensure_engine_ready()
    run_allowed(rt, ["rm", "-f", container], timeout=60)   # idempotent recreate
    args = [
        "run", "-d", "--name", container,
        "-e", "EULA=TRUE", "-e", f"TYPE={params['kind']}", "-e", f"VERSION={params['version']}",
        "-e", f"MEMORY={int(params['ram_mb'])}M",
        "-e", "ENABLE_RCON=true", "-e", f"RCON_PASSWORD={params['rcon_password']}",
        "-e", f"RCON_PORT={CONTAINER_RCON_PORT}",
        "-p", f"{int(params['port'])}:{DEFAULT_MC_PORT}",
        "-p", f"127.0.0.1:{int(params['rcon_port'])}:{CONTAINER_RCON_PORT}",
        "-v", f"{_volume_name(sid)}:/data", "--restart", "unless-stopped",
        MC_IMAGE,
    ]
    r = run_allowed(rt, args)
    if r.returncode != 0:
        raise CapabilityError(_bounded(r.stderr, 600).strip() or "create failed")
    return {"server_id": sid, "container": container, "state": "running"}


def _lifecycle(verb, state):
    def handler(params):
        container = _container_name(params["server_id"])
        _ensure_engine_ready()
        r = run_allowed(_runtime(), [verb, container], timeout=60)
        if r.returncode != 0:
            raise CapabilityError(_bounded(r.stderr, 400).strip() or f"{verb} failed")
        return {"server_id": params["server_id"], "state": state}
    return handler


def act_server_status(params):
    container = _container_name(params["server_id"])
    _ensure_engine_ready()
    r = run_allowed(_runtime(), ["inspect", "-f", "{{.State.Status}}", container], timeout=30)
    status = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "missing"
    return {"server_id": params["server_id"], "status": status}


def act_server_logs(params):
    container = _container_name(params["server_id"])
    lines = params.get("lines", 30)
    _ensure_engine_ready()
    r = run_allowed(_runtime(), ["logs", "--tail", str(int(lines)), container], timeout=30)
    return {"server_id": params["server_id"], "logs": _bounded((r.stdout or "") + (r.stderr or ""))}


def act_server_delete(params):
    if not _policy("allow_server_delete"):
        raise PolicyDenied("server_delete_disabled")
    sid = params["server_id"]
    container = _container_name(sid)
    rt = _runtime()
    _ensure_engine_ready()
    run_allowed(rt, ["rm", "-f", container], timeout=60)
    run_allowed(rt, ["volume", "rm", _volume_name(sid)], timeout=60)
    return {"server_id": sid, "deleted": True}


def act_reconcile_status(params):
    """Batch probe for the bot's reconciler (replaces the old inspect_states_cmd shell). Reports
    an engine-health sentinel so the bot never mistakes a momentarily-down engine for a missing
    container, plus per-server status and the playit container's status."""
    rt = _runtime()
    _ensure_engine_ready()
    info = run_allowed(rt, ["info"], timeout=30)
    engine = "up" if info.returncode == 0 else "down"
    servers = {}
    if engine == "up":
        for sid in params["server_ids"]:
            c = _container_name(sid)
            r = run_allowed(rt, ["inspect", "-f", "{{.State.Status}}", c], timeout=20)
            servers[sid] = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "missing"
        pr = run_allowed(rt, ["inspect", "-f", "{{.State.Status}}", PLAYIT_CONTAINER], timeout=20)
        playit = pr.stdout.strip() if pr.returncode == 0 and pr.stdout.strip() else "missing"
    else:
        playit = "unknown"
    return {"engine": engine, "servers": servers, "playit": playit}


def act_say(params):
    return {"server_id": params["server_id"], "text": _rcon_cli(params["server_id"], "say", params["message"])}


def act_save_all(params):
    return {"server_id": params["server_id"], "text": _rcon_cli(params["server_id"], "save-all")}


def act_set_difficulty(params):
    return {"server_id": params["server_id"],
            "text": _rcon_cli(params["server_id"], "difficulty", params["difficulty"])}


def act_set_gamemode(params):
    return {"server_id": params["server_id"],
            "text": _rcon_cli(params["server_id"], "defaultgamemode", params["gamemode"])}


def act_player_list(params):
    return {"server_id": params["server_id"], "ok": True,
            "text": _rcon_cli(params["server_id"], "list")}


def act_whitelist_add(params):
    return {"server_id": params["server_id"], "player": params["player"],
            "text": _rcon_cli(params["server_id"], "whitelist", "add", params["player"])}


def act_whitelist_remove(params):
    return {"server_id": params["server_id"], "player": params["player"],
            "text": _rcon_cli(params["server_id"], "whitelist", "remove", params["player"])}


def act_whitelist_list(params):
    return {"server_id": params["server_id"], "text": _rcon_cli(params["server_id"], "whitelist", "list")}


def act_kick(params):
    return {"server_id": params["server_id"], "player": params["player"],
            "text": _rcon_cli(params["server_id"], "kick", params["player"])}


def act_console_tail(params):
    """Read-only live console output (= the server's stdout). Safe; not gated (logs are already
    available). The matching write path, console_exec, IS gated."""
    return act_server_logs(params)


def act_console_exec(params):
    """Raw console command — the WebApp live-console write path. Gated OFF by default: the
    backend is untrusted, so a free-form console command is only relayed when the OWNER has
    opted in via policy allow_raw_rcon (spec §11)."""
    if not _policy("allow_raw_rcon"):
        raise PolicyDenied("raw_console_disabled")
    return {"server_id": params["server_id"], "text": _rcon_cli(params["server_id"], params["command"])}


# ---- backups (workspace-jailed; restore is gated) ----

def _backups_dir(server_id):
    return safe_join(_ensure_workspace(), "backups", server_id)


def act_backup_create(params):
    sid = params["server_id"]
    dest = _backups_dir(sid)
    os.makedirs(dest, exist_ok=True)
    backup_id = time.strftime("%Y%m%d-%H%M%S")
    out_path = safe_join(dest, backup_id + ".tgz")
    rt = _runtime()
    _ensure_engine_ready()
    # tar the named world volume into the workspace via a throwaway container (named volume in,
    # workspace-scoped bind mount out — spec §10). No host paths beyond the workspace are mounted.
    r = run_allowed(rt, [
        "run", "--rm", "-v", f"{_volume_name(sid)}:/data:ro", "-v", f"{dest}:/backup",
        BACKUP_IMAGE, "tar", "czf", f"/backup/{backup_id}.tgz", "-C", "/data", ".",
    ])
    if r.returncode != 0:
        raise CapabilityError(_bounded(r.stderr, 400).strip() or "backup failed")
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    return {"server_id": sid, "backup_id": backup_id, "size_bytes": size}


def act_backup_list(params):
    sid = params["server_id"]
    dest = _backups_dir(sid)
    items = []
    if os.path.isdir(dest):
        for name in sorted(os.listdir(dest)):
            if name.endswith(".tgz"):
                p = safe_join(dest, name)
                items.append({"backup_id": name[:-4], "size_bytes": os.path.getsize(p)})
    return {"server_id": sid, "backups": items}


def act_backup_delete(params):
    sid = params["server_id"]
    path = safe_join(_backups_dir(sid), params["backup_id"] + ".tgz")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return {"server_id": sid, "backup_id": params["backup_id"], "deleted": True}


def act_backup_restore(params):
    if not _policy("allow_backup_restore"):
        raise PolicyDenied("backup_restore_disabled")
    sid = params["server_id"]
    src = safe_join(_backups_dir(sid), params["backup_id"] + ".tgz")
    if not os.path.exists(src):
        raise CapabilityError("backup not found")
    dest = _backups_dir(sid)
    vol = _volume_name(sid)
    tar_in = f"/backup/{params['backup_id']}.tgz"   # backup_id is regex-validated + path-jailed
    rt = _runtime()
    _ensure_engine_ready()
    # Two fixed argv container runs (no shell): clear the world volume, then extract the backup
    # into it. The named volume goes in and a workspace-scoped backups dir comes in read-only.
    clear = run_allowed(rt, ["run", "--rm", "-v", f"{vol}:/data", BACKUP_IMAGE,
                             "find", "/data", "-mindepth", "1", "-delete"])
    if clear.returncode != 0:
        raise CapabilityError(_bounded(clear.stderr, 400).strip() or "restore (clear) failed")
    r = run_allowed(rt, ["run", "--rm", "-v", f"{vol}:/data", "-v", f"{dest}:/backup:ro",
                         BACKUP_IMAGE, "tar", "xzf", tar_in, "-C", "/data"])
    if r.returncode != 0:
        raise CapabilityError(_bounded(r.stderr, 400).strip() or "restore failed")
    return {"server_id": sid, "backup_id": params["backup_id"], "restored": True}


# ---- playit.gg public address (per-port tunnels; user links their OWN account) ----

PLAYIT_API = os.environ.get("PLAYIT_API", "https://api.playit.gg").rstrip("/")
PLAYIT_IMAGE = os.environ.get("PLAYIT_IMAGE", "ghcr.io/playit-cloud/playit-agent:latest")
PLAYIT_CONTAINER = "mc-spawn-playit"
PLAYIT_KEY_PATH = os.path.join(os.path.dirname(STATE_PATH) or ".", "playit.key")
PLAYIT_VERSION = os.environ.get("PLAYIT_VERSION", "playit 1.0.9")


def _playit_local_ip():
    return os.environ.get("PLAYIT_LOCAL_IP") or "127.0.0.1"


def _playit_api(path, body, secret=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(PLAYIT_API + path, data=data, method="POST")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", "Agent-Key " + secret)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            env = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            env = json.loads(e.read())
        except Exception:  # noqa: BLE001
            return False, {"http": e.code}
    except Exception as e:  # noqa: BLE001
        return False, {"error": type(e).__name__}
    if isinstance(env, dict) and env.get("status") == "success":
        return True, env.get("data")
    return False, (env.get("data") if isinstance(env, dict) else None)


def _playit_secret():
    try:
        with open(PLAYIT_KEY_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_playit_secret(secret):
    os.makedirs(os.path.dirname(PLAYIT_KEY_PATH) or ".", exist_ok=True)
    with open(PLAYIT_KEY_PATH, "w") as f:
        f.write(secret)
    os.chmod(PLAYIT_KEY_PATH, 0o600)


def _playit_rundata(secret):
    ok, data = _playit_api("/v1/agents/rundata", {}, secret=secret)
    return data if ok and isinstance(data, dict) else None


def _tunnel_name(local_port):
    return f"mc-spawn-{int(local_port)}" if local_port else "mc-spawn"


def _is_ours(name):
    return name == "mc-spawn" or (name or "").startswith("mc-spawn-")


def _port_address(data, local_port):
    want = _tunnel_name(local_port) if local_port else None

    def mine(t):
        n = t.get("name") or ""
        return (n == want) if want else _is_ours(n)

    for t in data.get("tunnels", []):
        if mine(t) and t.get("display_address"):
            return t["display_address"], False
    for t in data.get("pending", []):
        if mine(t):
            return None, True
    return None, False


def _playit_address(secret, local_port):
    data = _playit_rundata(secret)
    return _port_address(data, local_port) if data is not None else (None, False)


def _playit_create_tunnel(secret, agent_id, local_port):
    body = {
        "name": _tunnel_name(local_port), "tunnel_type": "minecraft-java",
        "port_type": "tcp", "port_count": 1,
        "origin": {"type": "agent", "data": {
            "agent_id": agent_id, "local_ip": _playit_local_ip(), "local_port": int(local_port)}},
        "enabled": True, "alloc": None, "firewall_id": None, "proxy_protocol": None,
    }
    ok, data = _playit_api("/tunnels/create", body, secret=secret)
    if ok:
        return True, None
    err = data if isinstance(data, str) else (data.get("error") if isinstance(data, dict) else None)
    return False, err


def _playit_delete_tunnels(secret, local_port=None):
    data = _playit_rundata(secret)
    if not data:
        return
    target = _tunnel_name(local_port) if local_port else None
    for t in data.get("tunnels", []):
        name = t.get("name") or ""
        if not _is_ours(name) or (target is not None and name != target):
            continue
        tid = t.get("id") or t.get("tunnel_id")
        if tid:
            _playit_api("/tunnels/delete", {"tunnel_id": tid}, secret=secret)


def _playit_run(secret):
    """(Re)start the playit container on the host network via the engine (argv only). The fixed
    container name is agent-owned; the image is on the allowlist."""
    _ensure_engine_ready()
    rt = _runtime()
    run_allowed(rt, ["rm", "-f", PLAYIT_CONTAINER], timeout=60)
    r = run_allowed(rt, [
        "run", "-d", "--name", PLAYIT_CONTAINER, "--restart", "unless-stopped",
        "--network", "host", "-e", f"SECRET_KEY={secret}", PLAYIT_IMAGE,
    ])
    return r.returncode == 0


def _playit_teardown():
    secret = _playit_secret()
    if secret:
        _playit_delete_tunnels(secret)
    _ensure_engine_ready()
    run_allowed(_runtime(), ["rm", "-f", PLAYIT_CONTAINER], timeout=60)
    try:
        os.remove(PLAYIT_KEY_PATH)
    except OSError:
        pass


_TUNNEL_CREATE_GUARD = {}
_TUNNEL_CREATE_GUARD_SEC = 120


def _recently_created_tunnel(local_port):
    if not local_port:
        return False
    ts = _TUNNEL_CREATE_GUARD.get(int(local_port))
    return ts is not None and (time.monotonic() - ts) < _TUNNEL_CREATE_GUARD_SEC


def act_playit_claim_begin(params):
    if _playit_secret():
        return {"status": "linked"}
    code = os.urandom(5).hex()
    ok, _ = _playit_api("/claim/setup", {"code": code, "agent_type": "self-managed", "version": PLAYIT_VERSION})
    if not ok:
        return {"status": "error", "error": "playit недоступен"}
    return {"status": "begin", "code": code, "url": "https://playit.gg/claim/" + code}


def act_playit_claim_poll(params):
    if _playit_secret():
        return {"status": "accepted"}
    code = params["code"]
    _ok, state = _playit_api("/claim/setup", {"code": code, "agent_type": "self-managed", "version": PLAYIT_VERSION})
    if state == "UserRejected":
        return {"status": "rejected"}
    if state == "WaitingForUserVisit":
        return {"status": "waiting", "stage": "visit"}
    if state != "UserAccepted":
        return {"status": "waiting", "stage": "approve"}
    ok, data = _playit_api("/claim/exchange", {"code": code})
    if not ok or not isinstance(data, dict) or not data.get("secret_key"):
        return {"status": "waiting", "stage": "approve"}
    _save_playit_secret(data["secret_key"])
    return {"status": "accepted"}


def act_playit_start(params):
    secret = _playit_secret()
    if not secret:
        return {"status": "unlinked"}
    if not _playit_run(secret):
        return {"status": "error", "error": "не удалось запустить playit (docker)"}
    return {"status": "ok"}


def act_playit_ensure_tunnel(params):
    secret = _playit_secret()
    local_port = params["local_port"]
    if not secret:
        return {"status": "unlinked"}
    data = _playit_rundata(secret)
    if data is None:
        return {"status": "connecting"}
    addr, pending = _port_address(data, local_port)
    if addr:
        _TUNNEL_CREATE_GUARD.pop(int(local_port), None)
        return {"status": "exists", "address": addr}
    if pending:
        _TUNNEL_CREATE_GUARD.pop(int(local_port), None)
        return {"status": "exists"}
    if _recently_created_tunnel(local_port):
        return {"status": "created"}
    agent_id = data.get("agent_id")
    if not agent_id:
        return {"status": "connecting"}
    ok, err = _playit_create_tunnel(secret, agent_id, local_port)
    if ok:
        _TUNNEL_CREATE_GUARD[int(local_port)] = time.monotonic()
        return {"status": "created"}
    if err == "AgentVersionTooOld":
        return {"status": "connecting"}
    return {"status": "error", "error": err or "не удалось создать туннель"}


def act_playit_status(params):
    secret = _playit_secret()
    if not secret:
        return {"status": "unlinked"}
    addr, _pending = _playit_address(secret, params.get("local_port"))
    return {"status": "ok" if addr else "no_tunnel", "address": addr}


def act_playit_remove_tunnel(params):
    secret = _playit_secret()
    local_port = params["local_port"]
    if secret:
        _playit_delete_tunnels(secret, local_port)
    _TUNNEL_CREATE_GUARD.pop(int(local_port), None)
    return {"status": "ok"}


def act_playit_teardown(params):
    _playit_teardown()
    _TUNNEL_CREATE_GUARD.clear()
    return {"status": "ok"}


# ---- agent self-uninstall (scoped: removes ONLY agent-owned resources + itself) ----

SERVICE_NAME = "mc-spawn-agent"


def act_uninstall(params):
    if not _policy("allow_agent_uninstall"):
        raise PolicyDenied("agent_uninstall_disabled")
    rt = _runtime()
    _ensure_engine_ready()
    # Remove every container WE created (matched by our own name prefix — the backend never
    # supplies a container name) and its world volume, then playit, then the agent itself.
    listed = run_allowed(rt, ["ps", "-a", "--filter", f"name=^{SERVER_PREFIX}",
                              "--format", "{{.Names}}"], timeout=60)
    names = [n for n in (listed.stdout or "").split() if n.startswith(SERVER_PREFIX)]
    for c in names:
        run_allowed(rt, ["rm", "-f", c], timeout=60)
        run_allowed(rt, ["volume", "rm", c + "_data"], timeout=60)
    _playit_teardown()
    try:
        _spawn_self_cleanup()
    except Exception as e:  # noqa: BLE001
        return {"status": "ok", "removed": names, "self_cleanup": f"deferred-failed:{type(e).__name__}"}
    return {"status": "ok", "removed": names}


def _spawn_self_cleanup():
    """Launch a DETACHED Python helper (this same file, `_cleanup` subcommand) that removes the
    service/autostart entry + agent files a few seconds later — surviving the agent's own death
    when its service is stopped. Argv only, no shell (the helper uses argv subprocess + winreg)."""
    d = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.dirname(STATE_PATH) or "."
    argv = [sys.executable, os.path.abspath(__file__), "_cleanup", d, state_dir]
    if IS_WINDOWS:
        subprocess.Popen(
            argv,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(argv, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cleanup_main(install_dir, state_dir):
    """Detached self-cleanup. Fixed agent commands only (argv / winreg), never a backend string
    and never a shell. Best-effort + idempotent."""
    time.sleep(5)
    if IS_WINDOWS:
        _run_internal(["schtasks", "/delete", "/tn", SERVICE_NAME, "/f"], timeout=30)
        try:
            import winreg  # noqa: PLC0415 — Windows-only, lazy
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, SERVICE_NAME)
        except OSError:
            pass
        _run_internal(["wsl", "--terminate", WSL_DISTRO], timeout=30)
        _run_internal(["wsl", "--unregister", WSL_DISTRO], timeout=60)
    else:
        for argv in (
            ["systemctl", "disable", "--now", SERVICE_NAME],
            ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
            ["rc-update", "del", SERVICE_NAME, "default"],
            ["rc-service", SERVICE_NAME, "stop"],
        ):
            _run_internal(argv, timeout=30)
        for path in (f"/etc/systemd/system/{SERVICE_NAME}.service",
                     os.path.expanduser(f"~/.config/systemd/user/{SERVICE_NAME}.service"),
                     f"/etc/init.d/{SERVICE_NAME}"):
            try:
                os.remove(path)
            except OSError:
                pass
        _run_internal(["systemctl", "daemon-reload"], timeout=30)
        _drop_reboot_crontab()
    for d in (install_dir, state_dir):
        shutil.rmtree(d, ignore_errors=True)


def _drop_reboot_crontab():
    """Remove our @reboot line from the user's crontab without a shell pipe."""
    cur = _run_internal(["crontab", "-l"], timeout=20)
    if cur is None or cur.returncode != 0:
        return
    kept = [ln for ln in (cur.stdout or "").splitlines() if SERVICE_NAME not in ln]
    try:
        subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True,
                       capture_output=True, timeout=20, check=False)
    except Exception:  # noqa: BLE001
        pass


# ---- action registry (deny-by-default; no dynamic registration from the server) ----

ACTION_REGISTRY = {
    "agent.health": act_health,
    "agent.capabilities": act_capabilities,
    "agent.uninstall": act_uninstall,

    "minecraft.server.create": act_server_create,
    "minecraft.server.start": _lifecycle("start", "running"),
    "minecraft.server.stop": _lifecycle("stop", "stopped"),
    "minecraft.server.restart": _lifecycle("restart", "running"),
    "minecraft.server.status": act_server_status,
    "minecraft.server.logs": act_server_logs,
    "minecraft.server.delete": act_server_delete,
    "minecraft.server.reconcile_status": act_reconcile_status,

    "minecraft.server.say": act_say,
    "minecraft.server.save_all": act_save_all,
    "minecraft.server.console_tail": act_console_tail,
    "minecraft.server.console_exec": act_console_exec,

    "minecraft.config.set_difficulty": act_set_difficulty,
    "minecraft.config.set_gamemode": act_set_gamemode,

    "minecraft.player.list": act_player_list,
    "minecraft.player.whitelist_add": act_whitelist_add,
    "minecraft.player.whitelist_remove": act_whitelist_remove,
    "minecraft.player.whitelist_list": act_whitelist_list,
    "minecraft.player.kick": act_kick,

    "minecraft.backup.create": act_backup_create,
    "minecraft.backup.list": act_backup_list,
    "minecraft.backup.delete": act_backup_delete,
    "minecraft.backup.restore": act_backup_restore,

    "playit.claim_begin": act_playit_claim_begin,
    "playit.claim_poll": act_playit_claim_poll,
    "playit.playit_start": act_playit_start,
    "playit.ensure_tunnel": act_playit_ensure_tunnel,
    "playit.status": act_playit_status,
    "playit.remove_tunnel": act_playit_remove_tunnel,
    "playit.teardown": act_playit_teardown,
}


# ---- protocol v2 envelope: parse, replay/expiry guard, dispatch ----

_REPLAY = {}     # request_id -> (monotonic_ts, result_envelope)


def _parse_ts(s):
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _result(request_id, action, status, result):
    return {"request_id": request_id, "status": status, "action": action,
            "result": result, "agent_policy_version": _policy("policy_version")}


def process_command(envelope):
    """Validate a v2 envelope and run the allowed capability. Returns the result envelope.
    Deny-by-default at every step; never raises into the caller; never fails OPEN."""
    if not isinstance(envelope, dict):
        return _result("", "", "invalid", {"reason": "envelope_not_object"})
    request_id = envelope.get("request_id") or ""
    action = envelope.get("action") or ""

    if envelope.get("protocol_version") != PROTOCOL_VERSION:
        _audit("INVALID", action, request_id, "bad_protocol_version")
        return _result(request_id, action, "invalid", {"reason": "bad_protocol_version"})
    params = envelope.get("params")
    if not isinstance(request_id, str) or not isinstance(action, str) or not isinstance(params, dict):
        _audit("INVALID", action, request_id, "bad_envelope")
        return _result(request_id, action, "invalid", {"reason": "bad_envelope"})

    # expiry / clock-skew (spec §17)
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        issued = _parse_ts(envelope["issued_at"])
        expires = _parse_ts(envelope["expires_at"])
    except (KeyError, ValueError, TypeError):
        _audit("INVALID", action, request_id, "bad_timestamps")
        return _result(request_id, action, "invalid", {"reason": "bad_timestamps"})
    if now > expires:
        _audit("DENIED", action, request_id, "expired")
        return _result(request_id, action, "denied", {"reason": "expired"})
    if issued > now + datetime.timedelta(seconds=CLOCK_SKEW_SEC):
        _audit("DENIED", action, request_id, "issued_in_future")
        return _result(request_id, action, "denied", {"reason": "issued_in_future"})

    # replay protection (spec §17)
    cutoff = time.monotonic() - REPLAY_WINDOW_SEC
    for rid in [r for r, (ts, _) in _REPLAY.items() if ts < cutoff]:
        _REPLAY.pop(rid, None)
    if request_id in _REPLAY:
        _audit("DENIED", action, request_id, "replay")
        return _REPLAY[request_id][1]

    handler = ACTION_REGISTRY.get(action)
    if handler is None:
        _audit("DENIED", action, request_id, "unknown_action")
        return _result(request_id, action, "denied", {"reason": "unknown_action"})

    allowed = set(_policy("allowed_actions") or []) | _ALWAYS_ALLOWED
    if action not in allowed:
        _audit("DENIED", action, request_id, "action_disabled_by_policy")
        return _result(request_id, action, "denied", {"reason": "action_disabled_by_policy"})

    ok, reason = validate_params(action, params)
    if not ok:
        _audit("INVALID", action, request_id, reason)
        return _result(request_id, action, "invalid", {"reason": reason})

    try:
        data = handler(params)
        envelope_out = _result(request_id, action, "ok", data)
        _audit("OK", action, request_id)
    except (PolicyDenied, SecurityError) as e:
        envelope_out = _result(request_id, action, "denied", {"reason": str(e)})
        _audit("DENIED", action, request_id, str(e))
    except CapabilityError as e:
        envelope_out = _result(request_id, action, "failed", {"error": str(e)})
        _audit("FAILED", action, request_id, _bounded(str(e), 120))
    except Exception as e:  # noqa: BLE001 — a handler bug must not crash the poll loop
        envelope_out = _result(request_id, action, "failed", {"error": type(e).__name__})
        _audit("FAILED", action, request_id, type(e).__name__)
    _REPLAY[request_id] = (time.monotonic(), envelope_out)
    return envelope_out


# ---- CLI subcommands (owner-facing transparency / control) ----

def _cli_audit():
    path = _audit_path()
    try:
        with open(path) as f:
            lines = f.readlines()[-50:]
        sys.stdout.write("".join(lines))
    except OSError:
        print(f"no audit log yet at {path}")


def _cli_policy():
    print(json.dumps(_load_policy(), indent=2))


def _cli_capabilities():
    print(json.dumps(act_capabilities({}), indent=2))


def _cli_wipe_creds():
    for p in (STATE_PATH, PLAYIT_KEY_PATH):
        try:
            os.remove(p)
            print(f"removed {p}")
        except OSError:
            pass


def _dispatch_cli(argv):
    cmd = argv[0]
    if cmd == "_cleanup" and len(argv) >= 3:
        _cleanup_main(argv[1], argv[2])
    elif cmd == "audit":
        _cli_audit()
    elif cmd == "policy":
        _cli_policy()
    elif cmd == "capabilities":
        _cli_capabilities()
    elif cmd == "wipe-creds":
        _cli_wipe_creds()
    elif cmd == "approve":
        # Local-approval store is scaffolded but unused by the default UX (deletes are scoped +
        # confirmed in the bot). Present so the command exists if an owner enables stricter gating.
        print("local approval is not required by the current policy")
    else:
        print("usage: agent.py [audit|policy|capabilities|wipe-creds|approve <id>]")
        sys.exit(2)


# ---- main poll loop ----

def main():
    if len(sys.argv) > 1:
        _dispatch_cli(sys.argv[1:])
        return
    _log("agent_start", version=USER_AGENT, platform=PLATFORM, runtime=_runtime(),
         control_url=CONTROL_URL, state=STATE_PATH, policy=POLICY_PATH,
         workspace=_workspace_root())
    if not CONTROL_URL:
        _log("config_missing", level="error", key="CONTROL_URL")
        sys.exit(1)
    if not CONTROL_URL.startswith("https://") and not DEV_MODE:
        _log("config_invalid", level="error", key="CONTROL_URL", reason="https_required")
        sys.exit(1)
    _ensure_workspace()
    state = _load_state()
    secret = state["secret"] if state else _enroll()
    if not secret:
        sys.exit(1)
    _log("poll_loop_start")
    backoff = 1
    while True:
        try:
            status, cmd = _http("GET", "/poll", secret=secret)
            if status == 200 and cmd:
                envelope = cmd.get("payload") if isinstance(cmd, dict) else None
                result = process_command(envelope if isinstance(envelope, dict) else {})
                _http("POST", "/result",
                      {"id": cmd["id"], "status": "done", "result": result}, secret=secret)
                if result.get("action") == "agent.uninstall" and result.get("status") == "ok":
                    _log("agent_uninstalled")
                    sys.exit(0)
            elif status in (200, 204):
                pass
            elif status == 401:
                _log("auth_rejected", level="warning")
                new_secret = _enroll()
                if not new_secret:
                    _log("reenroll_failed", level="error")
                    sys.exit(1)
                secret = new_secret
            else:
                _log("poll_http_error", level="warning", http_status=status, retry_sec=backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            _log("poll_transport_error", level="warning", error=type(e).__name__, retry_sec=backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
