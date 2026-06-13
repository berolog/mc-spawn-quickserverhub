"""Tests for the secure v2 agent — pure, deterministic (no control plane, no Docker).

Focus: the security boundary. Malicious / malformed commands must be DENIED or INVALID, the
workspace path jail holds, local policy gates the dangerous capabilities, and replay/expiry
protection works. Capability argv shape is checked with run_allowed mocked (no real engine).
"""
import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# agent.py lives at the repo root; isolate state/policy onto temp paths so tests never read or
# write a real install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent  # noqa: E402


def envelope(action, params, *, request_id="r", issued_delta=0, expires_delta=120):
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "protocol_version": 2,
        "request_id": request_id,
        "action": action,
        "params": params,
        "issued_at": (now + datetime.timedelta(seconds=issued_delta)).isoformat(),
        "expires_at": (now + datetime.timedelta(seconds=expires_delta)).isoformat(),
    }


def set_policy(**overrides):
    # Merge over the current cache (set in setUp) so a per-test override doesn't reset the
    # jailed workspace_root back to the real ~/.mc-spawn default.
    base = agent._POLICY_CACHE or agent._DEFAULT_POLICY
    agent._POLICY_CACHE = {**base, **overrides}


class _CP:
    """Stand-in for subprocess.CompletedProcess."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SecurityBase(unittest.TestCase):
    def setUp(self):
        agent._REPLAY.clear()
        # Jail the workspace (so the audit log doesn't write to the real ~/.mc-spawn).
        self._ws = tempfile.TemporaryDirectory()
        set_policy(workspace_root=self._ws.name)
        self.addCleanup(self._ws.cleanup)
        self.addCleanup(lambda: setattr(agent, "_POLICY_CACHE", None))
        self.addCleanup(agent._REPLAY.clear)

    def run_cmd(self, action, params, **kw):
        return agent.process_command(envelope(action, params, **kw))


class RejectionTests(SecurityBase):
    """The spec §21.1 malicious-command matrix — all must be denied/invalid."""

    def test_legacy_shell_denied(self):
        r = self.run_cmd("shell", {"script": "id"})
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "unknown_action")

    def test_unknown_create_field_invalid(self):
        r = self.run_cmd("minecraft.server.create", {
            "server_id": "main", "kind": "VANILLA", "version": "1.21", "ram_mb": 2048,
            "port": 25565, "rcon_port": 25575, "rcon_password": "abcdefgh", "extra": "bad"})
        self.assertEqual(r["status"], "invalid")
        self.assertEqual(r["result"]["reason"], "unknown_field:extra")

    def test_server_id_path_traversal_invalid(self):
        r = self.run_cmd("minecraft.server.logs", {"server_id": "../../..", "lines": 100})
        self.assertEqual(r["status"], "invalid")
        self.assertIn("server_id", r["result"]["reason"])

    def test_ram_out_of_range_invalid(self):
        r = self.run_cmd("minecraft.server.create", {
            "server_id": "main", "kind": "VANILLA", "version": "1.21", "ram_mb": 999999,
            "port": 25565, "rcon_port": 25575, "rcon_password": "abcdefgh"})
        self.assertEqual(r["status"], "invalid")
        self.assertEqual(r["result"]["reason"], "ram_mb:above_max")

    def test_raw_rcon_action_denied(self):
        r = self.run_cmd("minecraft.rcon.raw", {"command": "op attacker"})
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "unknown_action")

    def test_update_from_url_denied(self):
        r = self.run_cmd("agent.update_from_url", {"url": "https://evil.test/a.py"})
        self.assertEqual(r["status"], "denied")

    def test_missing_required_field_invalid(self):
        r = self.run_cmd("minecraft.player.whitelist_add", {"server_id": "main"})  # no player
        self.assertEqual(r["status"], "invalid")
        self.assertEqual(r["result"]["reason"], "missing:player")

    def test_bad_player_name_invalid(self):
        r = self.run_cmd("minecraft.player.kick", {"server_id": "main", "player": "a b; rm -rf"})
        self.assertEqual(r["status"], "invalid")

    def test_bad_protocol_version_invalid(self):
        e = envelope("agent.health", {})
        e["protocol_version"] = 1
        r = agent.process_command(e)
        self.assertEqual(r["status"], "invalid")
        self.assertEqual(r["result"]["reason"], "bad_protocol_version")


class PolicyTests(SecurityBase):
    def test_action_disabled_by_policy_denied(self):
        set_policy(allowed_actions=["agent.health"])  # start not allowed
        r = self.run_cmd("minecraft.server.start", {"server_id": "main"})
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "action_disabled_by_policy")

    def test_console_exec_denied_by_default(self):
        r = self.run_cmd("minecraft.server.console_exec", {"server_id": "main", "command": "op x"})
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "raw_console_disabled")

    def test_console_exec_allowed_when_owner_opts_in(self):
        set_policy(allow_raw_rcon=True)
        with mock.patch.object(agent, "_rcon_cli", return_value="done"):
            r = self.run_cmd("minecraft.server.console_exec", {"server_id": "main", "command": "op x"})
        self.assertEqual(r["status"], "ok")

    def test_delete_denied_when_policy_off(self):
        set_policy(allow_server_delete=False)
        r = self.run_cmd("minecraft.server.delete", {"server_id": "main"})
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "server_delete_disabled")

    def test_uninstall_denied_when_policy_off(self):
        set_policy(allow_agent_uninstall=False)
        r = self.run_cmd("agent.uninstall", {})
        self.assertEqual(r["status"], "denied")

    def test_port_outside_range_denied(self):
        set_policy(allowed_port_range=[25565, 25565])
        with mock.patch.object(agent, "_ensure_engine_ready"), \
             mock.patch.object(agent, "run_allowed", return_value=_CP(0)):
            r = self.run_cmd("minecraft.server.create", {
                "server_id": "main", "kind": "VANILLA", "version": "1.21", "ram_mb": 1024,
                "port": 25600, "rcon_port": 25575, "rcon_password": "abcdefgh"})
        self.assertEqual(r["status"], "denied")
        self.assertIn("port_not_allowed", r["result"]["reason"])

    def test_capabilities_reflects_policy(self):
        set_policy(allowed_actions=["minecraft.server.start"], allow_raw_rcon=False)
        r = self.run_cmd("agent.capabilities", {})
        self.assertEqual(r["status"], "ok")
        self.assertIn("minecraft.server.start", r["result"]["allowed_actions"])
        self.assertIn("agent.health", r["result"]["allowed_actions"])  # always-allowed
        self.assertFalse(r["result"]["limits"]["allow_raw_rcon"])


class ReplayExpiryTests(SecurityBase):
    def test_expired_denied(self):
        r = self.run_cmd("agent.health", {}, issued_delta=-600, expires_delta=-540)
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "expired")

    def test_future_denied(self):
        r = self.run_cmd("agent.health", {}, issued_delta=600, expires_delta=900)
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["result"]["reason"], "issued_in_future")

    def test_replay_returns_cached_result(self):
        first = self.run_cmd("agent.health", {}, request_id="dup")
        second = self.run_cmd("agent.health", {}, request_id="dup")
        self.assertEqual(first, second)  # cached envelope returned, not re-executed


class PathJailTests(unittest.TestCase):
    def test_parent_escape(self):
        with self.assertRaises(agent.SecurityError):
            agent.safe_join("/tmp/mcsroot", "..", "etc")

    def test_absolute_escape(self):
        with self.assertRaises(agent.SecurityError):
            agent.safe_join("/tmp/mcsroot", "/etc/passwd")

    def test_nested_ok(self):
        out = agent.safe_join("/tmp/mcsroot", "backups", "main")
        self.assertTrue(out.endswith("/backups/main"))

    def test_path_separator_in_segment_escape(self):
        with self.assertRaises(agent.SecurityError):
            agent.safe_join("/tmp/mcsroot", "../../secret")


class CapabilityArgvTests(SecurityBase):
    """The hardcoded capabilities must build a SAFE fixed argv — RCON on loopback, container
    name derived from server_id, semantic RCON via `docker exec rcon-cli` (no password)."""

    def _run(self, action, params):
        self.calls = []

        def fake_run_allowed(rt, args, timeout=600):
            self.calls.append((rt, args))
            return _CP(0, stdout="ok")

        with mock.patch.object(agent, "_ensure_engine_ready"), \
             mock.patch.object(agent, "run_allowed", side_effect=fake_run_allowed):
            return agent.process_command(envelope(action, params))

    def test_create_argv_binds_rcon_to_loopback_and_derives_name(self):
        r = self._run("minecraft.server.create", {
            "server_id": "7", "kind": "PAPER", "version": "1.21", "ram_mb": 2048,
            "port": 25565, "rcon_port": 25575, "rcon_password": "abcdefgh"})
        self.assertEqual(r["status"], "ok")
        run_call = next(a for _rt, a in self.calls if a and a[0] == "run")
        joined = " ".join(run_call)
        self.assertIn("--name mcspawn-server-7", joined)              # agent-derived name
        self.assertIn("127.0.0.1:25575:25575", joined)               # RCON loopback only
        self.assertIn("EULA=TRUE", joined)
        self.assertIn("-v mcspawn-server-7_data:/data", joined)       # named world volume
        self.assertIn("itzg/minecraft-server", run_call)

    def test_whitelist_add_uses_rcon_cli_exec(self):
        r = self._run("minecraft.player.whitelist_add", {"server_id": "7", "player": "Steve"})
        self.assertEqual(r["status"], "ok")
        _rt, args = self.calls[-1]
        self.assertEqual(args, ["exec", "mcspawn-server-7", "rcon-cli", "whitelist", "add", "Steve"])

    def test_reconcile_status_shape(self):
        def fake_run_allowed(rt, args, timeout=600):
            if args[0] == "info":
                return _CP(0)
            if args[:2] == ["inspect", "-f"]:
                name = args[-1]
                return _CP(0, stdout="running" if name == "mcspawn-server-1" else "missing")
            return _CP(0)

        with mock.patch.object(agent, "_ensure_engine_ready"), \
             mock.patch.object(agent, "run_allowed", side_effect=fake_run_allowed):
            r = agent.process_command(envelope("minecraft.server.reconcile_status",
                                               {"server_ids": ["1", "2"]}))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["result"]["engine"], "up")
        self.assertEqual(r["result"]["servers"], {"1": "running", "2": "missing"})


class RunAllowedTests(unittest.TestCase):
    def test_rejects_disallowed_engine(self):
        with self.assertRaises(agent.SecurityError):
            agent.run_allowed("rm", ["-rf", "/"])

    def test_rejects_non_string_argv(self):
        with self.assertRaises(agent.SecurityError):
            agent.run_allowed("docker", ["ps", 5])

    def test_engine_lookup_checks_system_paths(self):
        def isfile(path):
            return path == "/usr/bin/docker"

        with mock.patch.object(agent.shutil, "which", return_value=None), \
             mock.patch.object(agent.os.path, "isfile", side_effect=isfile), \
             mock.patch.object(agent.os, "access", return_value=True):
            self.assertEqual(agent._find_executable("docker"), "/usr/bin/docker")

    def test_engine_lookup_checks_snap_path(self):
        def isfile(path):
            return path == "/snap/bin/docker"

        with mock.patch.object(agent.shutil, "which", return_value=None), \
             mock.patch.object(agent.os.path, "isfile", side_effect=isfile), \
             mock.patch.object(agent.os, "access", return_value=True):
            self.assertEqual(agent._find_executable("docker"), "/snap/bin/docker")

    def test_engine_lookup_prefers_installer_docker_bin(self):
        def isfile(path):
            return path == "/custom/bin/docker"

        with mock.patch.dict(agent.os.environ, {"DOCKER_BIN": "/custom/bin/docker"}), \
             mock.patch.object(agent.os.path, "isfile", side_effect=isfile), \
             mock.patch.object(agent.os, "access", return_value=True):
            self.assertEqual(agent._find_executable("docker"), "/custom/bin/docker")

    def test_engine_lookup_error_lists_checked_paths(self):
        with mock.patch.dict(agent.os.environ, {"PATH": "/service/path", "DOCKER_BIN": "/custom/docker"}):
            msg = agent._engine_lookup_error("docker")
        self.assertIn("container_engine_unavailable:docker", msg)
        self.assertIn("path=/service/path", msg)
        self.assertIn("/custom/docker:", msg)
        self.assertIn("/usr/bin/docker:", msg)


class LogFormatTests(unittest.TestCase):
    def test_key_value_log_line_omits_empty_fields(self):
        line = agent._log_line("info", "agent_start", {"path": "/tmp/a b", "debug": None})
        self.assertIn('level="info"', line)
        self.assertIn('event="agent_start"', line)
        self.assertIn('path="/tmp/a b"', line)
        self.assertNotIn("debug=", line)


class HealthTests(SecurityBase):
    def test_health_ok(self):
        r = self.run_cmd("agent.health", {})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["result"]["agent_version"], agent.AGENT_VERSION)


if __name__ == "__main__":
    unittest.main()
