"""hooks: Claude Code hook entry point, fed sample stdin JSON with fake runners and gates."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import hooks  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402


def config(root, **over):
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(over)
    cfg["_root"] = str(root)
    cfg["_configured"] = True
    return cfg


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def stdin(self, **kw):
        return json.dumps({"session_id": "s1", "cwd": str(self.root), **kw})


class Dispatch(HookTest):
    def test_no_config_does_nothing(self):
        self.assertEqual(hooks.main("stop", self.stdin(), load=lambda cwd: None), (0, ""))

    def test_unknown_event_does_nothing(self):
        self.assertEqual(hooks.main("nope", self.stdin(), load=lambda cwd: config(self.root)), (0, ""))

    def test_internal_error_never_blocks(self):
        def boom(cwd):
            raise ValueError("bad")
        code, msg = hooks.main("stop", self.stdin(), load=boom)
        self.assertEqual(code, 0)
        self.assertIn("bad", msg)

    def test_bad_stdin_never_blocks(self):
        code, _ = hooks.main("stop", "not json", load=lambda cwd: config(self.root))
        self.assertEqual(code, 0)


class FakeGates:
    """check(cfg, cwd) -> a gates report; counts calls."""

    def __init__(self, *oks):
        self.oks, self.calls = list(oks), 0

    def __call__(self, cfg, cwd):
        ok = self.oks[min(self.calls, len(self.oks) - 1)]
        self.calls += 1
        res = {"name": "test", "ok": ok, "secs": 0.1}
        if not ok:
            res["output"] = "FAIL: test_x"
        return {"ok": ok, "results": [res]}


class StopGate(HookTest):
    def stop(self, gates, active=False, sid="s1", **cfg_over):
        cfg = config(self.root, **cfg_over)
        return hooks.main("stop", self.stdin(stop_hook_active=active, session_id=sid),
                          load=lambda cwd: cfg, check=gates)

    def test_passing_gates_allow_stop(self):
        self.assertEqual(self.stop(FakeGates(True)), (0, ""))

    def test_failing_gates_block_with_output(self):
        code, msg = self.stop(FakeGates(False))
        self.assertEqual(code, 2)
        self.assertIn("Gates fail, fix before finishing", msg)
        self.assertIn("FAIL: test_x", msg)

    def test_gives_up_after_max_blocks_in_a_row(self):
        g = FakeGates(False)
        codes = [self.stop(g, active=i > 0)[0] for i in range(4)]
        self.assertEqual(codes, [2, 2, 0, 0])

    def test_counter_resets_on_a_fresh_stop(self):
        g = FakeGates(False)
        self.stop(g)
        self.stop(g, active=True)
        self.assertEqual(self.stop(g, active=False)[0], 2)

    def test_counter_is_per_session(self):
        g = FakeGates(False)
        self.stop(g, sid="a")
        self.stop(g, sid="a", active=True)
        self.assertEqual(self.stop(g, sid="b", active=True)[0], 2)

    def test_disabled_stop_gate_does_nothing(self):
        g = FakeGates(False)
        self.assertEqual(self.stop(g, hooks={"stop_gate": False, "stop_max_blocks": 2}), (0, ""))
        self.assertEqual(g.calls, 0)


class FakeRun:
    """Records commands; exits with `code` and prints `out`."""

    def __init__(self, code=0, out=""):
        self.code, self.out, self.calls = code, out, []

    def __call__(self, cmd, cwd=None, check=False, env=None, timeout=None, **kw):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return subprocess.CompletedProcess(cmd, self.code, self.out, "")


RUFF = {"name": "ruff", "match": "*.py", "cmd": "ruff check {file}", "timeout": 60}


class PostEdit(HookTest):
    def edit(self, path, run, file_gates=(RUFF,), tool="Edit"):
        cfg = config(self.root, file_gates=list(file_gates))
        raw = self.stdin(tool_name=tool, tool_input={"file_path": str(path)})
        return hooks.main("post-edit", raw, load=lambda cwd: cfg, run=run)

    def test_matching_file_runs_gate_with_repo_relative_path(self):
        run = FakeRun()
        self.assertEqual(self.edit(self.root / "src" / "a.py", run), (0, ""))
        self.assertEqual([c["cmd"] for c in run.calls], ["ruff check src/a.py"])
        self.assertEqual(Path(run.calls[0]["cwd"]), self.root.resolve())
        self.assertEqual(run.calls[0]["timeout"], 60)

    def test_other_files_are_skipped(self):
        run = FakeRun()
        self.assertEqual(self.edit(self.root / "README.md", run), (0, ""))
        self.assertEqual(run.calls, [])

    def test_files_outside_the_repo_are_skipped(self):
        run = FakeRun()
        self.assertEqual(self.edit("/elsewhere/x.py", run), (0, ""))
        self.assertEqual(run.calls, [])

    def test_failure_blocks_with_compacted_output(self):
        code, msg = self.edit(self.root / "a.py", FakeRun(1, "\x1b[31ma.py:1:1: F401 unused import\x1b[0m\n"))
        self.assertEqual(code, 2)
        self.assertIn("ruff", msg)
        self.assertIn("a.py:1:1: F401 unused import", msg)
        self.assertNotIn("\x1b", msg)

    def test_paths_with_spaces_are_quoted(self):
        run = FakeRun()
        self.edit(self.root / "my dir" / "a.py", run)
        self.assertEqual(run.calls[0]["cmd"], "ruff check 'my dir/a.py'")

    def test_no_file_gates_does_nothing(self):
        run = FakeRun()
        self.assertEqual(self.edit(self.root / "a.py", run, file_gates=()), (0, ""))
        self.assertEqual(run.calls, [])


FOREIGN = {"permissions": {"allow": ["Bash(ls *)"]}, "hooks": {
    "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-logger"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "notify-me"}]}],
}}


def commands(settings, event):
    return [h["command"] for g in settings.get("hooks", {}).get(event, []) for h in g["hooks"]]


class Settings(unittest.TestCase):
    def test_install_adds_both_hooks(self):
        s = hooks.merge({})
        self.assertEqual(s["hooks"]["PostToolUse"],
                         [{"matcher": "Edit|Write|MultiEdit",
                           "hooks": [{"type": "command", "command": "agent-kit hook post-edit"}]}])
        self.assertEqual(commands(s, "Stop"), ["agent-kit hook stop"])
        self.assertTrue(hooks.installed(s))

    def test_install_twice_is_the_same(self):
        once = hooks.merge(copy.deepcopy(FOREIGN))
        self.assertEqual(hooks.merge(copy.deepcopy(once)), once)

    def test_install_keeps_unrelated_settings_and_hooks(self):
        s = hooks.merge(copy.deepcopy(FOREIGN))
        self.assertEqual(s["permissions"], FOREIGN["permissions"])
        self.assertEqual(commands(s, "PostToolUse"), ["my-logger", "agent-kit hook post-edit"])
        self.assertEqual(commands(s, "Stop"), ["notify-me", "agent-kit hook stop"])

    def test_uninstall_removes_only_ours(self):
        self.assertEqual(hooks.unmerge(hooks.merge(copy.deepcopy(FOREIGN))), FOREIGN)

    def test_uninstall_leaves_no_empty_hooks(self):
        self.assertEqual(hooks.unmerge(hooks.merge({})), {})

    def test_not_installed(self):
        self.assertFalse(hooks.installed(FOREIGN))
        self.assertFalse(hooks.installed({}))


if __name__ == "__main__":
    unittest.main()
