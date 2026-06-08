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
AGENT_STATE (cred file, default /etc/mc-spawn-agent/cred.json).

Thin by design: all Minecraft logic lives in the bot (mc-spawn-bot) — the agent is
a small, auditable executor. This is the client half of the system and lives in its
own repository so users can inspect exactly what they install; a Go rewrite can be a
drop-in (same HTTP protocol).
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "").strip()
STATE_PATH = os.environ.get("AGENT_STATE", "/etc/mc-spawn-agent/cred.json")

POLL_TIMEOUT = 40        # > server long-poll (25s)
SHELL_TIMEOUT = 600      # provisioning (docker pull) can be slow
# urllib's default "Python-urllib/x.y" UA is on Cloudflare's Browser Integrity
# Check banlist (HTTP 403, error 1010) — the control plane sits behind a proxied
# Cloudflare hostname, so send a real product UA or enroll/poll never get through.
USER_AGENT = "mc-spawn-agent/1.0"


def _log(msg):
    print(f"[mc-spawn-agent] {msg}", flush=True)


def _http(method, path, body=None, secret=None, timeout=POLL_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CONTROL_URL + path, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
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
        _log("no stored creds and no TOKEN env — cannot enroll")
        sys.exit(1)
    status, data = _http("POST", "/enroll", {"token": TOKEN}, timeout=20)
    if status != 200 or not data:
        _log(f"enroll failed (HTTP {status})")
        sys.exit(1)
    _save_state({"machine_id": data["machine_id"], "secret": data["secret"]})
    _log(f"enrolled as machine {data['machine_id']}")
    return data["secret"]


# ---- command execution ----

def _run_shell(payload):
    try:
        p = subprocess.run(
            ["bash", "-c", payload["script"]],
            capture_output=True, text=True, timeout=SHELL_TIMEOUT,
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
PLAYIT_VERSION = "mc-spawn-agent 1"


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
            "agent_id": agent_id, "local_ip": "127.0.0.1", "local_port": int(local_port)}},
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


def _playit_ensure_tunnel(secret, local_port):
    """Ensure THIS port has its own tunnel; auto-create one (named per the port) if it
    doesn't. Returns (address_or_None, pending_bool, error_or_None). A freshly created
    tunnel has no address yet — the caller polls _playit_address for it."""
    data = _playit_rundata(secret)
    if data is None:
        return None, False, "playit недоступен"
    addr, pending = _port_address(data, local_port)
    if addr or pending:
        return addr, pending, None  # this port already has/awaits its tunnel
    if not local_port:
        return None, False, None  # nothing to create against
    agent_id = data.get("agent_id")
    if not agent_id:
        return None, False, "no agent_id"
    ok, err = _playit_create_tunnel(secret, agent_id, local_port)
    if not ok:
        return None, False, err or "не удалось создать туннель"
    return None, False, None  # created; address appears within seconds


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
        subprocess.run(["docker", "rm", "-f", PLAYIT_CONTAINER],
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
        subprocess.run(["docker", "rm", "-f", PLAYIT_CONTAINER],
                       capture_output=True, text=True, timeout=30)
        return subprocess.run(
            ["docker", "run", "-d", "--name", PLAYIT_CONTAINER, "--restart", "unless-stopped",
             "--network", "host", "-e", "SECRET_KEY=" + secret, PLAYIT_IMAGE],
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
        if state != "UserAccepted":
            return {"status": "waiting"}
        ok, data = _playit_api("/claim/exchange", {"code": code})
        if not ok or not isinstance(data, dict) or not data.get("secret_key"):
            return {"status": "waiting"}
        _save_playit_secret(data["secret_key"])
        return {"status": "accepted"}
    if op == "playit_start":
        secret = _playit_secret()  # may have just been saved by claim_poll
        if not secret:
            return {"status": "unlinked"}
        if not _playit_run(secret):
            return {"status": "error", "error": "не удалось запустить playit (docker)"}
        _addr, _pending, cerr = _playit_ensure_tunnel(secret, local_port)
        if cerr:
            return {"status": "error", "error": cerr}
        return {"status": "ok"}  # tunnel ensured; bot polls `status` for the address
    if op == "status":
        if not secret:
            return {"status": "unlinked"}
        addr, _pending, cerr = _playit_ensure_tunnel(secret, local_port)
        if cerr:
            return {"status": "error", "error": cerr}
        return {"status": "ok" if addr else "no_tunnel", "address": addr}
    return {"status": "error", "error": f"unknown playit op {op}"}


def _execute(cmd):
    kind, payload = cmd["kind"], cmd["payload"]
    if kind == "shell":
        return "done", _run_shell(payload)
    if kind == "rcon":
        return "done", _rcon(payload)
    if kind == "playit":
        return "done", _playit(payload)
    return "failed", {"error": f"unknown kind {kind}"}


def main():
    if not CONTROL_URL:
        _log("CONTROL_URL is required")
        sys.exit(1)
    state = _load_state()
    secret = state["secret"] if state else _enroll()
    _log("running; polling for commands")
    backoff = 1
    while True:
        try:
            status, cmd = _http("GET", "/poll", secret=secret)
            if status == 200 and cmd:
                st, res = _execute(cmd)
                _http("POST", "/result", {"id": cmd["id"], "status": st, "result": res}, secret=secret)
            elif status in (200, 204):
                pass  # no command this cycle
            elif status == 401:
                _log("unauthorized — secret rejected; stopping")
                sys.exit(1)
            backoff = 1
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            _log(f"poll error ({type(e).__name__}); retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
