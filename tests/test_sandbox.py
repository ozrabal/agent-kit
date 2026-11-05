"""Offline checks for sandbox/issues.json: references resolve and blockers parse as intended."""
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.tracker import GitHub  # noqa: E402

spec = importlib.util.spec_from_file_location("reset", ROOT / "sandbox" / "reset.py")
reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)


class SandboxTickets(unittest.TestCase):
    def setUp(self):
        self.tickets = json.loads((ROOT / "sandbox" / "issues.json").read_text())
        cfg = copy.deepcopy(DEFAULTS)
        cfg["_root"] = "."
        self.tr = GitHub(cfg)

    def test_blockers_resolve_to_earlier_tickets(self):
        numbers = {}
        got = {}
        for idx, t in enumerate(self.tickets, start=1):
            n = 100 + idx  # fake issue numbers, as after previous resets
            body = reset.render(t["body"], numbers)  # raises if it points forward
            numbers[idx] = n
            got[idx] = self.tr.blockers({"number": n, "body": body})
        self.assertEqual(got[1], [])
        self.assertEqual(got[2], [101])
        self.assertEqual(got[3], [102])
        self.assertEqual(got[5], [])

    def test_render_rejects_forward_reference(self):
        with self.assertRaises(ValueError):
            reset.render("## Blocked by\n- {#3}", {1: 101})

    def test_every_ticket_has_probe(self):
        for t in self.tickets:
            self.assertTrue(t.get("probe"), t["title"])


class SafeDelete(unittest.TestCase):
    def test_guard(self):
        import subprocess
        import tempfile
        slug = "me/agent-kit-sandbox"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            reset.check_safe_dir(tmp / "missing", slug)  # nothing to delete: ok
            (tmp / "empty").mkdir()
            reset.check_safe_dir(tmp / "empty", slug)
            plain = tmp / "plain"
            plain.mkdir()
            (plain / "f").write_text("x")
            with self.assertRaises(SystemExit):
                reset.check_safe_dir(plain, slug)
            outer = tmp / "outer"  # a real project with the sandbox name as a subfolder
            (outer / "agent-kit-sandbox").mkdir(parents=True)
            (outer / "agent-kit-sandbox" / "f").write_text("x")
            subprocess.run(["git", "init", "-q", str(outer)], check=True)
            subprocess.run(["git", "-C", str(outer), "remote", "add", "origin",
                            "https://github.com/me/agent-kit-sandbox-not.git"], check=True)
            with self.assertRaises(SystemExit):
                reset.check_safe_dir(outer / "agent-kit-sandbox", slug)
            clone = tmp / "clone"
            subprocess.run(["git", "init", "-q", str(clone)], check=True)
            (clone / "f").write_text("x")
            subprocess.run(["git", "-C", str(clone), "remote", "add", "origin",
                            f"https://github.com/{slug}.git"], check=True)
            reset.check_safe_dir(clone, slug)  # the sandbox clone itself: ok
