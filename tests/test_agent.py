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


class ShellLoggingTests(unittest.TestCase):
    """A failing command must log its stderr (so 'Cannot connect to Podman' is visible)
    but NEVER the script text — the provisioning script carries the RCON password."""

    def test_failure_logs_stderr_not_script(self):
        logged = []
        with mock.patch.object(agent, "_log", lambda m: logged.append(m)):
            agent._run_shell({"script": "echo SECRETPASS >&2; exit 7"})
        blob = "\n".join(logged)
        self.assertIn("exit=7", blob)
        self.assertIn("SECRETPASS", blob)          # stderr tail is shown
        self.assertNotIn("echo SECRETPASS", blob)   # the script itself is not

    def test_success_is_quiet_without_debug(self):
        logged = []
        with mock.patch.object(agent, "DEBUG", False), \
             mock.patch.object(agent, "_log", lambda m: logged.append(m)):
            agent._run_shell({"script": "exit 0"})
        self.assertEqual(logged, [])


class EngineReadyTests(unittest.TestCase):
    def setUp(self):
        agent._ENGINE_READY = False
        agent._RUNTIME_CACHE = None
        self.addCleanup(lambda: setattr(agent, "_ENGINE_READY", False))
        self.addCleanup(lambda: setattr(agent, "_RUNTIME_CACHE", None))

    def test_noop_off_windows(self):
        with mock.patch.object(agent, "IS_WINDOWS", False), \
             mock.patch.object(agent.subprocess, "run") as run:
            agent._ensure_engine_ready()
        run.assert_not_called()

    def test_windows_podman_starts_machine_once(self):
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.object(agent, "_runtime", return_value="podman"), \
             mock.patch.object(agent.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            agent._ensure_engine_ready()
            agent._ensure_engine_ready()   # cached — must not start twice
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0], ["podman", "machine", "start"])


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

    def test_claim_poll_distinguishes_visit_vs_approve(self):
        # playit's claim is two site steps; the agent surfaces which one is pending.
        agent._playit_secret = lambda: None
        agent._playit_api = lambda p, b, secret=None: (True, "WaitingForUserVisit")
        r = agent._playit({"op": "claim_poll", "code": "x"})
        self.assertEqual((r["status"], r["stage"]), ("waiting", "visit"))
        agent._playit_api = lambda p, b, secret=None: (True, "WaitingForUser")
        r = agent._playit({"op": "claim_poll", "code": "x"})
        self.assertEqual((r["status"], r["stage"]), ("waiting", "approve"))

    def test_claim_setup_sends_playit_version_shape(self):
        # A non-"playit <semver>" version makes /tunnels/create fail AgentVersionTooOld.
        captured = {}

        def fake_api(path, body, secret=None):
            captured.update(body)
            return True, "WaitingForUserVisit"

        agent._playit_secret = lambda: None
        agent._playit_api = fake_api
        agent._playit({"op": "claim_poll", "code": "abc"})
        self.assertTrue(captured["version"].startswith("playit "))
        self.assertEqual(captured["agent_type"], "self-managed")

    def test_playit_start_only_runs_docker_no_tunnel(self):
        # playit_start just ensures the container; it must NOT create a tunnel (that's
        # ensure_tunnel's retryable job).
        agent._playit_secret = lambda: "sek"
        ran = {}
        agent._playit_run = lambda secret: ran.setdefault("ran", True) or True
        calls = []
        agent._playit_api = lambda path, body, secret=None: (calls.append(path), (False, None))[1]
        res = agent._playit({"op": "playit_start", "local_port": 25570})
        self.assertEqual(res["status"], "ok")
        self.assertTrue(ran.get("ran"))
        self.assertNotIn("/tunnels/create", calls)

    def test_ensure_tunnel_creates_when_absent(self):
        agent._playit_secret = lambda: "sek"
        calls = []

        def fake_api(path, body, secret=None):
            calls.append(path)
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "aid", "tunnels": [], "pending": []}
            if path == "/tunnels/create":
                return True, {"id": "tid"}
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "ensure_tunnel", "local_port": 25570})
        self.assertEqual(res["status"], "created")
        self.assertIn("/tunnels/create", calls)

    def test_ensure_tunnel_idempotent_when_present(self):
        # If this port already has a tunnel, ensure must NOT create another (no duplicates).
        agent._playit_secret = lambda: "sek"
        calls = []

        def fake_api(path, body, secret=None):
            calls.append(path)
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "aid", "pending": [],
                              "tunnels": [{"name": "mc-spawn-25570", "display_address": "x.ply.gg:1"}]}
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "ensure_tunnel", "local_port": 25570})
        self.assertEqual(res["status"], "exists")
        self.assertEqual(res["address"], "x.ply.gg:1")
        self.assertNotIn("/tunnels/create", calls)

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

    def test_status_is_read_only_never_creates(self):
        # status must NOT create tunnels — otherwise the bot's poll loop spawns duplicates.
        agent._playit_secret = lambda: "sek"
        calls = []

        def fake_api(path, body, secret=None):
            calls.append(path)
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "a", "tunnels": [], "pending": []}
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "status", "local_port": 25565})
        self.assertEqual(res["status"], "no_tunnel")
        self.assertNotIn("/tunnels/create", calls)  # the bug fix: no creation on status

    def test_ensure_tunnel_hard_error_surfaces(self):
        # Real create failures (e.g. guest account) surface via ensure_tunnel as an error.
        agent._playit_secret = lambda: "sek"

        def fake_api(path, body, secret=None):
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "aid", "tunnels": [], "pending": []}
            if path == "/tunnels/create":
                return False, "RequiresVerifiedAccount"
            return False, None

        agent._playit_api = fake_api
        res = agent._playit({"op": "ensure_tunnel", "local_port": 25565})
        self.assertEqual(res["status"], "error")
        self.assertIn("RequiresVerifiedAccount", res["error"])

    def test_ensure_tunnel_version_too_old_is_connecting_retryable(self):
        # While the container is still connecting, create returns AgentVersionTooOld —
        # ensure_tunnel reports "connecting" (not error) so the bot keeps retrying create.
        agent._playit_secret = lambda: "sek"

        def fake_api(path, body, secret=None):
            if path == "/v1/agents/rundata":
                return True, {"agent_id": "aid", "tunnels": [], "pending": []}
            if path == "/tunnels/create":
                return False, "AgentVersionTooOld"
            return False, None

        agent._playit_api = fake_api
        self.assertEqual(
            agent._playit({"op": "ensure_tunnel", "local_port": 25565})["status"], "connecting")

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


class UninstallTests(unittest.TestCase):
    def test_uninstall_purges_containers_teardowns_playit_and_spawns_cleanup(self):
        scripts = []
        with mock.patch.object(agent, "_run_shell",
                               side_effect=lambda p: scripts.append(p["script"]) or {"exit": 0}), \
             mock.patch.object(agent, "_playit_teardown") as teardown, \
             mock.patch.object(agent, "_spawn_self_cleanup") as cleanup:
            res = agent._uninstall({"containers": ["mcw-1", "mcw-2"]})
        self.assertEqual(res["status"], "ok")
        teardown.assert_called_once()
        cleanup.assert_called_once()
        blob = "\n".join(scripts)
        # purge runs through the shell (so it reaches the same engine, incl. WSL on Windows)
        self.assertIn("rm -f mcw-1", blob)
        self.assertIn("volume rm mcw-1_data", blob)
        self.assertIn("rm -f mcw-2", blob)
        self.assertIn("${MCSPAWN_RT:-docker}", blob)

    def test_execute_dispatches_uninstall(self):
        with mock.patch.object(agent, "_uninstall", return_value={"status": "ok"}) as u:
            st, res = agent._execute({"kind": "uninstall", "payload": {"containers": []}})
        self.assertEqual((st, res["status"]), ("done", "ok"))
        u.assert_called_once()

    def test_self_cleanup_posix_setsid_script_targets_all_backends(self):
        popen = []
        with mock.patch.object(agent, "IS_WINDOWS", False), \
             mock.patch.object(agent.shutil, "which", return_value=None), \
             mock.patch.object(agent.subprocess, "Popen",
                               side_effect=lambda *a, **k: popen.append((a, k))):
            agent._spawn_self_cleanup()
        (argv,), kwargs = popen[0]
        self.assertEqual(argv[0], "/bin/sh")
        self.assertTrue(kwargs.get("start_new_session"))
        script = argv[2]
        for needle in ("systemctl disable --now", "rc-update del", "crontab", "rm -rf"):
            self.assertIn(needle, script)

    def test_self_cleanup_prefers_systemd_run_when_present(self):
        popen = []
        with mock.patch.object(agent, "IS_WINDOWS", False), \
             mock.patch.object(agent.shutil, "which", return_value="/usr/bin/systemd-run"), \
             mock.patch.object(agent.subprocess, "Popen",
                               side_effect=lambda *a, **k: popen.append((a, k))):
            agent._spawn_self_cleanup()
        argv = popen[0][0][0]
        self.assertEqual(argv[0], "systemd-run")

    def test_self_cleanup_windows_uses_schtasks_via_powershell(self):
        popen = []
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.object(agent.subprocess, "Popen",
                               side_effect=lambda *a, **k: popen.append((a, k))):
            agent._spawn_self_cleanup()
        argv = popen[0][0][0]
        self.assertEqual(argv[0], "powershell")
        self.assertIn("schtasks /delete", " ".join(argv))


class _Stop(Exception):
    """Sentinel to break main()'s infinite poll loop in tests (it is not one of the
    network errors main catches, so it propagates straight out)."""


class EnrollTests(unittest.TestCase):
    def test_enroll_returns_none_without_token(self):
        with mock.patch.object(agent, "TOKEN", ""):
            self.assertIsNone(agent._enroll())

    def test_enroll_returns_none_on_http_error(self):
        with mock.patch.object(agent, "TOKEN", "tok"), \
             mock.patch.object(agent, "_http", return_value=(401, None)):
            self.assertIsNone(agent._enroll())

    def test_enroll_saves_and_returns_secret(self):
        with mock.patch.object(agent, "TOKEN", "tok"), \
             mock.patch.object(agent, "_http", return_value=(200, {"machine_id": 7, "secret": "s3"})), \
             mock.patch.object(agent, "_save_state") as save:
            self.assertEqual(agent._enroll(), "s3")
            save.assert_called_once_with({"machine_id": 7, "secret": "s3"})


class MainReenrollTests(unittest.TestCase):
    def test_exits_when_no_creds_and_no_token(self):
        with mock.patch.object(agent, "CONTROL_URL", "http://c"), \
             mock.patch.object(agent, "_load_state", return_value=None), \
             mock.patch.object(agent, "_enroll", return_value=None):
            with self.assertRaises(SystemExit):
                agent.main()

    def test_401_reenrolls_and_continues_with_new_secret(self):
        polls = []

        def fake_http(method, path, body=None, secret=None, timeout=agent.POLL_TIMEOUT):
            if path == "/poll":
                polls.append(secret)
                if len(polls) == 1:
                    return 401, None      # stale secret rejected
                raise _Stop               # second poll: end the loop
            raise AssertionError(f"unexpected call to {path}")

        with mock.patch.object(agent, "CONTROL_URL", "http://c"), \
             mock.patch.object(agent, "_load_state", return_value={"machine_id": 1, "secret": "old"}), \
             mock.patch.object(agent, "_enroll", return_value="new") as enroll, \
             mock.patch.object(agent, "_http", side_effect=fake_http):
            with self.assertRaises(_Stop):
                agent.main()
        enroll.assert_called_once()
        self.assertEqual(polls, ["old", "new"])  # adopted the re-enrolled secret

    def test_5xx_backs_off_instead_of_hammering(self):
        seq = [503]

        def fake_http(method, path, body=None, secret=None, timeout=agent.POLL_TIMEOUT):
            if path == "/poll":
                if seq:
                    return seq.pop(0), None  # control plane restarting
                raise _Stop                  # end the loop on the retry
            raise AssertionError(f"unexpected call to {path}")

        slept = []
        with mock.patch.object(agent, "CONTROL_URL", "http://c"), \
             mock.patch.object(agent, "_load_state", return_value={"machine_id": 1, "secret": "s"}), \
             mock.patch.object(agent, "_http", side_effect=fake_http), \
             mock.patch.object(agent.time, "sleep", side_effect=lambda s: slept.append(s)):
            with self.assertRaises(_Stop):
                agent.main()
        self.assertTrue(slept)  # backed off on the 503 rather than spinning

    def test_401_exits_when_reenroll_fails(self):
        def fake_http(method, path, body=None, secret=None, timeout=agent.POLL_TIMEOUT):
            if path == "/poll":
                return 401, None
            raise AssertionError(f"unexpected call to {path}")

        with mock.patch.object(agent, "CONTROL_URL", "http://c"), \
             mock.patch.object(agent, "_load_state", return_value={"machine_id": 1, "secret": "old"}), \
             mock.patch.object(agent, "_enroll", return_value=None), \
             mock.patch.object(agent, "_http", side_effect=fake_http):
            with self.assertRaises(SystemExit):
                agent.main()


class CrossPlatformTests(unittest.TestCase):
    def test_shell_argv_posix_uses_bash(self):
        with mock.patch.object(agent, "IS_WINDOWS", False), \
             mock.patch.dict(agent.os.environ, {}, clear=False):
            agent.os.environ.pop("AGENT_SHELL", None)
            self.assertEqual(agent._shell_argv("x"), ["bash", "-c", "x"])

    def test_shell_argv_windows_prefers_bash_when_present(self):
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.object(agent.shutil, "which", return_value="C:/bash.exe"):
            agent.os.environ.pop("AGENT_SHELL", None)
            self.assertEqual(agent._shell_argv("x"), ["bash", "-c", "x"])

    def test_shell_argv_windows_falls_back_to_cmd(self):
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.object(agent.shutil, "which", return_value=None):
            agent.os.environ.pop("AGENT_SHELL", None)
            self.assertEqual(agent._shell_argv("x"), ["cmd", "/c", "x"])

    def test_shell_argv_env_override_wins(self):
        with mock.patch.dict(agent.os.environ, {"AGENT_SHELL": "zsh"}):
            self.assertEqual(agent._shell_argv("x"), ["zsh", "-c", "x"])

    def test_default_state_path_per_os(self):
        with mock.patch.object(agent, "IS_WINDOWS", False):
            self.assertEqual(agent._default_state_path(), "/etc/mc-spawn-agent/cred.json")
        with mock.patch.object(agent, "IS_WINDOWS", True), \
             mock.patch.dict(agent.os.environ, {"ProgramData": r"C:\ProgramData"}):
            self.assertTrue(agent._default_state_path().endswith("cred.json"))
            self.assertIn("mc-spawn-agent", agent._default_state_path())

    def test_playit_networking_per_os(self):
        with mock.patch.object(agent, "IS_WINDOWS", False):
            agent.os.environ.pop("PLAYIT_LOCAL_IP", None)
            self.assertEqual(agent._playit_local_ip(), "127.0.0.1")
            self.assertEqual(agent._playit_net_args(), ["--network", "host"])
        with mock.patch.object(agent, "IS_WINDOWS", True):
            agent.os.environ.pop("PLAYIT_LOCAL_IP", None)
            self.assertEqual(agent._playit_local_ip(), "host.docker.internal")
            self.assertIn("host.docker.internal:host-gateway", agent._playit_net_args())

    def test_playit_local_ip_env_override(self):
        with mock.patch.dict(agent.os.environ, {"PLAYIT_LOCAL_IP": "10.0.0.5"}):
            self.assertEqual(agent._playit_local_ip(), "10.0.0.5")


class ProtocolNegotiationTests(unittest.TestCase):
    def test_enroll_sends_protocol_and_platform(self):
        captured = {}

        def fake_http(method, path, body=None, secret=None, timeout=agent.POLL_TIMEOUT):
            captured["body"] = body
            return 200, {"machine_id": 1, "secret": "s"}

        with mock.patch.object(agent, "TOKEN", "tok"), \
             mock.patch.object(agent, "_http", side_effect=fake_http), \
             mock.patch.object(agent, "_save_state"):
            agent._enroll()
        self.assertEqual(captured["body"]["protocol_version"], agent.PROTOCOL_VERSION)
        self.assertEqual(captured["body"]["platform"], agent.PLATFORM)

    def test_http_attaches_protocol_and_platform_headers(self):
        seen = {}

        class FakeResp:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return FakeResp()

        with mock.patch.object(agent, "CONTROL_URL", "http://c"), \
             mock.patch.object(agent.urllib.request, "urlopen", side_effect=fake_urlopen):
            agent._http("GET", "/poll", secret="s")
        self.assertEqual(seen["headers"]["x-mc-spawn-protocol"], str(agent.PROTOCOL_VERSION))
        self.assertEqual(seen["headers"]["x-mc-spawn-platform"], agent.PLATFORM)


if __name__ == "__main__":
    unittest.main()
