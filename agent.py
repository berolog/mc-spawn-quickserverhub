#!/usr/bin/env python3
"""mc-spawn agent — runs on the user's own machine. STDLIB ONLY (python3).

Outbound only: it dials the control API (no inbound ports on this box), redeems a
one-time enroll token for a long-lived secret, then long-polls for commands and
executes them locally:
  * shell  — run a shell script (provisioning/lifecycle issued by the bot);
  * rcon   — talk to a Minecraft RCON on 127.0.0.1 (no tunnel needed; we're local);
  * playit — (Phase 3) bring up the public play address.

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


def _log(msg):
    print(f"[mc-spawn-agent] {msg}", flush=True)


def _http(method, path, body=None, secret=None, timeout=POLL_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CONTROL_URL + path, data=data, method=method)
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


def _execute(cmd):
    kind, payload = cmd["kind"], cmd["payload"]
    if kind == "shell":
        return "done", _run_shell(payload)
    if kind == "rcon":
        return "done", _rcon(payload)
    if kind == "playit":
        return "failed", {"ok": False, "text": "playit not implemented yet"}
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
