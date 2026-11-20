"""Test-first check: which files are tests, which gate runs them, and red/green on the base. Run: python3 -m unittest tests.test_tdd"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import tdd  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.loop import Loop, Run, _pathspec_excludes, fix_model  # noqa: E402

PATHS = DEFAULTS["loop"]["tdd"]["test_paths"]


def cfg(gates=(), **t):
    return {"gates": list(gates), "loop": {**DEFAULTS["loop"], "tdd": {**DEFAULTS["loop"]["tdd"], **t}}}


class TestFiles(unittest.TestCase):
    def test_default_patterns(self):
        changed = ["tests/test_store.py", "tests/unit/deep/helpers.py", "pkg/store_test.go", "src/a.test.ts",
                   "src/b.spec.js", "lib/test_util.py", "todo/store.py", "README.md", "contest.py"]
        self.assertEqual(tdd.test_files(changed, PATHS),
                         ["tests/test_store.py", "tests/unit/deep/helpers.py", "pkg/store_test.go", "src/a.test.ts",
                          "src/b.spec.js", "lib/test_util.py"])

    def test_no_patterns_means_no_test_files(self):
        self.assertEqual(tdd.test_files(["tests/test_a.py"], []), [])


class Gate(unittest.TestCase):
    def test_first_gate_named_test(self):
        c = cfg(["npm run lint", {"name": "unit-tests", "cmd": "pytest"}, {"name": "test-e2e", "cmd": "x"}])
        self.assertEqual(tdd.gate(c), {"name": "unit-tests", "cmd": "pytest"})

    def test_named_gate(self):
        c = cfg([{"name": "test", "cmd": "a"}, {"name": "check", "cmd": "b"}], gate="check")
        self.assertEqual(tdd.gate(c), {"name": "check", "cmd": "b"})

    def test_none_when_no_test_gate(self):
        self.assertIsNone(tdd.gate(cfg([{"name": "lint", "cmd": "a"}])))

    def test_none_when_named_gate_unknown(self):
        self.assertIsNone(tdd.gate(cfg([{"name": "test", "cmd": "a"}], gate="tests")))


class Finding(unittest.TestCase):
    def test_passes_on_base(self):
        r = {"status": "passes_on_base", "files": ["tests/a.py", "tests/b.py"], "gate": "test"}
        self.assertEqual(tdd.finding(r), {
            "severity": "major", "file": "tests/a.py",
            "issue": "tests pass without the change (gate test on the base): tests/a.py, tests/b.py",
            "fix": "make these tests fail on the old code: assert the new behavior"})

    def test_no_tests(self):
        f = tdd.finding({"status": "no_tests", "files": [], "gate": "test"})
        self.assertEqual((f["severity"], f["file"], f["issue"]), ("major", "-", "no tests added"))

    def test_no_finding(self):
        for r in (None, {"status": "red"}, {"status": "skipped"}):
            self.assertIsNone(tdd.finding(r))


class FixModel(unittest.TestCase):
    def models(self, escalate):
        return {"models": {**DEFAULTS["models"], "implement": "sonnet", "escalate": escalate}}

    def test_no_escalate(self):
        self.assertEqual(fix_model(self.models(None), 2, 2), "sonnet")

    def test_escalate_on_last_round_only(self):
        c = self.models("opus")
        self.assertEqual([fix_model(c, r, 3) for r in (1, 2, 3)], ["sonnet", "sonnet", "opus"])


GATE = {"name": "test", "cmd": "python3 -m unittest discover -s tests"}
OLD = "def f():\n    return 1\n"
NEW = "def f():\n    return 2\n"
PROVES = "import unittest\nfrom app import f\n\nclass T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(f(), 2)\n"
WEAK = "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Check(unittest.TestCase):
    """A repo whose `base` branch has app.py returning 1; each test commits a change on `work`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "base")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        self.write({"app.py": OLD, "tests/__init__.py": "", "README.md": "x\n"})
        git(self.repo, "switch", "-q", "-c", "work")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, files):
        for p, text in files.items():
            (self.repo / p).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / p).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")

    def check(self, run_one=None, **t):
        setup = t.pop("setup", [])
        c = {**cfg([GATE], enabled=True, **t), "_root": str(self.repo), "review": DEFAULTS["review"],
             "workspace": {**DEFAULTS["workspace"], "setup": setup}}
        ex = _pathspec_excludes(DEFAULTS["review"]["exclude"])
        return tdd.check(c, self.repo, "base", ex, **({"run_one": run_one} if run_one else {}))

    def worktrees(self):
        return git(self.repo, "worktree", "list").count("\n")

    def test_red_on_base(self):
        self.write({"app.py": NEW, "tests/test_app.py": PROVES})
        r = self.check()
        self.assertEqual((r["status"], r["files"], r["gate"]), ("red", ["tests/test_app.py"], "test"))
        self.assertEqual(self.worktrees(), 1)

    def test_passes_on_base(self):
        self.write({"app.py": NEW, "tests/test_app.py": WEAK})
        self.assertEqual(self.check()["status"], "passes_on_base")

    def test_no_tests(self):
        self.write({"app.py": NEW})
        self.assertEqual(self.check()["status"], "no_tests")

    def test_docs_only_is_skipped(self):
        self.write({"README.md": "y\n"})
        r = self.check()
        self.assertEqual((r["status"], r["reason"]), ("skipped", "docs only"))

    def test_docs_and_excluded_files_are_docs_only(self):
        self.write({"README.md": "y\n", "poetry.lock": "z\n"})
        self.assertEqual(self.check()["reason"], "docs only")

    def test_deleted_test_is_not_copied(self):
        self.write({"tests/test_old.py": WEAK})
        git(self.repo, "branch", "-f", "base")
        git(self.repo, "rm", "-q", "tests/test_old.py")
        self.write({"app.py": NEW, "tests/test_app.py": PROVES})
        self.assertEqual(self.check()["files"], ["tests/test_app.py"])

    def test_skipped_without_gate(self):
        r = self.check(gate="nope")
        self.assertEqual(r["status"], "skipped")
        self.assertIn("nope", r["reason"])

    def test_disabled_returns_none(self):
        c = {**cfg([GATE]), "_root": str(self.repo), "review": DEFAULTS["review"], "workspace": DEFAULTS["workspace"]}
        self.assertIsNone(tdd.check(c, self.repo, "base"))

    def test_failed_setup_is_skipped_not_red(self):
        self.write({"app.py": NEW, "tests/test_app.py": PROVES})
        r = self.check(setup=["false"])
        self.assertEqual(r["status"], "skipped")
        self.assertIn("setup", r["reason"])
        self.assertEqual(self.worktrees(), 1)

    def test_worktree_removed_when_gate_raises(self):
        self.write({"app.py": NEW, "tests/test_app.py": PROVES})

        def boom(*a, **k):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.check(run_one=boom)
        self.assertEqual(self.worktrees(), 1)


class Rounds(unittest.TestCase):
    """_quality_rounds with a fake claude: the tdd finding drives a fix round; escalation picks the model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(root / "origin.git"))
        self.write({"app.py": OLD, "tests/__init__.py": "", ".gitignore": ".agent/\n"})
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "switch", "-q", "-c", "work")
        self.calls, self.actions = [], {}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, files, commit=True):
        for p, text in files.items():
            (self.repo / p).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / p).write_text(text)
        if commit:
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", "c")

    def claude(self, text, *, cwd, step, model=None, cost_log=None, **kw):
        self.calls.append((step, model))
        Path(cost_log).parent.mkdir(parents=True, exist_ok=True)
        with open(cost_log, "a") as f:
            f.write(json.dumps({"step": step, "model": model, "usd": 0.01}) + "\n")
        self.write(self.actions.get(step, {}), commit=False)
        return {"summary": "s", "pr_sections": [], "lessons": [], "concerns": []}, "sid"

    def rounds(self, escalate="opus", fix=1, review=1, tdd_on=True):
        c = copy.deepcopy(DEFAULTS)
        c.update(_root=str(self.repo), gates=[GATE])
        c["loop"].update(max_fix_rounds=fix, max_review_rounds=review)
        c["loop"]["tdd"]["enabled"] = tdd_on
        c["models"]["escalate"] = escalate
        c["review"]["skip_under_lines"] = 1000  # review plan "skip": only the tdd finding can fail it
        lp = Loop(c, claude=self.claude)
        run = Run(c, 7)
        run.set(ws=str(self.repo))
        failure = lp._quality_rounds(run, self.repo, {"key": "#7", "title": "t", "body": "Do it."})
        return failure, Run(c, 7)

    def test_weak_test_gets_a_finding_and_an_escalated_fix_round(self):
        self.write({"app.py": NEW, "tests/test_app.py": WEAK})
        self.actions["fix-review:1"] = {"tests/test_app.py": PROVES}
        failure, run = self.rounds()
        self.assertIsNone(failure)
        self.assertEqual(self.calls, [("fix-review:1", "opus")])
        first = run["reviews"][0]
        self.assertFalse(first["pass"])
        self.assertEqual(first["findings"][0]["issue"][:34], "tests pass without the change (gat")
        self.assertEqual(run["tdd"]["status"], "red")
        self.assertEqual([t["status"] for t in run["tdd_checks"]], ["passes_on_base", "red"])
        self.assertEqual(run["escalations"], [{"step": "fix-review:1", "round": 1, "model": "opus",
                                               "reason": "last review-fix round (1 of 1)"}])

    def test_gate_fix_escalates_only_on_the_last_round(self):
        self.write({"app.py": NEW, "tests/test_app.py": PROVES.replace("2)", "30)")})
        self.actions["fix-gates:2"] = {"tests/test_app.py": PROVES}
        failure, run = self.rounds(fix=2, tdd_on=False)
        self.assertIsNone(failure)
        self.assertEqual(self.calls, [("fix-gates:1", "sonnet"), ("fix-gates:2", "opus")])
        rows = [json.loads(l) for l in run.cost_log.read_text().splitlines()]
        self.assertEqual([r["model"] for r in rows], ["sonnet", "opus"])
        self.assertIsNone(run["tdd"])

    def test_no_escalate_keeps_implement_model(self):
        self.write({"app.py": NEW, "tests/test_app.py": WEAK})
        self.actions["fix-review:1"] = {"tests/test_app.py": PROVES}
        _, run = self.rounds(escalate=None)
        self.assertEqual(self.calls, [("fix-review:1", "sonnet")])
        self.assertFalse(run["escalations"])


if __name__ == "__main__":
    unittest.main()
