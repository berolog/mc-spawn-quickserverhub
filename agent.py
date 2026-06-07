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


def _playit_address(secret):
    """(public_address_or_None, [pending_tunnel_names]) from the user's playit account."""
    ok, data = _playit_api("/v1/agents/rundata", {}, secret=secret)
    if not ok or not isinstance(data, dict):
        return None, []
    addrs = [t.get("display_address") for t in data.get("tunnels", []) if t.get("display_address")]
    pending = [t.get("name", "?") for t in data.get("pending", [])]
    return (addrs[0] if addrs else None), pending


def _playit_run(secret):
    """(Re)start the playit agent under the user's secret via docker on the host network
    (so it reaches 127.0.0.1:<mc_port>). Idempotent."""
    subprocess.run(["docker", "rm", "-f", PLAYIT_CONTAINER],
                   capture_output=True, text=True, timeout=30)
    return subprocess.run(
        ["docker", "run", "-d", "--name", PLAYIT_CONTAINER, "--restart", "unless-stopped",
         "--network", "host", "-e", "SECRET_KEY=" + secret, PLAYIT_IMAGE],
        capture_output=True, text=True, timeout=120,
    ).returncode == 0


def _playit(payload):
    """Link/report the playit public address. Two-step (the user must approve in a
    browser between the steps): claim_begin -> show URL; claim_finish -> wait for
    approval, run playit, return the address. Returns a structured status; never raises."""
    op = payload.get("op")
    secret = _playit_secret()
    if op == "claim_begin":
        if secret:  # already linked — report current state instead of re-claiming
            addr, pending = _playit_address(secret)
            return {"status": "linked", "address": addr, "pending": pending}
        code = os.urandom(5).hex()  # matches playit's claim-code format (5 bytes hex)
        ok, _ = _playit_api(
            "/claim/setup", {"code": code, "agent_type": "self-managed", "version": PLAYIT_VERSION})
        if not ok:
            return {"status": "error", "error": "playit недоступен"}
        return {"status": "begin", "code": code, "url": "https://playit.gg/claim/" + code}
    if op == "claim_finish":
        code = payload.get("code", "")
        if not secret:
            deadline = time.time() + 100  # bounded wait for the browser approval
            while time.time() < deadline:
                _ok, state = _playit_api(
                    "/claim/setup",
                    {"code": code, "agent_type": "self-managed", "version": PLAYIT_VERSION})
                if state == "UserAccepted":
                    break
                if state == "UserRejected":
                    return {"status": "rejected"}
                time.sleep(3)
            else:
                return {"status": "waiting"}
            ok, data = _playit_api("/claim/exchange", {"code": code})
            if not ok or not isinstance(data, dict) or not data.get("secret_key"):
                return {"status": "waiting"}
            secret = data["secret_key"]
            _save_playit_secret(secret)
        if not _playit_run(secret):
            return {"status": "error", "error": "не удалось запустить playit (docker)"}
        deadline = time.time() + 60  # let playit register the tunnel, then read its address
        while time.time() < deadline:
            addr, pending = _playit_address(secret)
            if addr:
                return {"status": "ok", "address": addr, "pending": pending}
            time.sleep(4)
        return {"status": "no_tunnel", "pending": pending}
    if op == "status":
        if not secret:
            return {"status": "unlinked"}
        addr, pending = _playit_address(secret)
        return {"status": "ok" if addr else "no_tunnel", "address": addr, "pending": pending}
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
