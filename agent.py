#!/usr/bin/env python3
"""mc-spawn agent — runs on the user's own machine. STDLIB ONLY (python3).

Outbound only: it dials the control API (no inbound ports on this box), redeems a
one-time enroll token for a long-lived secret, then long-polls for commands and
executes them locally:
  * shell  — run a shell script (provisioning/lifecycle issued by the bot);
  * rcon   — talk to a Minecraft RCON on 127.0.0.1 (no tunnel needed; we're local);
  * playit — link the user's own playit.gg account (claim flow) and report the public
             play address friends connect to (no inbound port opened on this box).

Config via env: CONTROL_URL (required), TOKEN (one-time, first run only),
AGENT_STATE (cred file; default /etc/mc-spawn-agent/cred.json on Linux,
%LOCALAPPDATA%\\mc-spawn-agent\\cred.json on Windows).

Cross-platform (Linux + Windows): paths, the script shell, and the playit container's
networking adapt to the OS; MC hosting + RCON already work on both via port mapping
(Docker Desktop publishes ports to the host's localhost). See install.sh / install.ps1.

Thin by design: all Minecraft logic lives in the bot (mc-spawn-bot) — the agent is
a small, auditable executor. This is the client half of the system and lives in its
own repository so users can inspect exactly what they install; a Go rewrite can be a
drop-in (same versioned HTTP protocol — PROTOCOL_VERSION).
"""
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Phase 6: the agent is cross-platform (Linux + Windows). os.name == "nt" on Windows.
IS_WINDOWS = os.name == "nt"

# Versioned control-plane contract (Phase 6): the agent advertises this on every
# call so a future compiled (Go) binary can be a drop-in and the control plane can
# negotiate. Bump ONLY on a breaking change to the /enroll·/poll·/result wire shapes.
PROTOCOL_VERSION = 1
# Coarse platform tag the control plane stores (e.g. "linux/x86_64", "windows/amd64").
PLATFORM = f"{platform.system().lower()}/{(platform.machine() or '').lower()}"


def _default_state_path():
    """Cred-file default. The installer always sets AGENT_STATE explicitly; this only
    covers a bare manual run. Windows has no /etc; the installer runs the agent as the
    user (not SYSTEM), so prefer per-user LOCALAPPDATA (writable without admin)."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("ProgramData") or os.getcwd()
        return os.path.join(base, "mc-spawn-agent", "cred.json")
    return "/etc/mc-spawn-agent/cred.json"


CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "").strip()
STATE_PATH = os.environ.get("AGENT_STATE", _default_state_path())

POLL_TIMEOUT = 40        # > server long-poll (25s)
SHELL_TIMEOUT = 600      # provisioning (docker pull) can be slow
# urllib's default "Python-urllib/x.y" UA is on Cloudflare's Browser Integrity
# Check banlist (HTTP 403, error 1010) — the control plane sits behind a proxied
# Cloudflare hostname, so send a real product UA or enroll/poll never get through.
USER_AGENT = "mc-spawn-agent/1.0"


def _log(msg):
    print(f"[mc-spawn-agent] {msg}", flush=True)


_RUNTIME_CACHE = None


def _runtime():
    """The container engine to use (Phase 6.5). Auto-detect `docker → podman → nerdctl`
    on PATH; `CONTAINER_RUNTIME` env overrides. Cached. All three share the run/-p/-v/
    start/stop/rm/logs/inspect surface the bot's scripts use, so the engine is a drop-in.
    Defaults to `docker` if none is found (so an absent engine still produces sensible
    error output rather than crashing here). The bot's scripts read it via `$MCSPAWN_RT`;
    the agent's own playit commands call this directly."""
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None:
        return _RUNTIME_CACHE
    override = os.environ.get("CONTAINER_RUNTIME", "").strip()
    if override:
        _RUNTIME_CACHE = override
    else:
        _RUNTIME_CACHE = next(
            (rt for rt in ("docker", "podman", "nerdctl") if shutil.which(rt)), "docker"
        )
    return _RUNTIME_CACHE


def _shell_argv(script):
    """Pick the shell to run a bot-issued script. The bot emits POSIX `docker` one-
    liners (see provisioner.py), so we need a POSIX shell. On Linux that's `bash`.
    On Windows, Docker Desktop ships with WSL/Git-Bash so `bash` is normally on PATH —
    prefer it; fall back to `cmd /c` only if there's genuinely no bash (the docker
    commands themselves are largely shell-agnostic). Override with AGENT_SHELL."""
    override = os.environ.get("AGENT_SHELL")
    if override:
        return [override, "-c", script]
    if IS_WINDOWS and not shutil.which("bash"):
        return ["cmd", "/c", script]
    return ["bash", "-c", script]


def _http(method, path, body=None, secret=None, timeout=POLL_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CONTROL_URL + path, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    # Versioned contract + platform (Phase 6) — cheap headers on every call so the
    # control plane can negotiate/record without changing any response shape.
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
    """Redeem the one-time TOKEN for a long-lived secret. Returns the new secret,
    or None if no token is set or enrollment was rejected — the caller decides
    whether that is fatal. The enroll token is single-use (consumed server-side on
    success), so a repeat attempt with a stale/used token correctly returns None."""
    if not TOKEN:
        _log("no stored creds and no TOKEN env — cannot enroll")
        return None
    status, data = _http(
        "POST", "/enroll",
        {"token": TOKEN, "protocol_version": PROTOCOL_VERSION, "platform": PLATFORM},
        timeout=20,
    )
    if status != 200 or not data:
        _log(f"enroll failed (HTTP {status})")
        return None
    _save_state({"machine_id": data["machine_id"], "secret": data["secret"]})
    _log(f"enrolled as machine {data['machine_id']}")
    return data["secret"]


# ---- command execution ----

def _run_shell(payload):
    try:
        # Expose the detected container engine to the bot's POSIX scripts as $MCSPAWN_RT
        # (they call `${MCSPAWN_RT:-docker} …`), so one script set runs on docker/podman/
        # nerdctl without the bot knowing what's installed on the box (Phase 6.5).
        env = {**os.environ, "MCSPAWN_RT": _runtime()}
        p = subprocess.run(
            _shell_argv(payload["script"]),
            capture_output=True, text=True, timeout=SHELL_TIMEOUT, env=env,
        )
        return {"exit": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}
    except subprocess.TimeoutExpired:
        return {"exit": None, "stdout": "", "stderr": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {"exit": None, "stdout": "", "stderr": type(e).__name__}


def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


def _rcon(payload):
    """Minimal Source RCON client (stdlib) to 127.0.0.1 — the server is local."""
    host, port = "127.0.0.1", int(payload["rcon_port"])
    password, command = payload["password"], payload["command"]

    def pkt(rid, ptype, body):
        p = struct.pack("<ii", rid, ptype) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(p)) + p

    def recv(sock):
        (ln,) = struct.unpack("<i", _recvn(sock, 4))
        data = _recvn(sock, ln)
        rid, ptype = struct.unpack("<ii", data[:8])
        return rid, ptype, data[8:-2].decode("utf-8", "replace")

    try:
        s = socket.create_connection((host, port), timeout=10)
        s.settimeout(10)
        s.sendall(pkt(1, 3, password))
        rid, _t, _b = recv(s)
        if rid == -1:
            s.close()
            return {"ok": False, "text": "Неверный RCON-пароль."}
        s.sendall(pkt(2, 2, command))
        _rid, _t2, text = recv(s)
        s.close()
        return {"ok": True, "text": text.strip()[:1500]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": f"RCON недоступен ({type(e).__name__})."}


# ---- playit.gg public address (Phase 3) ----
#
# The user links their OWN free playit account (claim flow) — the operator never holds
# the account, which keeps us ToS-clean (no resale). The agent then runs the playit
# agent under that secret (docker, host network so it reaches the local MC port) and
# reads the assigned public address from playit's API. The secret stays on this box,
# chmod 600, and is never sent to the control plane.
#
# playit HTTP API (https://api.playit.gg) — JSON, enveloped {"status":"success","data":..}:
#   POST /claim/setup     {code, agent_type:"self-managed", version}
#                           -> "WaitingForUser*" | "UserAccepted" | "UserRejected"
#   POST /claim/exchange  {code} -> {secret_key}          (once the user approves)
#   POST /v1/agents/rundata  (auth: "Agent-Key <secret>")
#                           -> {agent_id, tunnels:[{display_address,..}], pending:[..]}

PLAYIT_API = os.environ.get("PLAYIT_API", "https://api.playit.gg").rstrip("/")
PLAYIT_IMAGE = os.environ.get("PLAYIT_IMAGE", "ghcr.io/playit-cloud/playit-agent:latest")
PLAYIT_CONTAINER = "mc-spawn-playit"
PLAYIT_KEY_PATH = os.path.join(os.path.dirname(STATE_PATH) or ".", "playit.key")
# playit checks this at /claim/setup and remembers it as the agent's version; a bad/old
# value makes a later /tunnels/create fail with `AgentVersionTooOld`. The real client
# sends "playit <semver>" (format!("playit {}", CARGO_PKG_VERSION)), so we MUST mimic that
# exact shape with a current version — not "mc-spawn-agent". Bump as playit's minimum rises;
# overridable via env. (The playit docker container itself reports its own real version once
# it connects, but we may create the tunnel before that, so the claim version must be valid.)
PLAYIT_VERSION = os.environ.get("PLAYIT_VERSION", "playit 1.0.9")


def _playit_local_ip():
    """Address the playit container uses to reach the local MC server's published port
    (Phase 6, cross-platform). On Linux we run playit with `--network host`, so the
    host loopback `127.0.0.1:<mc_port>` is correct. On Windows/macOS, Docker Desktop
    runs containers in a Linux VM where `--network host` does NOT expose the host's
    ports, so playit must reach the host via the Docker-Desktop alias
    `host.docker.internal`. Override with PLAYIT_LOCAL_IP. (Live-verify on Windows.)"""
    override = os.environ.get("PLAYIT_LOCAL_IP")
    if override:
        return override
    return "127.0.0.1" if not IS_WINDOWS else "host.docker.internal"


def _playit_net_args():
    """Docker networking flags for the playit container per OS. Linux: host network.
    Windows/macOS: bridge + a guaranteed `host.docker.internal` mapping so playit can
    forward to the host-published MC port."""
    if not IS_WINDOWS:
        return ["--network", "host"]
    return ["--add-host", "host.docker.internal:host-gateway"]


def _playit_api(path, body, secret=None):
    """POST to the playit API; return (ok, data). Never raises — `ok` is False on any
    transport/HTTP error or a non-success envelope."""
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
    """Raw agent run-data (tunnels, pending, agent_id) or None on error."""
    ok, data = _playit_api("/v1/agents/rundata", {}, secret=secret)
    return data if ok and isinstance(data, dict) else None


def _tunnel_name(local_port):
    """Each hosted server gets its OWN tunnel, keyed by its box-local port, so several
    servers on one machine don't collide on a single shared tunnel/address."""
    return f"mc-spawn-{int(local_port)}" if local_port else "mc-spawn"


def _is_ours(name):
    """A tunnel WE created (vs one the user made by hand)."""
    return name == "mc-spawn" or (name or "").startswith("mc-spawn-")


def _port_address(data, local_port):
    """(address_or_None, pending_bool) for OUR tunnel matching this local port. With no
    port (legacy / no-arg call) match any tunnel we created."""
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
    """(address_or_None, pending_bool) for this port from the user's playit account."""
    data = _playit_rundata(secret)
    return _port_address(data, local_port) if data is not None else (None, False)


def _playit_create_tunnel(secret, agent_id, local_port):
    """Create a Minecraft-Java tunnel pointing at the local server, so the user never
    has to make one on playit's (English) dashboard. Free shared-IP allocation
    (alloc=None). Returns (ok, error_str_or_None).

    Wire format from playit-agent's api_client (POST /tunnels/create, auth header
    `Agent-Key <secret>` — same as rundata). origin.type=agent maps the public
    tunnel to 127.0.0.1:<local_port> on this box."""
    body = {
        "name": _tunnel_name(local_port),
        "tunnel_type": "minecraft-java",
        "port_type": "tcp",
        "port_count": 1,
        "origin": {"type": "agent", "data": {
            "agent_id": agent_id, "local_ip": _playit_local_ip(), "local_port": int(local_port)}},
        "enabled": True,
        "alloc": None,
        "firewall_id": None,
        "proxy_protocol": None,
    }
    ok, data = _playit_api("/tunnels/create", body, secret=secret)
    if ok:
        return True, None
    # playit returns the error as a bare enum string (e.g. "RequiresVerifiedAccount").
    err = data if isinstance(data, str) else (data.get("error") if isinstance(data, dict) else None)
    return False, err


def _playit_delete_tunnels(secret, local_port=None):
    """Delete the tunnels WE created. With `local_port`, only that one server's tunnel;
    otherwise all of ours (machine teardown). Best-effort; never raises. Only touches
    our own tunnels — any the user made by hand are left alone."""
    data = _playit_rundata(secret)
    if not data:
        return
    target = _tunnel_name(local_port) if local_port else None
    for t in data.get("tunnels", []):
        name = t.get("name") or ""
        if not _is_ours(name):
            continue
        if target is not None and name != target:
            continue
        tid = t.get("id") or t.get("tunnel_id")
        if tid:
            # playit-agent's api_client: POST /tunnels/delete {tunnel_id}.
            _playit_api("/tunnels/delete", {"tunnel_id": tid}, secret=secret)


def _playit_teardown():
    """Tear playit down on this box: delete our tunnels, stop+remove the playit
    container, drop the stored secret. Idempotent and never raises (docker/key may
    be absent) so machine/server deletion always proceeds."""
    secret = _playit_secret()
    if secret:
        _playit_delete_tunnels(secret)
    try:
        subprocess.run([_runtime(), "rm", "-f", PLAYIT_CONTAINER],
                       capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(PLAYIT_KEY_PATH)
    except OSError:
        pass


def _playit_run(secret):
    """(Re)start the playit agent under the user's secret via docker on the host network
    (so it reaches 127.0.0.1:<mc_port>). Idempotent; never raises (docker may be
    absent) — returns False on any failure so the playit op stays soft."""
    try:
        rt = _runtime()
        subprocess.run([rt, "rm", "-f", PLAYIT_CONTAINER],
                       capture_output=True, text=True, timeout=30)
        return subprocess.run(
            [rt, "run", "-d", "--name", PLAYIT_CONTAINER, "--restart", "unless-stopped",
             *_playit_net_args(), "-e", "SECRET_KEY=" + secret, PLAYIT_IMAGE],
            capture_output=True, text=True, timeout=120,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _playit(payload):
    """Link/report a server's playit public address. The ops are SMALL and quick so the
    BOT can drive pacing and show live progress (it loops claim_poll / status, editing the
    message each tick) instead of one multi-minute call that looks frozen. Per-port:
    each hosted server has its own tunnel keyed by `local_port`. Never raises.

    Ops: claim_begin (mint code/url, or 'linked'); claim_poll (one quick approval check,
    exchanges+saves the secret on accept); playit_start (run playit + ensure this port's
    tunnel, fast); status (read this port's address, auto-creating its tunnel);
    remove_tunnel (delete just this port's tunnel); teardown (full cleanup)."""
    op = payload.get("op")
    local_port = payload.get("local_port")
    secret = _playit_secret()

    if op == "teardown":
        _playit_teardown()
        return {"status": "ok"}
    if op == "remove_tunnel":
        if secret:
            _playit_delete_tunnels(secret, local_port)
        return {"status": "ok"}
    if op == "claim_begin":
        if secret:
            return {"status": "linked"}  # already linked; bot goes straight to address stage
        code = os.urandom(5).hex()  # matches playit's claim-code format (5 bytes hex)
        ok, _ = _playit_api(
            "/claim/setup", {"code": code, "agent_type": "self-managed", "version": PLAYIT_VERSION})
        if not ok:
            return {"status": "error", "error": "playit недоступен"}
        return {"status": "begin", "code": code, "url": "https://playit.gg/claim/" + code}
    if op == "claim_poll":
        # One quick check of the browser-approval state — the bot loops this with its own
        # pacing + a live "waiting…" message, so nothing blocks for minutes here.
        if secret:
            return {"status": "accepted"}  # already linked
        code = payload.get("code", "")
        _ok, state = _playit_api(
            "/claim/setup", {"code": code, "agent_type": "self-managed", "version": PLAYIT_VERSION})
        if state == "UserRejected":
            return {"status": "rejected"}
        # playit's claim is TWO site steps; surface which one is still pending so the bot
        # can tell the user exactly what to do (open the link vs. approve it on the page).
        if state == "WaitingForUserVisit":
            return {"status": "waiting", "stage": "visit"}
        if state != "UserAccepted":  # WaitingForUser (or anything not-yet-accepted)
            return {"status": "waiting", "stage": "approve"}
        ok, data = _playit_api("/claim/exchange", {"code": code})
        if not ok or not isinstance(data, dict) or not data.get("secret_key"):
            return {"status": "waiting", "stage": "approve"}
        _save_playit_secret(data["secret_key"])
        return {"status": "accepted"}
    if op == "playit_start":
        # Just ensure the playit container is running. Tunnel creation is a SEPARATE,
        # retryable op (`ensure_tunnel`) the bot loops — because on first run the container
        # must connect to playit before `/tunnels/create` is accepted (otherwise the very
        # first create fails and, with a read-only status poll, would never be retried).
        secret = _playit_secret()
        if not secret:
            return {"status": "unlinked"}
        if not _playit_run(secret):
            return {"status": "error", "error": "не удалось запустить playit (docker)"}
        return {"status": "ok"}
    if op == "ensure_tunnel":
        # ONE create-or-detect attempt (fast; the bot loops it). Idempotent: never creates
        # if this port already has a tunnel. `connecting` ⇒ "retry later" (playit API/agent
        # not ready yet, incl. the transient AgentVersionTooOld while the container connects);
        # the bot keeps looping until `created`/`exists`, so creation isn't a one-shot.
        if not secret:
            return {"status": "unlinked"}
        data = _playit_rundata(secret)
        if data is None:
            return {"status": "connecting"}
        addr, pending = _port_address(data, local_port)
        if addr:
            return {"status": "exists", "address": addr}
        if pending:
            return {"status": "exists"}
        agent_id = data.get("agent_id")
        if not agent_id:
            return {"status": "connecting"}
        if not local_port:
            return {"status": "error", "error": "no local_port"}
        ok, err = _playit_create_tunnel(secret, agent_id, local_port)
        if ok:
            return {"status": "created"}
        if err == "AgentVersionTooOld":
            return {"status": "connecting"}
        return {"status": "error", "error": err or "не удалось создать туннель"}
    if op == "status":
        if not secret:
            return {"status": "unlinked"}
        # READ-ONLY: never creates (creation is ensure_tunnel's job). Just reports this
        # port's address once its tunnel has one.
        addr, _pending = _playit_address(secret, local_port)
        return {"status": "ok" if addr else "no_tunnel", "address": addr}
    return {"status": "error", "error": f"unknown playit op {op}"}


# ---- full self-uninstall (Phase 6.5) ----
#
# When the user deletes the machine in the bot, the agent removes EVERYTHING it put on
# the box and then itself: MC containers + their world volumes, the playit container +
# tunnels, the service/Scheduled-Task, and its own files. Containers + playit are torn
# down synchronously (reliable); the service + files are removed by a DETACHED helper
# so it survives the agent's own death when the service is stopped (and, on Windows,
# can delete agent.py which is locked while running). Best-effort + idempotent.

SERVICE_NAME = "mc-spawn-agent"


def _uninstall(payload):
    rt = _runtime()
    for c in payload.get("containers", []):
        for args in ([rt, "rm", "-f", c], [rt, "volume", "rm", f"{c}_data"]):
            try:
                subprocess.run(args, capture_output=True, text=True, timeout=60)
            except Exception:  # noqa: BLE001
                pass
    _playit_teardown()
    try:
        _spawn_self_cleanup()
    except Exception as e:  # noqa: BLE001
        return {"status": "ok", "self_cleanup": f"deferred-failed:{type(e).__name__}"}
    return {"status": "ok"}


def _agent_paths():
    """Files/dirs the installer created (best-effort; mirrors install.sh/.ps1)."""
    d = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.dirname(STATE_PATH) or "."
    return d, state_dir


def _spawn_self_cleanup():
    """Launch a detached process that removes the service + agent files a few seconds
    later — long enough for the result POST to land. Detached (new session / transient
    unit) so stopping our own service doesn't kill it mid-cleanup."""
    d, state_dir = _agent_paths()
    if IS_WINDOWS:
        # Remove BOTH autostart forms the installer may have used: the Scheduled Task and
        # the HKCU Run-key fallback. schtasks delete is synchronous-safe; the file delete
        # waits out the lock on the running agent.py, then removes the dir tree.
        ps = (
            f"Start-Sleep 5; "
            f"schtasks /delete /tn {SERVICE_NAME} /f 2>$null; "
            f"Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
            f"-Name '{SERVICE_NAME}' -ErrorAction SilentlyContinue; "
            f"Remove-Item -Recurse -Force '{d}','{state_dir}' -ErrorAction SilentlyContinue"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        return
    # POSIX: disable+stop whichever init backend exists, drop the @reboot cron line, then
    # remove our files. Each step best-effort. Run via setsid so the service-stop's
    # process-group/cgroup teardown doesn't take this script with it.
    script = f"""sleep 5
systemctl disable --now {SERVICE_NAME} 2>/dev/null; rm -f /etc/systemd/system/{SERVICE_NAME}.service 2>/dev/null; systemctl daemon-reload 2>/dev/null
systemctl --user disable --now {SERVICE_NAME} 2>/dev/null; rm -f "$HOME/.config/systemd/user/{SERVICE_NAME}.service" 2>/dev/null; systemctl --user daemon-reload 2>/dev/null
rc-update del {SERVICE_NAME} default 2>/dev/null; rc-service {SERVICE_NAME} stop 2>/dev/null; rm -f /etc/init.d/{SERVICE_NAME} 2>/dev/null
(crontab -l 2>/dev/null | grep -v {SERVICE_NAME}) | crontab - 2>/dev/null
rm -rf "{d}" "{state_dir}" 2>/dev/null
"""
    # Prefer systemd-run (transient unit in its own cgroup → immune to our service's
    # cgroup kill); fall back to a new-session sh if it isn't available.
    if shutil.which("systemd-run"):
        subprocess.Popen(
            ["systemd-run", "--collect", "--unit", f"{SERVICE_NAME}-uninstall", "/bin/sh", "-c", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["/bin/sh", "-c", script], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def _execute(cmd):
    kind, payload = cmd["kind"], cmd["payload"]
    if kind == "shell":
        return "done", _run_shell(payload)
    if kind == "rcon":
        return "done", _rcon(payload)
    if kind == "playit":
        return "done", _playit(payload)
    if kind == "uninstall":
        return "done", _uninstall(payload)
    return "failed", {"error": f"unknown kind {kind}"}


def main():
    if not CONTROL_URL:
        _log("CONTROL_URL is required")
        sys.exit(1)
    state = _load_state()
    secret = state["secret"] if state else _enroll()
    if not secret:
        sys.exit(1)
    _log("running; polling for commands")
    backoff = 1
    while True:
        try:
            status, cmd = _http("GET", "/poll", secret=secret)
            if status == 200 and cmd:
                st, res = _execute(cmd)
                _http("POST", "/result", {"id": cmd["id"], "status": st, "result": res}, secret=secret)
                if cmd.get("kind") == "uninstall":
                    # We've reported the result and kicked off the detached self-cleanup;
                    # exit so the (about-to-be-removed) service doesn't relaunch us.
                    _log("uninstalled; exiting")
                    sys.exit(0)
            elif status in (200, 204):
                pass  # no command this cycle
            elif status == 401:
                # The long-lived secret is no longer recognised (e.g. the control
                # plane's DB was reset). Try one re-enroll: succeeds only if a fresh,
                # unused TOKEN is present — then we adopt the new secret and carry on.
                # Otherwise exit so the operator re-pairs, instead of crash-looping
                # a poll the secret can never satisfy.
                _log("unauthorized — secret rejected; attempting re-enroll")
                new_secret = _enroll()
                if not new_secret:
                    _log("re-enroll failed (need a fresh TOKEN from the bot); stopping")
                    sys.exit(1)
                secret = new_secret
            else:
                # 5xx / unexpected status — the control plane is restarting or a
                # Cloudflare edge blip; back off instead of hammering at backoff=1
                # (which would spin a tight loop against a 502). Auto-reconnects once
                # the control plane is healthy again (Phase 6.5).
                _log(f"poll got HTTP {status}; retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            _log(f"poll error ({type(e).__name__}); retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
