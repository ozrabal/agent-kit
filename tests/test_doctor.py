"""doctor: setup checks, with external commands replaced by a fake runner."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import doctor  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402


class FakeRun:
    """Answers commands by their first words; unknown tools raise FileNotFoundError like a missing binary."""

    def __init__(self, answers=None, missing=()):
        self.answers = {"git": (0, ""), "gh": (0, ""), "claude": (0, ""), **(answers or {})}
        self.missing = set(missing)

    def __call__(self, cmd, cwd=None, check=False, **kw):
        if cmd[0] in self.missing:
            raise FileNotFoundError(cmd[0])
        for n in range(len(cmd), 0, -1):
            hit = self.answers.get(" ".join(cmd[:n]))
            if hit is not None:
                code, stdout = hit
                return subprocess.CompletedProcess(cmd, code, stdout, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def config(root, user=None):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_root"] = str(root)
    cfg["_configured"] = user is not None
    if user is not None:
        (root / ".agent").mkdir(parents=True, exist_ok=True)
        (root / ".agent" / "config.json").write_text(json.dumps(user))
    return cfg


LABELS = json.dumps([{"name": n} for n in ("ready-for-agent", "in-progress", "done")])
HEALTHY = {"gh label list": (0, LABELS), "git ls-remote": (0, "abc\trefs/heads/main\n")}


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def results(self, cfg, run, run_gates=None):
        return {r["name"]: r for r in doctor.check(cfg, run=run, run_gates=run_gates)}

    def test_missing_tool_fails_with_hint(self):
        res = self.results(config(self.root, {}), FakeRun(HEALTHY, missing={"claude"}))
        self.assertFalse(res["claude"]["ok"])
        self.assertIn("install", res["claude"]["hint"].lower())
        self.assertTrue(res["git"]["ok"])

    def test_gh_not_logged_in(self):
        res = self.results(config(self.root, {}), FakeRun({**HEALTHY, "gh auth status": (1, "")}))
        self.assertFalse(res["gh auth"]["ok"])
        self.assertIn("gh auth login", res["gh auth"]["hint"])

    def test_missing_config(self):
        res = self.results(config(self.root), FakeRun(HEALTHY))
        self.assertFalse(res["config"]["ok"])
        self.assertIn("agent-kit init", res["config"]["hint"])

    def test_unknown_config_keys_warn_but_free_maps_allowed(self):
        user = {"loop": {"max_fix_round": 3}, "modles": {}, "workspace": {"env": {"ANY_NAME": "1"}}}
        res = self.results(config(self.root, user), FakeRun(HEALTHY))
        self.assertTrue(res["config"]["ok"])
        self.assertTrue(res["config"]["warn"])
        self.assertIn("loop.max_fix_round", res["config"]["hint"])
        self.assertIn("modles", res["config"]["hint"])
        self.assertNotIn("ANY_NAME", res["config"]["hint"])

    def test_valid_config(self):
        res = self.results(config(self.root, {"gates": ["make test"], "loop": {"max_fix_rounds": 1}}), FakeRun(HEALTHY))
        self.assertTrue(res["config"]["ok"])

    def test_missing_labels_named(self):
        only_ready = json.dumps([{"name": "ready-for-agent"}])
        res = self.results(config(self.root, {}), FakeRun({**HEALTHY, "gh label list": (0, only_ready)}))
        self.assertFalse(res["labels"]["ok"])
        self.assertIn("in-progress", res["labels"]["hint"])
        self.assertIn("done", res["labels"]["hint"])
        self.assertNotIn("ready-for-agent", res["labels"]["hint"])

    def test_base_branch_missing_on_remote(self):
        res = self.results(config(self.root, {}), FakeRun({**HEALTHY, "git ls-remote": (0, "")}))
        self.assertFalse(res["base branch"]["ok"])
        self.assertIn("origin/main", res["base branch"]["hint"])

    def test_healthy_setup_passes_remote_checks(self):
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))
        self.assertTrue(res["labels"]["ok"])
        self.assertTrue(res["base branch"]["ok"])

    def test_gates_none_configured(self):
        res = self.results(config(self.root, {}), FakeRun(HEALTHY), run_gates=lambda: {"ok": True, "results": []})
        self.assertFalse(res["gates"]["ok"])
        self.assertIn("gates", res["gates"]["hint"])

    def test_failing_gate_named(self):
        report = {"ok": False, "results": [{"name": "lint", "ok": True}, {"name": "test", "ok": False}]}
        cfg = config(self.root, {})
        cfg["gates"] = ["make lint", "make test"]
        res = self.results(cfg, FakeRun(HEALTHY), run_gates=lambda: report)
        self.assertFalse(res["gates"]["ok"])
        self.assertIn("test", res["gates"]["hint"])
        self.assertNotIn("lint", res["gates"]["hint"])

    def test_no_gates_still_checks_they_are_configured(self):
        cfg = config(self.root, {})
        self.assertFalse(self.results(cfg, FakeRun(HEALTHY), run_gates=None)["gates"]["ok"])
        cfg["gates"] = ["make test"]
        self.assertTrue(self.results(cfg, FakeRun(HEALTHY), run_gates=None)["gates"]["ok"])

    def test_knowledge_base(self):
        cfg = config(self.root, {})
        self.assertFalse(self.results(cfg, FakeRun(HEALTHY))["knowledge base"]["ok"])
        (self.root / ".knowledge").mkdir()
        (self.root / ".knowledge" / "files.jsonl").write_text("")
        self.assertTrue(self.results(cfg, FakeRun(HEALTHY))["knowledge base"]["ok"])

    def test_render_and_ok(self):
        rs = [doctor.result("git", True), doctor.warning("config", "unknown keys (typo?): x"),
              doctor.result("gh auth", False, "run `gh auth login`")]
        self.assertEqual(doctor.render(rs), "OK   git\nWARN config -> unknown keys (typo?): x\n"
                                            "FAIL gh auth -> run `gh auth login`")
        self.assertFalse(doctor.all_ok(rs))
        self.assertTrue(doctor.all_ok(rs[:2]))


if __name__ == "__main__":
    unittest.main()
