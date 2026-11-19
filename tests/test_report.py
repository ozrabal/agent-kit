"""report: per-ticket cost, tokens and rounds from .agent/runs/<n>/ files."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import report  # noqa: E402


def call(step, usd, ts=100, **tokens):
    return {"ts": ts, "step": step, "model": "sonnet", "usd": usd, "in": tokens.get("i", 0),
            "cache_r": tokens.get("cr", 0), "out": tokens.get("o", 0)}


def make_run(runs, number, state, calls):
    d = runs / str(number)
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps(state))
    (d / "cost.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_run_sums_cost_tokens_and_rounds(self):
        make_run(self.runs, 7, {"key": "#7", "title": "Add remove", "status": "pr_open",
                                "gates": {"ok": True, "results": []}},
                 [call("implement", 1.0, i=10, cr=100, o=5), call("fix-gates:1", 0.25, i=2, cr=50, o=1),
                  call("review", 0.5, i=3, o=2), call("fix-review:1", 0.25), call("review", 0.5)])
        [row] = report.collect(self.runs)["rows"]
        self.assertEqual(row["key"], "#7")
        self.assertEqual(row["title"], "Add remove")
        self.assertEqual(row["status"], "pr_open")
        self.assertEqual(row["usd"], 2.5)
        self.assertEqual(row["calls"], 5)
        self.assertEqual((row["in"], row["cache_r"], row["out"]), (15, 150, 8))
        self.assertEqual(row["fix_rounds"], 1)
        self.assertEqual(row["review_rounds"], 2)
        self.assertEqual(row["gates"], "pass")

    def test_since_keeps_only_newer_calls_and_runs(self):
        make_run(self.runs, 1, {"status": "done"}, [call("implement", 1.0, ts=50)])
        make_run(self.runs, 2, {"status": "pr_open"}, [call("implement", 1.0, ts=50), call("review", 0.5, ts=200)])
        (self.runs / "kb-cost.jsonl").write_text(json.dumps(call("summarize", 9.0)) + "\n")
        (self.runs / "notes").mkdir()
        rep = report.collect(self.runs, since=100)
        self.assertEqual([r["ticket"] for r in rep["rows"]], [2])
        self.assertEqual(rep["total"]["usd"], 0.5)
        self.assertEqual(rep["rows"][0]["key"], "#2")
        self.assertEqual(rep["rows"][0]["gates"], "-")

    def test_broken_cost_line_is_skipped(self):
        make_run(self.runs, 5, {}, [call("implement", 1.0)])
        with (self.runs / "5" / "cost.jsonl").open("a") as f:
            f.write("{not json\n")
        self.assertEqual(report.collect(self.runs)["total"]["usd"], 1.0)

    def test_missing_runs_dir_is_empty(self):
        rep = report.collect(self.runs / "nope")
        self.assertEqual(rep["rows"], [])
        self.assertEqual(report.render(rep), "no runs")

    def test_render_has_row_and_total(self):
        make_run(self.runs, 3, {"key": "#3", "status": "needs_human", "gates": {"ok": False}},
                 [call("implement", 1.25, i=1000, o=20), call("fix-gates:1", 0.5), call("fix-gates:2", 0.5)])
        lines = report.render(report.collect(self.runs)).splitlines()
        self.assertEqual(lines[0].split(), ["ticket", "status", "usd", "calls", "fix", "review", "gates",
                                            "in", "cache_r", "out"])
        self.assertEqual(lines[1].split(), ["#3", "needs_human", "2.25", "3", "2", "0", "FAIL", "1000", "0", "20"])
        self.assertEqual(lines[-1].split(), ["total", "2.25", "3", "2", "0", "1000", "0", "20"])


if __name__ == "__main__":
    unittest.main()
