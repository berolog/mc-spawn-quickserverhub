"""Tests for the agent's local executor — pure, deterministic pieces.

The full enroll→poll→shell→rcon loop is covered by a live control_api+agent
end-to-end check (see README); here we keep the fast bits that don't need a
control plane or a network.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

# agent.py lives at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent  # noqa: E402


class AgentShellTests(unittest.TestCase):
    def test_run_shell_ok(self):
        res = agent._run_shell({"script": "echo hi && exit 0"})
        self.assertEqual(res["exit"], 0)
        self.assertIn("hi", res["stdout"])

    def test_run_shell_nonzero_exit(self):
        res = agent._run_shell({"script": "echo oops >&2; exit 3"})
        self.assertEqual(res["exit"], 3)
        self.assertIn("oops", res["stderr"])


class ExecuteDispatchTests(unittest.TestCase):
    def test_unknown_kind(self):
        status, _res = agent._execute({"kind": "nope", "payload": {}})
        self.assertEqual(status, "failed")

    def test_playit_dispatch_never_fails_loop(self):
        # playit always reports "done" with a structured status, so a bad op can't
        # kill the poll loop.
        status, res = agent._execute({"kind": "playit", "payload": {"op": "bogus"}})
        self.assertEqual(status, "done")
        self.assertEqual(res["status"], "error")


class PlayitTests(unittest.TestCase):
    def setUp(self):
        self._orig = (agent._playit_secret, agent._playit_api, agent._playit_run)

    def tearDown(self):
        agent._playit_secret, agent._playit_api, agent._playit_run = self._orig

    def test_claim_begin_returns_claim_url(self):
        agent._playit_secret = lambda: None
        agent._playit_api = lambda path, body, secret=None: (True, "WaitingForUserVisit")
        res = agent._playit({"op": "claim_begin"})
        self.assertEqual(res["status"], "begin")
        self.assertTrue(res["url"].startswith("https://playit.gg/claim/"))
        self.assertEqual(len(res["code"]), 10)  # 5 bytes hex

    def test_claim_begin_when_already_linked_reports_address(self):
        agent._playit_secret = lambda: "sek"
        agent._playit_api = lambda path, body, secret=None: (
            True, {"tunnels": [{"display_address": "joy.gl.at.ply.gg:30000"}], "pending": []})
        res = agent._playit({"op": "claim_begin"})
        self.assertEqual(res["status"], "linked")
        self.assertEqual(res["address"], "joy.gl.at.ply.gg:30000")

    def test_status_unlinked(self):
        agent._playit_secret = lambda: None
        self.assertEqual(agent._playit({"op": "status"})["status"], "unlinked")

    def test_status_reads_display_address(self):
        agent._playit_secret = lambda: "sek"
        agent._playit_api = lambda path, body, secret=None: (
            True, {"tunnels": [{"display_address": "abc.ply.gg:25"}], "pending": []})
        res = agent._playit({"op": "status"})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["address"], "abc.ply.gg:25")

    def test_status_no_tunnel_when_only_pending(self):
        agent._playit_secret = lambda: "sek"
        agent._playit_api = lambda path, body, secret=None: (
            True, {"tunnels": [], "pending": [{"name": "mc"}]})
        res = agent._playit({"op": "status"})
        self.assertEqual(res["status"], "no_tunnel")
        self.assertEqual(res["pending"], ["mc"])

    def test_status_auto_creates_tunnel_when_none(self):
        """Linked but tunnel-less ⇒ the agent POSTs /tunnels/create itself (no manual
        dashboard step), then reads back the new address."""
        agent._playit_secret = lambda: "sek"
        calls = []
        state = {"created": False}

        def fake_api(path, body, secret=None):
            calls.append((path, body))
            if path == "/v1/agents/rundata":
                tunnels = [{"display_address": "new.ply.gg:25"}] if state["created"] else []
                return True, {"agent_id": "aid", "tunnels": tunnels, "pending": []}
            if path == "/tunnels/create":
                state["created"] = True
                return True, {"id": "tid"}
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "status", "local_port": 25571})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["address"], "new.ply.gg:25")
        create = [b for p, b in calls if p == "/tunnels/create"][0]
        self.assertEqual(create["tunnel_type"], "minecraft-java")
        self.assertEqual(create["origin"]["data"]["agent_id"], "aid")
        self.assertEqual(create["origin"]["data"]["local_port"], 25571)

    def test_create_tunnel_failure_surfaces_error(self):
        agent._playit_secret = lambda: "sek"

        def fake_api(path, body, secret=None):
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "aid", "tunnels": [], "pending": []}
            if path == "/tunnels/create":
                return False, "RequiresVerifiedAccount"
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "status", "local_port": 25565})
        self.assertEqual(res["status"], "error")
        self.assertIn("RequiresVerifiedAccount", res["error"])


class TeardownTests(unittest.TestCase):
    def setUp(self):
        self._orig = (agent._playit_secret, agent._playit_api)

    def tearDown(self):
        agent._playit_secret, agent._playit_api = self._orig

    def test_delete_tunnels_only_targets_ours(self):
        deleted = []

        def fake_api(path, body, secret=None):
            if path == "/v1/agents/rundata":
                return True, {"tunnels": [
                    {"name": "mc-spawn", "id": "t1"},
                    {"name": "user-made-by-hand", "id": "t2"},  # leave the user's own alone
                ]}
            if path == "/tunnels/delete":
                deleted.append(body["tunnel_id"])
                return True, {}
            return False, None

        agent._playit_api = fake_api
        agent._playit_delete_tunnels("sek")
        self.assertEqual(deleted, ["t1"])

    def test_teardown_is_ok_even_with_no_link(self):
        agent._playit_secret = lambda: None  # nothing linked
        with mock.patch.object(agent.subprocess, "run"), \
                mock.patch.object(agent.os, "remove", side_effect=OSError):
            res = agent._playit({"op": "teardown"})
        self.assertEqual(res["status"], "ok")


class RconErrorPathTests(unittest.TestCase):
    def test_rcon_unreachable_is_soft_error(self):
        # Nothing listening on this loopback port → never raises, returns ok=False.
        res = agent._rcon({"rcon_port": 1, "password": "x", "command": "list"})
        self.assertFalse(res["ok"])
        self.assertIn("RCON", res["text"])


if __name__ == "__main__":
    unittest.main()
