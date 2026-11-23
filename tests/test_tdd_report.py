"""Where the red/green check and escalation show up: PR body, report, doctor, `agent-kit log`. Run: python3 -m unittest tests.test_tdd_report"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import doctor, report  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.loop import Loop  # noqa: E402

RED = {"status": "red", "reason": "red on base -> green on branch", "files": ["tests/a.py", "tests/b.py"],
       "gate": "test", "secs": 1.2}
WEAK = {"status": "passes_on_base", "reason": "tests pass without the change", "files": ["tests/a.py"],
        "gate": "test", "secs": 0.8}
ESC = {"step": "fix-review:1", "round": 1, "model": "opus", "reason": "last review-fix round (loop.max_review_rounds=1)"}
GATES = {"ok": True, "results": [{"name": "test", "ok": True}]}


def pr_body(tdd):
    lp = Loop.__new__(Loop)  # _pr_body needs only cfg
    lp.cfg = copy.deepcopy(DEFAULTS)
    issue = {"number": 7, "key": "#7", "title": "t"}
    return lp._pr_body(issue, {"summary": "s"}, {"gates": GATES, "reviews": [], "tdd": tdd}, None)


class PrBody(unittest.TestCase):
    def test_red_green(self):
        self.assertIn("- pass: test\n- Tests: red on base -> green on branch (2 test files, gate test)\n", pr_body(RED))

    def test_problem(self):
        self.assertIn("- Tests: FAIL: tests pass without the change: `tests/a.py`\n", pr_body(WEAK))

    def test_no_tests_and_skipped(self):
        self.assertIn("- Tests: FAIL: no test files added or changed\n",
                      pr_body({"status": "no_tests", "reason": "no test files added or changed", "files": []}))
        self.assertIn("- Tests: skipped: docs only\n", pr_body({"status": "skipped", "reason": "docs only", "files": []}))

    def test_skipped_review_failed_by_tdd(self):
        lp = Loop.__new__(Loop)
        lp.cfg = copy.deepcopy(DEFAULTS)
        rv = {"skipped": "docs only", "pass": False, "plan": {"run": False}, "findings": []}
        body = lp._pr_body({"number": 7}, {"summary": "s"}, {"gates": GATES, "reviews": [rv], "tdd": WEAK}, None)
        self.assertIn("Automated review skipped: docs only; failed by the red/green check\n", body)

    def test_off(self):
        self.assertNotIn("Tests:", pr_body(None))


class Report(unittest.TestCase):
    def test_tdd_status_and_escalations(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "7"
            d.mkdir()
            (d / "state.json").write_text(json.dumps({"key": "#7", "tdd": RED, "escalations": [ESC]}))
            (d / "cost.jsonl").write_text(json.dumps({"ts": 1, "step": "fix-review:1", "model": "opus", "usd": 0.1}) + "\n")
            (Path(t) / "8").mkdir()
            rep = report.collect(t)
        a, b = rep["rows"]
        self.assertEqual((a["tdd"], a["escalated"], b["tdd"], b["escalated"]), ("red", 1, "-", 0))
        self.assertEqual(rep["total"]["escalated"], 1)
        self.assertIn("escalated", report.METRICS)
        self.assertIn("red", report.render(rep))

    def test_run_lines(self):
        self.assertEqual(report.run_lines({"tdd_checks": [WEAK, RED], "escalations": [ESC]}), [
            "## tdd",
            "1. PASSES_ON_BASE gate test, 0.8s: tests pass without the change (tests/a.py)",
            "2. RED gate test, 1.2s: red on base -> green on branch (tests/a.py, tests/b.py)",
            "## escalations",
            "fix-review:1 -> opus: last review-fix round (loop.max_review_rounds=1)"])
        self.assertEqual(report.run_lines({}), [])


class Doctor(unittest.TestCase):
    def results(self, gates, **t):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["gates"] = gates
        cfg["loop"]["tdd"].update(t)
        return doctor.check_tdd(doctor.Ctx(cfg, None, None, None))

    def test_off_says_nothing(self):
        self.assertEqual(self.results([]), [])

    def test_resolved_gate(self):
        self.assertEqual(self.results(["make test"], enabled=True), [doctor.result("tdd", True)])

    def test_unresolved_gate_fails(self):
        [r] = self.results(["make lint"], enabled=True)
        self.assertFalse(r["ok"])
        self.assertIn("loop.tdd.gate", r["hint"])
        [r] = self.results(["make test"], enabled=True, gate="unit")
        self.assertFalse(r["ok"])
        self.assertIn("unit", r["hint"])


if __name__ == "__main__":
    unittest.main()
