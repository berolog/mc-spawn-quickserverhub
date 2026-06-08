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

    def test_claim_begin_when_already_linked(self):
        agent._playit_secret = lambda: "sek"
        # No address work here anymore — the bot drives the address stage next.
        self.assertEqual(agent._playit({"op": "claim_begin"})["status"], "linked")

    def test_claim_poll_accepted_saves_secret(self):
        agent._playit_secret = lambda: None

        def fake_api(path, body, secret=None):
            if path == "/claim/setup":
                return True, "UserAccepted"
            if path == "/claim/exchange":
                return True, {"secret_key": "newsek"}
            return False, None

        agent._playit_api = fake_api
        with mock.patch.object(agent, "_save_playit_secret") as save:
            res = agent._playit({"op": "claim_poll", "code": "abcdef"})
        self.assertEqual(res["status"], "accepted")
        save.assert_called_once_with("newsek")

    def test_claim_poll_waiting_then_rejected(self):
        agent._playit_secret = lambda: None
        agent._playit_api = lambda path, body, secret=None: (True, "WaitingForUserVisit")
        self.assertEqual(agent._playit({"op": "claim_poll", "code": "x"})["status"], "waiting")
        agent._playit_api = lambda path, body, secret=None: (True, "UserRejected")
        self.assertEqual(agent._playit({"op": "claim_poll", "code": "x"})["status"], "rejected")

    def test_playit_start_ensures_tunnel_fast(self):
        agent._playit_secret = lambda: "sek"
        agent._playit_run = lambda secret: True
        calls = []

        def fake_api(path, body, secret=None):
            calls.append(path)
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "aid", "tunnels": [], "pending": []}
            if path == "/tunnels/create":
                return True, {"id": "tid"}
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "playit_start", "local_port": 25570})
        self.assertEqual(res["status"], "ok")  # returns once the tunnel is ensured
        self.assertIn("/tunnels/create", calls)  # no address-wait loop here

    def test_status_unlinked(self):
        agent._playit_secret = lambda: None
        self.assertEqual(agent._playit({"op": "status"})["status"], "unlinked")

    def test_status_reads_this_ports_address_not_the_first(self):
        agent._playit_secret = lambda: "sek"
        agent._playit_api = lambda path, body, secret=None: (True, {
            "agent_id": "a", "pending": [], "tunnels": [
                {"name": "mc-spawn-25565", "display_address": "a.ply.gg:1"},
                {"name": "mc-spawn-25566", "display_address": "b.ply.gg:2"},
            ]})
        res = agent._playit({"op": "status", "local_port": 25566})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["address"], "b.ply.gg:2")  # the matching port, not just the first

    def test_status_no_tunnel_right_after_create(self):
        agent._playit_secret = lambda: "sek"

        def fake_api(path, body, secret=None):
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "a", "tunnels": [], "pending": []}
            if path == "/tunnels/create":
                return True, {"id": "t"}
            return False, None

        agent._playit_api = fake_api  # created but address not yet present
        self.assertEqual(agent._playit({"op": "status", "local_port": 25565})["status"], "no_tunnel")

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

    def test_create_tunnel_named_and_routed_per_port(self):
        captured = {}

        def fake_api(path, body, secret=None):
            if path == "/tunnels/create":
                captured.update(body)
                return True, {"id": "t"}
            return False, None

        agent._playit_api = fake_api
        ok, _ = agent._playit_create_tunnel("sek", "aid", 25570)
        self.assertTrue(ok)
        self.assertEqual(captured["name"], "mc-spawn-25570")
        self.assertEqual(captured["origin"]["data"]["local_port"], 25570)


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

    def test_delete_tunnels_for_one_port_only(self):
        deleted = []

        def fake_api(path, body, secret=None):
            if path == "/v1/agents/rundata":
                return True, {"tunnels": [
                    {"name": "mc-spawn-25565", "id": "t1"},
                    {"name": "mc-spawn-25566", "id": "t2"},
                ]}
            if path == "/tunnels/delete":
                deleted.append(body["tunnel_id"])
                return True, {}
            return False, None

        agent._playit_api = fake_api
        agent._playit_delete_tunnels("sek", local_port=25566)
        self.assertEqual(deleted, ["t2"])  # only the matching port's tunnel

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
