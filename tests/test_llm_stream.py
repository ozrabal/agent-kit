"""Stream mode of agentkit/llm.py, driven by a fake `claude` script (no LLM calls)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import llm  # noqa: E402

TOOL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "/ws/todo/store.py"}},
    {"type": "tool_use", "name": "StructuredOutput", "input": {"x": 1}}]}}
DENIED = {"type": "user", "message": {"content": [
    {"type": "tool_result", "is_error": True, "content": "Claude requested permissions to use Bash, but you haven't granted it yet."}]}}
REFUSED = {"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
           "message": "A nested command in this command can't be checked before it runs"}
TEXT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}
RESULT = {"type": "result", "is_error": False, "num_turns": 3, "total_cost_usd": 0.0123,
          "structured_output": {"ok": True}, "session_id": "s1", "permission_denials": [{"tool_name": "Bash"}]}


def fake(events, sleep=0):
    lines = "".join(json.dumps(e) + "\n" for e in events)
    return [sys.executable, "-c",
            f"import sys,time; sys.stdin.read(); time.sleep({sleep}); sys.stdout.write({lines!r})"]


class Describe(unittest.TestCase):
    def test_tool_calls_relative_and_errors(self):
        self.assertEqual(llm.describe(TOOL, "/ws"), ["Read todo/store.py"])
        self.assertEqual(len(llm.describe(DENIED)), 1)
        self.assertTrue(llm.describe(DENIED)[0].startswith("  ! Claude requested permissions"))
        self.assertEqual(llm.describe(RESULT), [])

    def test_permission_denied_event_is_one_line(self):
        self.assertEqual(llm.describe(REFUSED),
                         ["  ! denied Bash: A nested command in this command can't be checked before it runs"])

    def test_unexpected_shapes_are_skipped(self):
        for ev in ({"type": "assistant", "message": "text"}, {"type": "user", "message": {"content": ["x"]}}, []):
            self.assertEqual(llm.describe(ev), [])

    def test_render_log_with_denial(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "implement.log"
            p.write_text("\n".join(json.dumps(e) for e in (TOOL, REFUSED, RESULT)) + "\n")
            lines = llm.render_log(p, cwd="/ws")
        self.assertIn("  ! denied Bash: A nested command in this command can't be checked before it runs", lines)
        self.assertEqual(lines[-1], "= done: turns=3 $0.0123 denied=1")

    def test_render_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "implement.log"
            p.write_text("\n".join(json.dumps(e) for e in (TOOL, DENIED, TEXT, RESULT)) + "\nnot json\n")
            lines = llm.render_log(p, cwd="/ws")
        self.assertEqual(lines[0], "Read todo/store.py")
        self.assertIn("  > Done.", lines)
        self.assertEqual(lines[-1], "= done: turns=3 $0.0123 denied=1")


class Stream(unittest.TestCase):
    def test_returns_result_and_writes_log(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "runs" / "implement.log"
            res = llm._stream(fake([TOOL, RESULT]), "prompt", cwd=d, step="implement", timeout=30, path=log)
            self.assertEqual(res["structured_output"], {"ok": True})
            self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_denial_does_not_stop_the_stream(self):
        with tempfile.TemporaryDirectory() as d:
            res = llm._stream(fake([TOOL, REFUSED, RESULT]), "p", cwd=d, step="x", timeout=30, path=Path(d) / "x.log")
            self.assertEqual(res["session_id"], "s1")

    def test_no_result_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(llm.LLMError):
                llm._stream(fake([TOOL]), "p", cwd=d, step="x", timeout=30, path=Path(d) / "x.log")

    def test_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(llm.LLMError, "timed out"):
                llm._stream(fake([RESULT], sleep=5), "p", cwd=d, step="x", timeout=0.5, path=Path(d) / "x.log")
