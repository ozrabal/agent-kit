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

    def test_follow_up_counts(self):
        state = {"status": "needs_human", "poll_count": 61, "polls": [{"ts": 1}],
                 "triage": [{"id": 1, "action": "fix"}, {"id": 2, "action": "reply"}, {"id": 3, "action": "ignore"}],
                 "needs_human": {"ts": 5, "reason": "CI rounds used: 2 of 2"}}
        make_run(self.runs, 7, state, [call("triage", 0.01), call("triage", 0.02), call("fix-comments:1", 0.5),
                                       call("fix-ci:1", 0.4), call("fix-ci:2", 0.4), call("fix-gates:1", 0.1)])
        [row] = report.collect(self.runs)["rows"]
        self.assertEqual({k: row[k] for k in ("polls", "triaged", "triage_usd", "comment_fixes", "ci_fixes",
                                              "needs_human", "fix_rounds")},
                         {"polls": 61, "triaged": 3, "triage_usd": 0.03, "comment_fixes": 1, "ci_fixes": 2,
                          "needs_human": 1, "fix_rounds": 1})
        for k in ("polls", "triaged", "triage_usd", "comment_fixes", "ci_fixes", "needs_human"):
            self.assertIn(k, report.METRICS)
        self.assertIn("triage_usd", report.MONEY)
        self.assertIn("ci_fix", report.render(report.collect(self.runs)))

    def test_follow_up_log_lines(self):
        state = {"polls": [{"ts": 0, "comments": 0, "ci": "pending", "sha": "abc1234", "note": "CI pending on abc1234"},
                           {"ts": 0, "error": "HTTP 502"}],
                 "triage": [{"id": 11, "action": "fix", "reason": "asks to rename", "reply_draft": "", "ts": 0}],
                 "follow_ups": [{"ts": 0, "kind": "comments", "round": 1, "step": "fix-comments:1",
                                 "fresh_session": True, "ids": [11], "result": "pushed", "commit": "abc9999"}],
                 "needs_human": {"ts": 0, "reason": "CI rounds used: 2 of 2"},
                 "overrides": [{"ts": 0, "was": "needs_human", "ci_rounds": 2, "comment_rounds": 0}]}
        self.assertEqual(report.run_lines(state), [
            "## follow-up",
            "poll: 0 new comment(s), CI pending on abc1234",
            "poll: error: HTTP 502",
            "triage #11: fix (asks to rename)",
            "fix-comments:1 (comments 11, fresh session): pushed abc9999",
            "needs a human: CI rounds used: 2 of 2",
            "override: needs_human cleared by `agent-kit address` (ci rounds 2, comment rounds 0)",
        ])

    def test_hook_calls_counted_per_worktree(self):
        make_run(self.runs, 7, {"status": "pr_open", "ws": "/wt/7-add"}, [call("implement", 1.0)])
        make_run(self.runs, 8, {"status": "pr_open", "ws": "/wt/8-other"}, [call("implement", 1.0)])
        hook = [{"event": "post-edit", "outcome": "block", "cwd": "/wt/7-add"},
                {"event": "post-edit", "outcome": "pass", "cwd": "/wt/7-add/todo"},
                {"event": "stop", "outcome": "block", "cwd": "/wt/7-add"},
                {"event": "stop", "outcome": "block", "cwd": "/wt/7-add"},
                {"event": "stop", "outcome": "give_up", "cwd": "/wt/7-add"},
                {"event": "stop", "outcome": "error", "cwd": "/wt/7-add"},
                {"event": "stop", "outcome": "block", "cwd": "/wt/7-additional"},
                {"event": "stop", "outcome": "pass", "cwd": "/somewhere/else"}]
        (self.runs / "hooks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in hook))
        a, b = report.collect(self.runs)["rows"]
        self.assertEqual((a["hook_calls"], a["edit_blocks"], a["stop_blocks"], a["hook_errors"]), (6, 1, 2, 1))
        self.assertEqual((b["hook_calls"], b["edit_blocks"], b["stop_blocks"], b["hook_errors"]), (0, 0, 0, 0))

    def test_guard_denials_counted(self):
        make_run(self.runs, 5, {"status": "pr_open", "ws": "/wt/5"}, [call("implement", 1.0)])
        hook = [{"event": "guard", "outcome": "block", "cwd": "/wt/5"}, {"event": "guard", "outcome": "pass", "cwd": "/wt/5"}]
        (self.runs / "hooks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in hook))
        [a] = report.collect(self.runs)["rows"]
        self.assertEqual((a["hook_calls"], a["guard_blocks"]), (2, 1))

    def test_hook_calls_matched_by_session_when_tickets_share_a_folder(self):
        make_run(self.runs, 7, {"status": "pr_open", "ws": "/repo", "sessions": [{"step": "implement", "id": "a"}]},
                 [call("implement", 1.0)])
        make_run(self.runs, 8, {"status": "pr_open", "ws": "/repo", "sessions": [{"step": "implement", "id": "b"}]},
                 [call("implement", 1.0)])
        hook = [{"event": "stop", "outcome": "block", "cwd": "/repo", "session": "a"},
                {"event": "stop", "outcome": "pass", "cwd": "/repo", "session": "b"}]
        (self.runs / "hooks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in hook))
        a, b = report.collect(self.runs)["rows"]
        self.assertEqual((a["hook_calls"], a["stop_blocks"]), (1, 1))
        self.assertEqual((b["hook_calls"], b["stop_blocks"]), (1, 0))

    def test_hook_calls_respect_since(self):
        make_run(self.runs, 7, {"status": "pr_open", "ws": "/wt/7"}, [call("implement", 1.0, ts=200)])
        hook = [{"ts": 50, "event": "stop", "outcome": "block", "cwd": "/wt/7"},
                {"ts": 250, "event": "stop", "outcome": "block", "cwd": "/wt/7"}]
        (self.runs / "hooks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in hook))
        self.assertEqual(report.collect(self.runs, since=100)["rows"][0]["stop_blocks"], 1)

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
        self.assertEqual(lines[0].split(), ["ticket", "status", "usd", "calls", "fix", "review", "escal", "rv_skip", "rv_light", "rv_full",
                                        "rv_usd", "rv_in", "rv_cache_r", "polls", "triaged", "tr_usd", "cm_fix", "ci_fix", "human", "rf_chk", "rf_fail", "rf_prop", "rf_usd", "rf_appl", "rf_split", "par", "lk_stale", "lk_busy", "gates", "tdd", "hooks",
                                            "edit_blk", "stop_blk", "guard_blk", "hook_err", "in", "cache_r", "out"])
        self.assertEqual(lines[1].split(), ["#3", "needs_human", "2.25", "3", "2", "0", "0", "0", "0", "0", "0.00", "0", "0", "0", "0", "0.00", "0", "0", "0", "0", "0", "0", "0.00", "0", "0", "0", "0", "0", "FAIL", "-", "0", "0", "0", "0", "0",
                                            "1000", "0", "20"])
        self.assertEqual(lines[-1].split(), ["total", "2.25", "3", "2", "0", "0", "0", "0", "0", "0.00", "0", "0", "0", "0", "0.00", "0", "0", "0", "0", "0", "0", "0.00", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "1000", "0", "20"])

    def test_review_decisions_and_review_cost(self):
        def rv(tier):
            return {"pass": True, "plan": {"run": tier != "skip", "model": None, "reason": "r", "tier": tier}}
        reviews = [rv("skip"), rv("light"), rv("full"), rv("light"), {"pass": True, "criteria": []}]  # last: no plan (older run)
        make_run(self.runs, 7, {"key": "#7", "reviews": reviews},
                 [call("implement", 1.0, i=10, cr=100), call("review", 0.5, i=3, cr=30), call("review", 0.25, i=1, cr=6)])
        make_run(self.runs, 8, {"key": "#8"}, [call("implement", 1.0)])  # older run, no plans
        rep = report.collect(self.runs)
        row = rep["rows"][0]
        self.assertEqual((row["review_skipped"], row["review_light"], row["review_full"]), (1, 2, 1))
        self.assertEqual((row["review_usd"], row["review_in"], row["review_cache_r"]), (0.75, 4, 36))
        self.assertEqual(rep["rows"][1]["review_skipped"], 0)
        self.assertEqual(rep["total"]["review_light"], 2)
        for m in ("review_skipped", "review_light", "review_full", "review_usd", "review_cache_r"):
            self.assertIn(m, report.METRICS)



class RefineParallel(unittest.TestCase):
    """#17: refine checks/proposals/applies, parallel batches and lock events are counted and listed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)

    def test_counts(self):
        make_run(self.runs, 158, {"key": "#158", "title": "Make the CLI nicer", "refine_checks": 3,
                                  "refine": {"ok": False, "mode": "auto", "reasons": ["no 'acceptance criteria' section"],
                                             "proposed": True, "source": "llm", "split": 2, "criteria_proposed": 0, "questions": 1},
                                  "refine_apply": {"kind": "split", "created": [301, 302], "done": True}},
                 [call("refine", 0.02)])
        make_run(self.runs, 153, {"key": "#153", "status": "pr_open", "refine_checks": 1, "refine": {"ok": True},
                                  "parallel": {"batch": [153, 156], "exit": 0},
                                  "locks": [{"event": "stale", "pid": 9}, {"event": "taken", "pid": 10},
                                            {"event": "released", "pid": 10}, {"event": "busy", "pid": 11}]},
                 [call("implement", 0.05)])
        rows = {r["ticket"]: r for r in report.collect(self.runs)["rows"]}
        self.assertEqual({k: rows[158][k] for k in report.REFINE + report.PARALLEL},
                         {"refine_checks": 3, "refine_failed": 1, "refine_proposed": 1, "refine_usd": 0.02,
                          "refine_applied": 1, "split_created": 2, "parallel": 0, "lock_stale": 0, "lock_busy": 0})
        self.assertEqual({k: rows[153][k] for k in report.REFINE + report.PARALLEL},
                         {"refine_checks": 1, "refine_failed": 0, "refine_proposed": 0, "refine_usd": 0,
                          "refine_applied": 0, "split_created": 0, "parallel": 1, "lock_stale": 1, "lock_busy": 1})
        self.assertIn("refine_usd", report.MONEY)
        self.assertTrue(set(report.REFINE + report.PARALLEL) <= set(report.METRICS))
        self.assertTrue(set(report.REFINE + report.PARALLEL) <= {k for _, k in report.COLUMNS})
        self.assertIn("rf_usd", report.render(report.collect(self.runs)))

    def test_single_pick_is_not_a_parallel_run(self):
        make_run(self.runs, 9, {"parallel": {"batch": [9], "slots": 2, "left": []}}, [])
        self.assertEqual(report.collect(self.runs)["rows"][0]["parallel"], 0)

    def test_log_lines(self):
        lines = report.run_lines({
            "refine": {"ok": False, "mode": "auto", "reasons": ["no 'acceptance criteria' section"], "proposed": True,
                       "source": "llm", "split": 2, "criteria_proposed": 0, "questions": 1},
            "refine_checks": 2, "refine_apply": {"kind": "split", "created": [301, 302], "done": True},
            "parallel": {"batch": [153, 156], "slots": 2, "left": [{"number": 154, "why": "waits on #153"}],
                         "pid": 77, "exit": 0, "secs": 41.5},
            "locks": [{"ts": 1, "event": "stale", "pid": 9}, {"ts": 2, "event": "taken", "pid": 10}]})
        self.assertEqual(lines, [
            "## refine",
            "auto: NOT READY (no 'acceptance criteria' section), 2 check(s)",
            "proposal posted (llm): 0 criteria, 2 split, 1 question(s)",
            "applied: split -> #301, #302",
            "## parallel",
            "batch #153, #156 in 2 slot(s); left out: #154 waits on #153",
            "child pid 77: exit 0 after 41.5s",
            "## locks",
            "stale pid 9", "taken pid 10"])


if __name__ == "__main__":
    unittest.main()
