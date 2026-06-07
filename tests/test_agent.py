"""Tests for the agent's local executor — pure, deterministic pieces.

The full enroll→poll→shell→rcon loop is covered by a live control_api+agent
end-to-end check (see README); here we keep the fast bits that don't need a
control plane or a network.
"""
import sys
import unittest
from pathlib import Path

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


class RconErrorPathTests(unittest.TestCase):
    def test_rcon_unreachable_is_soft_error(self):
        # Nothing listening on this loopback port → never raises, returns ok=False.
        res = agent._rcon({"rcon_port": 1, "password": "x", "command": "list"})
        self.assertFalse(res["ok"])
        self.assertIn("RCON", res["text"])


if __name__ == "__main__":
    unittest.main()
