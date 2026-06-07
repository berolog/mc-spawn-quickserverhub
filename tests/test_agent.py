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

    def test_playit_stub(self):
        status, _res = agent._execute({"kind": "playit", "payload": {}})
        self.assertEqual(status, "failed")


class RconErrorPathTests(unittest.TestCase):
    def test_rcon_unreachable_is_soft_error(self):
        # Nothing listening on this loopback port → never raises, returns ok=False.
        res = agent._rcon({"rcon_port": 1, "password": "x", "command": "list"})
        self.assertFalse(res["ok"])
        self.assertIn("RCON", res["text"])


if __name__ == "__main__":
    unittest.main()
