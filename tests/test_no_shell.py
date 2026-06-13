"""Static security check (spec §21.2): the agent must contain NO remote-command path.

This fails CI if agent.py grows any shell-execution pattern — the whole point of the v2
refactor is that the untrusted backend can never get a string executed as an OS command. A
genuine, locally-reviewed exception may tag its line `# shell-audit: ok`, per the spec.
"""
import re
import unittest
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "agent.py"

# High-signal patterns for "a string is being executed as a command / a shell is invoked".
BANNED = [
    r"shell\s*=\s*True",
    r"os\.system\s*\(",
    r"\.getoutput\s*\(",
    r"\bpopen2\b",
    r"bash\s+-l?c\b",
    r"\bsh\s+-l?c\b",
    r'["\']bash["\']\s*,\s*["\']-l?c["\']',     # ["bash", "-c"] / ["bash", "-lc"]
    r'["\']sh["\']\s*,\s*["\']-l?c["\']',        # ["sh", "-c"]
    r"Invoke-Expression",
    r"-Command\b",
    r"\biex\b",
    r'payload\[["\']script["\']\]',
]
_RES = [re.compile(p) for p in BANNED]


class NoShellTests(unittest.TestCase):
    def setUp(self):
        self.lines = AGENT.read_text(encoding="utf-8").splitlines()

    def test_no_banned_shell_patterns(self):
        violations = []
        for n, line in enumerate(self.lines, 1):
            if "# shell-audit: ok" in line:
                continue
            for rx in _RES:
                if rx.search(line):
                    violations.append(f"agent.py:{n}: {rx.pattern!r} :: {line.strip()}")
        self.assertEqual(violations, [], "banned shell-execution patterns found:\n" + "\n".join(violations))

    def test_no_dead_shell_helpers(self):
        text = AGENT.read_text(encoding="utf-8")
        for token in ("def _run_shell", "def _shell_argv", "AGENT_SHELL"):
            self.assertNotIn(token, text, f"{token} must be gone (removed remote-shell path)")

    def test_shell_kind_not_in_registry(self):
        import sys
        sys.path.insert(0, str(AGENT.parent))
        import agent
        self.assertNotIn("shell", agent.ACTION_REGISTRY)
        self.assertNotIn("exec", agent.ACTION_REGISTRY)
        self.assertNotIn("run", agent.ACTION_REGISTRY)


if __name__ == "__main__":
    unittest.main()
