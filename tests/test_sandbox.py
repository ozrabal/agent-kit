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
