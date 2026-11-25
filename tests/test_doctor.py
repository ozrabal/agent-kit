"""doctor: setup checks, with external commands replaced by a fake runner."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
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


LABELS = json.dumps([{"name": n} for n in ("ready-for-agent", "in-progress", "done", "needs-human", "needs-refinement")])
HEALTHY = {"gh label list": (0, LABELS), "git ls-remote": (0, "abc\trefs/heads/main\n")}


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.home = Path(self.tmp.name) / "home"
        self.root.mkdir()
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def results(self, cfg, run, run_gates=None):
        return {r["name"]: r for r in doctor.check(cfg, run=run, run_gates=run_gates,
                                                   user_settings=self.home / ".claude" / "settings.json")}

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

    def settings(self, base, data):
        (base / ".claude").mkdir(parents=True, exist_ok=True)
        (base / ".claude" / "settings.json").write_text(json.dumps(data))

    def test_hooks_missing_warns_with_install_hint(self):
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))
        self.assertTrue(res["hooks"]["warn"])
        self.assertIn("agent-kit hooks install", res["hooks"]["hint"])

    def test_hooks_in_repo_or_user_settings(self):
        from agentkit import hooks
        self.settings(self.root, hooks.merge({}))
        self.assertFalse(self.results(config(self.root, {}), FakeRun(HEALTHY))["hooks"]["warn"])
        (self.root / ".claude" / "settings.json").unlink()
        self.settings(self.home, hooks.merge({}))
        self.assertFalse(self.results(config(self.root, {}), FakeRun(HEALTHY))["hooks"]["warn"])

    def test_hooks_checked_also_when_stop_gate_off(self):
        cfg = config(self.root, {})
        cfg["hooks"]["stop_gate"] = False
        self.assertTrue(self.results(cfg, FakeRun(HEALTHY))["hooks"]["warn"])

    def test_old_install_names_the_missing_events(self):
        self.settings(self.root, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "agent-kit hook stop"}]}]}})
        hint = self.results(config(self.root, {}), FakeRun(HEALTHY))["hooks"]["hint"]
        self.assertIn("PreToolUse", hint)
        self.assertIn("SessionStart", hint)

    def test_recent_guard_denials_warn(self):
        import time
        log = self.root / ".agent" / "runs" / "hooks.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"ts": int(time.time()), "event": "guard", "outcome": "block", "cwd": "/wt/5",
                                   "note": "agent-kit guard: blocked Bash: curl"}) + "\n")
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))["guard denials"]
        self.assertTrue(res["warn"])
        self.assertIn("blocked Bash", res["hint"])

    def failing_run(self, n, ts):
        d = self.root / ".agent" / "runs" / str(n)
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"number": n, "gate_failures": [{"round": 0, "results": [
            {"name": "test", "ok": False, "secs": 1, "output": "E   assert 2 == 1"}]}]}))
        (d / "cost.jsonl").write_text(json.dumps({"ts": ts, "step": "implement", "usd": 0.05}) + "\n")

    def retro_row(self, ts, mode):
        with (self.root / ".agent" / "runs" / "retro.jsonl").open("a") as f:
            f.write(json.dumps({"ts": ts, "mode": mode}) + "\n")

    def test_retro_ok_without_repeats(self):
        self.failing_run(1, 100)
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))["retro"]
        self.assertEqual((res["ok"], res["warn"]), (True, False))

    def test_repeats_since_the_last_retro_warn(self):
        self.failing_run(1, 100)
        self.failing_run(2, 300)
        self.retro_row(200, "llm")
        self.retro_row(250, "no-llm")  # --no-llm, refused and failed retros learn nothing: they do not count
        self.assertFalse(self.results(config(self.root, {}), FakeRun(HEALTHY))["retro"]["warn"])
        self.failing_run(3, 400)
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))["retro"]
        self.assertTrue(res["warn"])
        self.assertEqual(res["hint"], "in 2 runs since the last retro: 1 repeated gate errors, 0 review findings, "
                                      "0 denied tools: run `agent-kit retro`")

    def test_recent_hook_errors_warn(self):
        import time
        log = self.root / ".agent" / "runs" / "hooks.jsonl"
        log.parent.mkdir(parents=True)
        now = int(time.time())
        rows = [{"ts": now - 90000, "event": "stop", "outcome": "error", "note": "old one"},
                {"ts": now - 60, "event": "stop", "outcome": "pass"},
                {"ts": now - 30, "event": "post-edit", "outcome": "error", "note": "TimeoutExpired: ruff",
                 "cwd": "/wt/7-add"}]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))["hook errors"]
        self.assertTrue(res["warn"])
        self.assertIn("1 in the last 24h", res["hint"])
        self.assertIn("TimeoutExpired: ruff", res["hint"])
        self.assertIn("/wt/7-add", res["hint"])
        self.assertNotIn("old one", res["hint"])

    def test_hook_error_window_from_config(self):
        import time
        log = self.root / ".agent" / "runs" / "hooks.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"ts": int(time.time()) - 7200, "event": "stop", "outcome": "error", "note": "x"}) + "\n")
        cfg = config(self.root, {})
        cfg["hooks"]["error_window_hours"] = 1
        self.assertFalse(self.results(cfg, FakeRun(HEALTHY))["hook errors"]["warn"])

    def test_no_hook_errors(self):
        res = self.results(config(self.root, {}), FakeRun(HEALTHY))["hook errors"]
        self.assertFalse(res["warn"])
        self.assertTrue(res["ok"])

    def test_render_and_ok(self):
        rs = [doctor.result("git", True), doctor.warning("config", "unknown keys (typo?): x"),
              doctor.result("gh auth", False, "run `gh auth login`")]
        self.assertEqual(doctor.render(rs), "OK   git\nWARN config -> unknown keys (typo?): x\n"
                                            "FAIL gh auth -> run `gh auth login`")
        self.assertFalse(doctor.all_ok(rs))
        self.assertTrue(doctor.all_ok(rs[:2]))



class LoopModes(unittest.TestCase):
    """doctor: loop.refine is a known mode; loop.parallel fits merge.max_open_prs and the workspace mode."""

    def check(self, **loop):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["loop"].update(loop.pop("loop", {}))
        for k, v in loop.items():
            cfg[k].update(v)
        return {r["name"]: r for r in doctor.check_loop_modes(SimpleNamespace(cfg=cfg))}

    def test_defaults_ok(self):
        res = self.check()
        self.assertTrue(res["refine"]["ok"] and not res["refine"]["warn"])
        self.assertTrue(res["parallel"]["ok"] and not res["parallel"]["warn"])

    def test_unknown_refine_mode_fails(self):
        self.assertEqual(self.check(loop={"refine": "on"})["refine"],
                         {"name": "refine", "ok": False, "warn": False, "hint": "loop.refine must be off, check or auto (got 'on')"})

    def test_parallel_above_max_open_prs_warns(self):
        r = self.check(loop={"parallel": 3}, merge={"max_open_prs": 2})["parallel"]
        self.assertTrue(r["warn"])
        self.assertIn("at most 2 run at once", r["hint"])

    def test_parallel_in_branch_mode_fails(self):
        r = self.check(loop={"parallel": 2}, merge={"max_open_prs": 2}, workspace={"mode": "branch"})["parallel"]
        self.assertFalse(r["ok"])
        self.assertIn("workspace.mode", r["hint"])

    def test_parallel_not_a_positive_int_fails(self):
        self.assertFalse(self.check(loop={"parallel": 0})["parallel"]["ok"])


if __name__ == "__main__":
    unittest.main()
