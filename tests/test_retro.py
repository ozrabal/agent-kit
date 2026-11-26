"""retro: aggregate repeated gate errors, review findings and denials across run logs."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit.config import DEFAULTS  # noqa: E402
from agentkit import cli, kb  # noqa: E402
from agentkit.kb import parse_rule  # noqa: E402
from agentkit.llm import LLMError  # noqa: E402
from agentkit.retro import SCHEMA, aggregate, collect, config_lines, llm_input, normalize, plan_rules, render, repeated, rule_text, run_facts  # noqa: E402
from agentkit.retro import run as retro_run  # noqa: E402
from agentkit.util import error_lines, read_jsonl  # noqa: E402


class ErrorLinesTest(unittest.TestCase):
    def test_first_lines_that_explain_the_failure(self):
        out = ("collected 3 items\n\n\x1b[31mFAILED tests/test_store.py::test_add - AssertionError\x1b[0m\n"
               "  ok line\nE   assert 2 == 1\nTraceback (most recent call last):\n")
        self.assertEqual(error_lines(out, 2),
                         ["FAILED tests/test_store.py::test_add - AssertionError", "E   assert 2 == 1"])

    def test_no_error_line_falls_back_to_the_last_lines(self):
        self.assertEqual(error_lines("building\n\nexit 2\n", 1), ["exit 2"])

    def test_empty_output(self):
        self.assertEqual(error_lines("", 3), [])
        self.assertEqual(error_lines(None, 3), [])


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "retro-runs"  # #167, #170: real sandbox runs, trimmed
WS = "/Users/me/.agent-wt/proj/12-add-x"


class NormalizeTest(unittest.TestCase):
    def test_run_folder_prefix_is_stripped_to_a_repo_path(self):
        self.assertEqual(normalize(f"{WS}/todo/store.py: error", [WS]), "todo/store.py: error")
        self.assertEqual(normalize(f"cd {WS} failed", [WS]), "cd . failed")

    def test_temp_dirs(self):
        self.assertEqual(normalize("no such file /tmp/pytest-of-me/x1/data.json", []), "no such file <tmp>")
        self.assertEqual(normalize("in /private/var/folders/ab/T/tmpq/x.py: boom", []), "in <tmp>: boom")

    def test_line_and_column_numbers(self):
        self.assertEqual(normalize("todo/cli.py:12:5: E501 line too long", []), "todo/cli.py:N:N: E501 line too long")
        self.assertEqual(normalize('File "todo/cli.py", line 42, in main', []), 'File "todo/cli.py", line N, in main')

    def test_hex_ids(self):
        self.assertEqual(normalize("object at 0x10a3f2b80 in commit 3f9c2a1d", []), "object at <hex> in commit <hex>")

    def test_numbers_and_durations(self):
        self.assertEqual(normalize("assert 2 == 1 after 0.03s (12ms), 1795610880", []), "assert N == N after N (N), N")

    def test_numbers_inside_names_and_quoted_values_are_kept(self):
        self.assertEqual(normalize("test_add2 got 'buy milk 2' in utf8", []), "test_add2 got 'buy milk 2' in utf8")

    def test_number_before_a_full_stop(self):
        self.assertEqual(normalize("assert 2 == 1.", []), "assert N == N.")

    def test_apostrophes_are_not_quotes(self):
        self.assertEqual(normalize("doesn't match 12 at line 5, can't 7", []), "doesn't match N at line N, can't N")

    def test_root_only_at_a_path_boundary(self):
        self.assertEqual(normalize("/repo2/x.py and /x/repo/y.py and /repo/z.py", ["/repo"]),
                         "/repo2/x.py and /x/repo/y.py and z.py")

    def test_temp_dir_only_at_the_start_of_a_path(self):
        self.assertEqual(normalize("/home/u/proj/tmp/x.py", []), "/home/u/proj/tmp/x.py")

    def test_uppercase_hex(self):
        self.assertEqual(normalize("id ABCDEF1234 at 0X1F", []), "id <hex> at <hex>")

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize("  E   assert   x  ", []), "E assert x")


def gate_out(name, *lines):
    return {"name": name, "ok": False, "secs": 1.0, "output": "\n".join(lines)}


def write_run(runs, number, state, logs=None):
    """A run dir <runs>/<number>/ with state.json and stream logs {step: [events]}."""
    d = Path(runs) / str(number)
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"number": number, "key": f"#{number}", **state}))
    for step, events in (logs or {}).items():
        (d / f"{step}.log").write_text("".join(json.dumps(e) + "\n" for e in events))
    return d


def denied_result(*denials):
    return {"type": "result", "is_error": False, "num_turns": 3, "total_cost_usd": 0.01,
            "permission_denials": list(denials)}


class RunFactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.runs = self.root / ".agent" / "runs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_errors_from_each_recorded_failing_round(self):
        d = write_run(self.runs, 4, {"ws": WS, "gate_failures": [
            {"round": 0, "results": [gate_out("test", "collected 3", f"FAILED {WS}/tests/test_store.py::test_add_twice",
                                               "E   assert 2 == 1", "E   where 2 = len(x)")]},
            {"round": 1, "results": [gate_out("lint", "todo/cli.py:3:1: F401 'os' imported but unused")]}],
            "gates": {"ok": True, "results": [{"name": "test", "ok": True, "secs": 1}]}})
        f = run_facts(d, json.loads((d / "state.json").read_text()), [], [], n_lines=2)
        self.assertEqual(f["ticket"], 4)
        self.assertEqual(f["errors"], [
            {"gate": "test", "text": "FAILED tests/test_store.py::test_add_twice"},
            {"gate": "test", "text": "E assert N == N"},
            {"gate": "lint", "text": "todo/cli.py:N:N: F401 'os' imported but unused"}])

    def test_old_runs_fall_back_to_the_final_gates_and_the_failure_reason(self):
        state = {"ws": WS, "gates": {"ok": False, "results": [gate_out("test", "E   assert 3 == 1")]},
                 "failure": {"reason": "gates still failing after fix rounds", "details": "FAIL test (1s)"}}
        f = run_facts(write_run(self.runs, 5, state), state, [], [])
        self.assertEqual(f["errors"], [{"gate": "test", "text": "E assert N == N"}])
        crash = {"failure": {"reason": f"command failed (1): git push\nerror: failed to push some refs to {self.root}"}}
        f = run_facts(write_run(self.runs, 6, crash), crash, [], [])
        self.assertEqual(f["errors"], [{"gate": "run", "text": "command failed (N): git push"},
                                       {"gate": "run", "text": "error: failed to push some refs to ."}])

    def test_hook_block_notes_count_as_gate_errors(self):
        state = {"ws": WS, "sessions": [{"step": "implement", "id": "s1"}]}
        rows = [{"session": "s1", "event": "stop", "outcome": "block",
                 "note": "PASS lint (0.1s)\nFAIL test (0.4s)\n    E   assert 2 == 1\n    1 failed in 0.03s"},
                {"session": "s1", "event": "post-edit", "outcome": "block", "file": "todo/cli.py",
                 "note": f"FAIL compile todo/cli.py (0.0s)\n      File \"{WS}/todo/cli.py\", line 9"},
                {"session": "s1", "event": "stop", "outcome": "pass", "note": ""},
                {"session": "other", "event": "stop", "outcome": "block", "note": "FAIL test (1s)\n    E   boom"}]
        f = run_facts(write_run(self.runs, 7, state), state, [], rows)
        self.assertEqual(f["errors"], [{"gate": "test", "text": "E assert N == N"},
                                       {"gate": "test", "text": "N failed in N"},
                                       {"gate": "compile todo/cli.py", "text": 'File "todo/cli.py", line N'}])

    def test_in_session_gates_block_rows_count_as_gate_errors(self):
        state = {"ws": WS, "sessions": [{"step": "implement", "id": "s1"}]}
        rows = [{"session": "s1", "event": "gates", "outcome": "block", "note": "FAIL test (0.4s)\n    E   assert 2 == 1"},
                {"session": "s1", "event": "gates", "outcome": "pass"}]
        f = run_facts(write_run(self.runs, 7, state), state, [], rows)
        self.assertEqual(f["errors"], [{"gate": "test", "text": "E assert N == N"}])

    def test_review_findings_including_tdd_ones(self):
        state = {"ws": WS, "reviews": [
            {"findings": [{"severity": "major", "file": "todo/store.py", "line": 12,
                           "issue": "Duplicate check is case-sensitive; 'Milk' and 'milk' both pass", "fix": "x"}]},
            {"findings": [{"severity": "major", "file": "tests/test_store.py",
                           "issue": "tests pass without the change (gate test on the base): tests/test_store.py",
                           "fix": "y"}]}]}
        f = run_facts(write_run(self.runs, 8, state), state, [], [])
        self.assertEqual(f["findings"], [
            {"severity": "major", "file": "todo/store.py",
             "text": "Duplicate check is case-sensitive; 'Milk' and 'milk' both pass"},
            {"severity": "major", "file": "tests/test_store.py",
             "text": "tests pass without the change (gate test on the base): tests/test_store.py"}])

    def test_denials_from_stream_logs_counted_once_and_guard_blocks(self):
        state = {"ws": WS, "sessions": [{"step": "implement", "id": "s1"}]}
        logs = {"implement": [
                    {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "message": "not allowed"},
                    denied_result({"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "npm run build -- --prod"}},
                                  {"tool_name": "WebFetch", "tool_use_id": "t2", "tool_input": {"url": "https://x"}})],
                "fix-gates:1": [{"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
                                 "message": "A nested command in this command can't be checked"}]}
        rows = [{"session": "s1", "event": "guard", "outcome": "block", "tool": "Bash", "command": "git push origin x",
                 "note": "blocked: git push"}]
        f = run_facts(write_run(self.runs, 9, state, logs), state, [], rows)
        self.assertCountEqual(f["denials"], [
            {"source": "claude", "tool": "Bash", "detail": "npm run"},
            {"source": "claude", "tool": "WebFetch", "detail": "https://x"},
            {"source": "claude", "tool": "Bash", "detail": "A nested command in this command can't be checked"},
            {"source": "guard", "tool": "Bash", "detail": "git push"}])

    def test_every_session_in_an_appended_log_counts(self):
        state = {"ws": WS}
        logs = {"implement": [
            denied_result({"tool_name": "Bash", "tool_input": {"command": "npm test"}}),
            {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "message": "second session"},
            denied_result(),
            {"type": "system", "subtype": "permission_denied", "tool_name": "Read", "message": "crashed session"}]}
        f = run_facts(write_run(self.runs, 11, state, logs), state, [], [])
        self.assertEqual(f["denials"], [{"source": "claude", "tool": "Bash", "detail": "npm test"},
                                        {"source": "claude", "tool": "Bash", "detail": "second session"},
                                        {"source": "claude", "tool": "Read", "detail": "crashed session"}])

    def test_bash_denials_are_normalized_so_runs_group(self):
        state = {"ws": WS}
        logs = {"implement": [denied_result({"tool_name": "Bash", "tool_input": {"command": f"cd {WS}/todo && ls"}})]}
        f = run_facts(write_run(self.runs, 12, state, logs), state, [], [])
        self.assertEqual(f["denials"][0]["detail"], "cd todo")

    def test_denied_counts_without_log_details(self):
        state = {"ws": WS}
        logs = {"implement": [denied_result({"tool_name": "Bash", "tool_input": {"command": "npm test"}})]}
        calls = [{"step": "implement", "usd": 0.05, "denied": 1}, {"step": "triage", "usd": 0.001, "denied": 2}]
        f = run_facts(write_run(self.runs, 13, state, logs), state, calls, [])
        self.assertEqual(f["denials"], [{"source": "claude", "tool": "Bash", "detail": "npm test"},
                                        {"source": "claude", "tool": "?", "detail": "(no details in the logs)"},
                                        {"source": "claude", "tool": "?", "detail": "(no details in the logs)"}])

    def test_cost_per_step_and_counts(self):
        state = {"ws": WS, "tdd_checks": [{"status": "passes_on_base"}, {"status": "red"}, {"status": "skipped"},
                                          {"status": "no_tests"}]}
        calls = [{"step": "implement", "usd": 0.05, "secs": 10}, {"step": "fix-gates:1", "usd": 0.02, "secs": 4},
                 {"step": "fix-gates:2", "usd": 0.03, "secs": 6}]
        f = run_facts(write_run(self.runs, 10, state), state, calls, [])
        self.assertEqual(f["steps"], {"implement": {"calls": 1, "usd": 0.05, "secs": 10},
                                      "fix-gates": {"calls": 2, "usd": 0.05, "secs": 10}})
        self.assertEqual(f["tdd_failed"], 2)  # passes_on_base and no_tests; skipped is no verdict


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name) / "repo" / ".agent" / "runs"
        shutil.copytree(FIXTURE, self.runs)
        state = {"ws": WS, "sessions": [{"step": "implement", "id": "s9"}], "fix_rounds": 1,
                 "gate_failures": [{"ts": 1795700050, "round": 0, "results": [gate_out("test", "E   assert 2 == 1")]}]}
        d = write_run(self.runs, 171, state)
        (d / "cost.jsonl").write_text("".join(json.dumps(c) + "\n" for c in [
            {"ts": 1795700000, "step": "implement", "usd": 0.05, "secs": 10},
            {"ts": 1795700100, "step": "fix-gates:1", "usd": 0.02, "secs": 4}]))
        with (self.runs / "hooks.jsonl").open("a") as f:
            for ts, note in ((1795600000, "FAIL lint (1s)\n    old: unused import"),
                             (1795700050, "FAIL test (1s)\n    E   assert 5 == 1")):
                f.write(json.dumps({"ts": ts, "session": "s9", "event": "stop", "outcome": "block", "note": note}) + "\n")
        (self.runs / "kb-cost.jsonl").write_text('{"ts": 1, "step": "summarize", "usd": 0.01}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_run_with_its_facts_and_report_counts(self):
        facts = collect(self.runs)
        self.assertEqual([f["ticket"] for f in facts], [167, 170, 171])
        real = facts[0]
        self.assertEqual(real["steps"], {"implement": {"calls": 1, "usd": 0.0697, "secs": 13.4},
                                         "review": {"calls": 1, "usd": 0.0243, "secs": 3.6}})
        self.assertEqual((real["errors"], real["findings"], real["denials"]), ([], [], []))
        self.assertEqual(real["counts"]["usd"], 0.094)
        mine = facts[2]
        self.assertEqual(mine["errors"], [{"gate": "test", "text": "E assert N == N"},
                                          {"gate": "lint", "text": "old: unused import"},
                                          {"gate": "test", "text": "E assert N == N"}])
        self.assertEqual({k: mine["counts"][k] for k in ("fix_rounds", "stop_blocks", "guard_blocks")},
                         {"fix_rounds": 1, "stop_blocks": 2, "guard_blocks": 0})

    def test_since_keeps_newer_calls_and_hook_rows_only(self):
        facts = collect(self.runs, since=1795610890)
        self.assertEqual([f["ticket"] for f in facts], [167, 170, 171])
        self.assertEqual(list(facts[0]["steps"]), ["review"])
        self.assertEqual(facts[2]["errors"], [{"gate": "test", "text": "E assert N == N"},
                                              {"gate": "test", "text": "E assert N == N"}])
        self.assertEqual(facts[2]["counts"]["stop_blocks"], 1)
        self.assertEqual([f["ticket"] for f in collect(self.runs, since=1795700001)], [171])

    def test_since_drops_state_entries_recorded_before_it(self):
        finding = {"severity": "major", "file": "todo/a.py", "issue": "old finding"}
        state = {"ws": WS, "sessions": [{"step": "implement", "id": "s8"}],
                 "gate_failures": [{"ts": 1795800000, "round": 0, "results": [gate_out("test", "E   old")]},
                                   {"ts": 1795900000, "round": 1, "results": [gate_out("test", "E   new")]},
                                   {"round": 2, "results": [gate_out("test", "E   no ts")]}],
                 "reviews": [{"ts": 1795800000, "findings": [finding]},
                             {"ts": 1795900000, "findings": [{**finding, "issue": "new finding"}]},
                             {"findings": [{**finding, "issue": "no ts"}]}],
                 "tdd_checks": [{"ts": 1795800000, "status": "no_tests"}, {"ts": 1795900000, "status": "passes_on_base"},
                                {"status": "no_tests"}],
                 "failure": {"ts": 1795800000, "reason": "command failed (1): git push"}}
        d = write_run(self.runs, 172, state)
        (d / "cost.jsonl").write_text(json.dumps({"ts": 1795900000, "step": "address", "usd": 0.01}) + "\n")
        [f] = [f for f in collect(self.runs, since=1795850000) if f["ticket"] == 172]
        self.assertEqual(f["errors"], [{"gate": "test", "text": "E new"}])
        self.assertEqual([x["text"] for x in f["findings"]], ["new finding"])
        self.assertEqual(f["tdd_failed"], 1)
        crash = {"failure": {"ts": 1795900000, "reason": "command failed (1): git push"}}
        d = write_run(self.runs, 173, crash)
        (d / "cost.jsonl").write_text(json.dumps({"ts": 1795900000, "step": "implement", "usd": 0.01}) + "\n")
        [f] = [f for f in collect(self.runs, since=1795850000) if f["ticket"] == 173]
        self.assertEqual(f["errors"], [{"gate": "run", "text": "command failed (N): git push"}])
        [f] = [f for f in collect(self.runs) if f["ticket"] == 172]
        self.assertEqual(len(f["errors"]), 4)
        self.assertEqual(len(f["findings"]), 3)
        self.assertEqual(f["tdd_failed"], 3)

    def test_no_runs_dir(self):
        self.assertEqual(collect(Path(self.tmp.name) / "none"), [])


def facts(ticket, errors=(), findings=(), denials=(), steps=None, **counts):
    return {"ticket": ticket, "key": f"#{ticket}", "errors": [{"gate": g, "text": t} for g, t in errors],
            "findings": [{"severity": sv, "file": fl, "text": t} for sv, fl, t in findings],
            "denials": [{"source": so, "tool": tl, "detail": dt} for so, tl, dt in denials],
            "steps": steps or {}, "tdd_failed": counts.pop("tdd_failed", 0), "counts": counts}


class AggregateTest(unittest.TestCase):
    DUP = ("test", "E assert N == N")

    def test_errors_repeated_in_distinct_runs(self):
        agg = aggregate([facts(1, errors=[self.DUP, self.DUP, ("lint", "F401 unused")]),
                         facts(2, errors=[self.DUP]), facts(3, errors=[("lint", "E501 long")])], min_runs=2, top=10)
        self.assertEqual(agg["runs"], 3)
        self.assertEqual(agg["errors"], [{"gate": "test", "text": "E assert N == N", "runs": [1, 2], "count": 3}])
        self.assertEqual(agg["single"]["errors"], 2)

    def test_occurrences_in_one_run_are_not_repeated(self):
        agg = aggregate([facts(1, errors=[self.DUP, self.DUP, self.DUP])], min_runs=2, top=10)
        self.assertEqual((agg["errors"], agg["single"]["errors"]), ([], 1))

    def test_min_runs_and_top(self):
        many = [facts(n, errors=[self.DUP] + ([("lint", "F401 unused")] if n < 3 else [])) for n in (1, 2, 3)]
        self.assertEqual([e["gate"] for e in aggregate(many, min_runs=2, top=10)["errors"]], ["test", "lint"])
        self.assertEqual([e["gate"] for e in aggregate(many, min_runs=3, top=10)["errors"]], ["test"])
        self.assertEqual([e["gate"] for e in aggregate(many, min_runs=2, top=1)["errors"]], ["test"])

    def test_findings_by_text_with_severities_and_files(self):
        text = "duplicate check is case-sensitive"
        agg = aggregate([facts(1, findings=[("major", "todo/store.py", text), ("minor", "README.md", "typo")]),
                         facts(2, findings=[("blocker", "todo/cli.py", text)])], min_runs=2, top=10)
        self.assertEqual(agg["findings"], [{"text": text, "runs": [1, 2], "count": 2, "severities": ["blocker", "major"],
                                            "files": ["todo/cli.py", "todo/store.py"]}])
        self.assertEqual(agg["single"]["findings"], 1)

    def test_finding_files_and_dirs_by_runs(self):
        agg = aggregate([facts(1, findings=[("major", "todo/store.py", "a"), ("minor", "todo/store.py", "b")]),
                         facts(2, findings=[("major", "todo/cli.py", "c"), ("major", "-", "no tests added")])],
                        min_runs=2, top=10)
        self.assertEqual(agg["files"], [{"file": "todo/store.py", "runs": [1], "count": 2},
                                        {"file": "todo/cli.py", "runs": [2], "count": 1}])
        self.assertEqual(agg["dirs"], [{"dir": "todo", "runs": [1, 2], "count": 3}])

    def test_denials_listed_with_their_runs(self):
        agg = aggregate([facts(1, denials=[("claude", "Bash", "npm run"), ("guard", "Bash", "git push")]),
                         facts(2, denials=[("claude", "Bash", "npm run")])], min_runs=2, top=10)
        self.assertEqual(agg["denials"], [
            {"source": "claude", "tool": "Bash", "detail": "npm run", "runs": [1, 2], "count": 2},
            {"source": "guard", "tool": "Bash", "detail": "git push", "runs": [1], "count": 1}])

    def test_steps_by_cost_and_summed_counts(self):
        agg = aggregate([facts(1, steps={"implement": {"calls": 1, "usd": 0.05, "secs": 10},
                                         "fix-gates": {"calls": 2, "usd": 0.06, "secs": 8}}, usd=0.11, fix_rounds=2),
                         facts(2, steps={"implement": {"calls": 1, "usd": 0.04, "secs": 9}}, usd=0.04, tdd_failed=1)],
                        min_runs=2, top=10)
        self.assertEqual(agg["steps"], [{"step": "implement", "calls": 2, "usd": 0.09, "secs": 19, "runs": 2},
                                        {"step": "fix-gates", "calls": 2, "usd": 0.06, "secs": 8, "runs": 1}])
        self.assertEqual(agg["counts"], {"usd": 0.15, "fix_rounds": 2, "tdd_failed": 1})

    def test_no_runs(self):
        agg = aggregate([], min_runs=2, top=10)
        self.assertEqual((agg["runs"], agg["errors"], agg["steps"], agg["counts"]), (0, [], [], {"tdd_failed": 0}))


class RenderTest(unittest.TestCase):
    def agg(self):
        text = "duplicate check is case-sensitive"
        return aggregate([
            facts(1, errors=[("test", "E assert N == N")] * 2, findings=[("major", "todo/store.py", text)],
                  denials=[("claude", "Bash", "npm run"), ("guard", "Bash", "git push")],
                  steps={"implement": {"calls": 1, "usd": 0.05, "secs": 10}}, usd=0.05, fix_rounds=2, stop_blocks=1, gates_blocks=3),
            facts(2, errors=[("test", "E assert N == N"), ("lint", "F401")], findings=[("blocker", "todo/cli.py", text)],
                  denials=[("claude", "Bash", "npm run")], steps={"implement": {"calls": 1, "usd": 0.04, "secs": 9},
                                                                   "review": {"calls": 1, "usd": 0.01, "secs": 3}},
                  usd=0.05, tdd_failed=1)], min_runs=2, top=10)

    def test_report_sections(self):
        self.assertEqual(render(self.agg()), "\n".join([
            "retro: 2 runs, $0.10, fix rounds 2, review rounds 0, escalated 0, red/green failed 1",
            "hook blocks: edit 0, stop 1, guard 0, in-session gates 3; after the PR: triaged 0, comment fixes 0, CI fixes 0, needs a human 0",
            "",
            "## Most expensive steps",
            "- implement: $0.09, 2 calls in 2 runs, 19.0s",
            "- review: $0.01, 1 call in 1 run, 3.0s",
            "",
            "## Repeated gate errors (in >= 2 runs)",
            "- [test] E assert N == N: 2 runs (#1, #2), 3 times",
            "1 more seen in one run only",
            "",
            "## Repeated review findings (in >= 2 runs)",
            "- duplicate check is case-sensitive: 2 runs (#1, #2), 2 times; blocker, major; todo/cli.py, todo/store.py",
            "",
            "## Files with findings",
            "- todo/store.py: 1 run (#1), 1 time",
            "- todo/cli.py: 1 run (#2), 1 time",
            "",
            "## Folders with findings",
            "- todo: 2 runs (#1, #2), 2 times",
            "",
            "## Denied tool calls",
            "- Bash `npm run`: 2 runs (#1, #2), 2 times (permission: candidate for implement.allow_bash)",
            "- Bash `git push`: 1 run (#1), 1 time (scope guard)"]))

    def test_empty_sections_say_none(self):
        out = render(aggregate([facts(1)], min_runs=2, top=10))
        self.assertIn("## Repeated gate errors (in >= 2 runs)\n(none)\n", out)
        self.assertTrue(out.endswith("## Denied tool calls\n(none)"))
        self.assertEqual(render(aggregate([], min_runs=2, top=10)), "retro: no runs")

    def test_one_run_groups_only_are_counted_under_none(self):
        out = render(aggregate([facts(1, errors=[("test", "x")], findings=[("major", "a.py", "f")])], 2, 10))
        self.assertIn("## Repeated gate errors (in >= 2 runs)\n(none)\n1 more seen in one run only\n", out)
        self.assertIn("## Repeated review findings (in >= 2 runs)\n(none)\n1 more seen in one run only\n", out)

    def test_only_bash_denials_suggest_allow_bash(self):
        out = render(aggregate([facts(1, denials=[("claude", "WebFetch", "https://x")])], 2, 10))
        self.assertTrue(out.endswith("- WebFetch `https://x`: 1 run (#1), 1 time (permission)"))

    def test_repeated(self):
        self.assertTrue(repeated(self.agg()))
        self.assertFalse(repeated(aggregate([facts(1, errors=[("t", "x")]), facts(2, errors=[("t", "y")])], 2, 10)))
        denied = [facts(n, denials=[("claude", "Bash", "npm run")]) for n in (1, 2)]
        self.assertTrue(repeated(aggregate(denied, 2, 10)))
        self.assertFalse(repeated(aggregate(denied[:1], 2, 10)))


class LlmInputTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULTS["retro"], {"min_runs": 2, "error_lines": 3, "top": 10, "max_chars": 12000,
                                             "known_chars": 3000})
        self.assertEqual((DEFAULTS["models"]["retro"], DEFAULTS["effort"]["retro"], DEFAULTS["budget_usd"]["retro"]),
                         ("sonnet", None, 0.5))

    def test_aggregate_and_known_rules_in_the_prompt(self):
        system, text = llm_input("retro: 2 runs\n## Repeated gate errors", "- rule: no os imports", DEFAULTS)
        self.assertIn("retro: 2 runs\n## Repeated gate errors", text)
        self.assertIn("- rule: no os imports", text)
        self.assertNotIn("$", text + system)
        self.assertIn("allow_bash", system)

    def test_both_parts_are_capped(self):
        cfg = {**DEFAULTS, "retro": {**DEFAULTS["retro"], "max_chars": 100, "known_chars": 50}}
        _, text = llm_input("@" * 500, "~" * 5000, cfg)
        self.assertEqual(text.count("@"), 100)
        self.assertEqual(text.count("~"), 50)
        self.assertIn("[truncated 400 chars]", text)

    def test_nothing_known(self):
        _, text = llm_input("agg", "", DEFAULTS)
        self.assertIn("(none recorded)", text)


def rule(summary, paths=("todo/**",), text="Compare titles with casefold()."):
    return {"summary": summary, "paths": list(paths), "text": text}


class PlanRulesTest(unittest.TestCase):
    def test_rule_file_round_trips_through_the_kb_parser(self):
        r = rule("Duplicate checks:  ignore case\n", ["todo/**", "tests/test_*.py"], "Use casefold().\nWhy: 'Milk' == 'milk'.")
        text = rule_text(r)
        self.assertTrue(text.startswith("---\n"))
        self.assertEqual(parse_rule(text, "x"), {"name": "x", "paths": ["todo/**", "tests/test_*.py"],
                                                 "summary": "Duplicate checks: ignore case",
                                                 "text": "Use casefold().\nWhy: 'Milk' == 'milk'."})

    def test_summary_with_brackets_and_quotes_round_trips(self):
        for summary in ('[api] use "X"', "'quoted' rule's end'", "a: b"):
            self.assertEqual(parse_rule(rule_text(rule(summary)), "x")["summary"], summary)

    def test_a_hand_written_rule_with_a_list_summary_does_not_crash(self):
        old = [parse_rule("---\nsummary: [a, b]\n---\nbody\n", "odd")]
        write, _ = plan_rules([rule("New rule", [])], old)
        self.assertEqual(len(write), 1)

    def test_global_rule_has_no_paths(self):
        self.assertEqual(parse_rule(rule_text(rule("Run the gates", [])), "x")["paths"], [])

    def test_new_rules_get_a_retro_name(self):
        write, skipped = plan_rules([rule("Duplicate checks ignore case")], [])
        self.assertEqual([(n, r["summary"]) for n, r in write], [("retro-duplicate-checks-ignore-case", "Duplicate checks ignore case")])
        self.assertEqual(skipped, [])

    def test_long_names_are_cut_at_a_word(self):
        write, _ = plan_rules([rule("Check existing tests before rejecting duplicate titles")], [])
        self.assertEqual(write[0][0], "retro-check-existing-tests-before-rejecting")

    def test_existing_name_is_never_overwritten(self):
        old = [parse_rule(rule_text(rule("Something else")), "retro-duplicate-checks-ignore-case")]
        write, skipped = plan_rules([rule("Duplicate checks ignore case")], old)
        self.assertEqual(write, [])
        self.assertEqual(skipped, [("Duplicate checks ignore case", "retro-duplicate-checks-ignore-case.md exists")])

    def test_similar_summary_with_the_same_paths_is_a_duplicate(self):
        old = [parse_rule(rule_text(rule("Duplicate checks must ignore case")), "dupes")]
        write, skipped = plan_rules([rule("Duplicate checks ignore case"), rule("Duplicate checks ignore case", ["cli/**"])], old)
        self.assertEqual([r["paths"] for _, r in write], [["cli/**"]])
        self.assertEqual(skipped, [("Duplicate checks ignore case", "like dupes.md")])

    def test_duplicates_within_the_proposal_and_empty_rules(self):
        write, skipped = plan_rules([rule("Same rule"), rule("Same rule"), rule("  ", text="x"), rule("No text", text=" ")], [])
        self.assertEqual(len(write), 1)
        self.assertEqual(skipped, [("Same rule", "retro-same-rule.md exists"), ("", "empty"), ("No text", "empty")])


class ConfigLinesTest(unittest.TestCase):
    def test_known_and_unknown_keys(self):
        lines = config_lines([
            {"key": "implement.allow_bash", "value": '["npm run *"]', "why": "denied in 3 runs"},
            {"key": "workspace.env.NODE_ENV", "value": '"test"', "why": "free-form map"},
            {"key": "loop.max_fix_round", "value": "3", "why": "typo"},
            {"key": "", "value": "1", "why": "no key"}], DEFAULTS)
        self.assertEqual(lines, ['- implement.allow_bash = ["npm run *"]: denied in 3 runs',
                                 '- workspace.env.NODE_ENV = "test": free-form map',
                                 "- loop.max_fix_round = 3: typo (unknown key)",
                                 "-  = 1: no key (unknown key)"])

    def test_none(self):
        self.assertEqual(config_lines([], DEFAULTS), [])


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


PROPOSAL = {"rules": [{"paths": ["todo/**"], "summary": "Duplicate checks ignore case", "text": "Use casefold()."},
                      {"paths": ["todo/**"], "summary": "Keep the store file sorted by id", "text": "Sort on save."}],
            "gotchas": ["Run the tests with python3 -m unittest", "tests share one temp file"],
            "config": [{"key": "implement.allow_bash", "value": '["npm run *"]', "why": "denied in 2 runs"}]}


class RetroRepo(unittest.TestCase):
    """A committed temp repo with a knowledge base (one rule, one gotcha); run dirs under .agent/runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / ".gitignore").write_text(".agent/\n")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["_root"] = str(self.repo)
        kb.ensure_seeds(self.cfg, self.repo)
        (self.repo / ".knowledge" / "gotchas.md").write_text("# Gotchas\n\n- Tests share one temp file (#3)\n")
        (self.repo / ".knowledge" / "rules").mkdir()
        (self.repo / ".knowledge" / "rules" / "store.md").write_text(rule_text(rule("Keep the store file sorted by id")))
        (self.repo / "todo").mkdir()
        (self.repo / "todo" / "store.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.runs = self.repo / ".agent" / "runs"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def failing_runs(self, *numbers):
        for n in numbers:
            d = write_run(self.runs, n, {"gate_failures": [{"ts": 100, "round": 0, "results": [gate_out("test", "E   assert 2 == 1")]}]})
            (d / "cost.jsonl").write_text(json.dumps({"ts": 100, "step": "implement", "usd": 0.05}) + "\n")

    def claude(self, text, **kw):
        self.calls.append((text, kw))
        Path(kw["cost_log"]).parent.mkdir(parents=True, exist_ok=True)
        with open(kw["cost_log"], "a") as f:
            f.write(json.dumps({"ts": 1, "step": kw["step"], "usd": 0.012}) + "\n")
        return self.reply, None

    def retro_rows(self):
        return read_jsonl(self.runs / "retro.jsonl")


class RetroRunTest(RetroRepo):
    def go(self, **kw):
        return retro_run(self.cfg, self.repo, claude=self.claude, **kw)

    def test_no_llm_prints_the_aggregate_and_writes_nothing(self):
        self.failing_runs(1, 2)
        code, out = self.go(use_llm=False)
        self.assertEqual(code, 0)
        self.assertIn("- [test] E assert N == N: 2 runs (#1, #2), 2 times", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True,
                                        text=True).stdout, "")
        row = self.retro_rows()[-1]
        self.assertEqual({k: row[k] for k in ("mode", "since", "runs", "errors", "findings", "denials", "usd")},
                         {"mode": "no-llm", "since": None, "runs": 2, "errors": 1, "findings": 0, "denials": 0, "usd": 0})

    def test_nothing_repeated_skips_the_call(self):
        self.failing_runs(1)
        code, out = self.go()
        self.assertEqual((code, self.calls), (0, []))
        self.assertIn("nothing repeated in >= 2 runs: no LLM call", out)
        self.assertEqual(self.retro_rows()[-1]["mode"], "nothing")

    def test_dirty_knowledge_folder_is_refused_before_the_call(self):
        self.failing_runs(1, 2)
        (self.repo / ".knowledge" / "gotchas.md").write_text("# Gotchas\n\n- edited\n")
        code, out = self.go()
        self.assertEqual((code, self.calls), (1, []))
        self.assertIn(".knowledge has uncommitted changes", out)
        self.assertEqual(self.retro_rows()[-1]["mode"], "refused")

    def test_untracked_knowledge_folder_is_not_refused(self):
        git(self.repo, "rm", "-r", "-q", "--cached", ".knowledge")
        git(self.repo, "commit", "-q", "-m", "untrack")
        self.failing_runs(1, 2)
        self.reply = PROPOSAL
        code, out = self.go()
        self.assertEqual((code, len(self.calls)), (0, 1))
        self.assertIn("untracked: .knowledge/gotchas.md", out)
        self.assertIn("untracked: .knowledge/rules/retro-duplicate-checks-ignore-case.md", out)
        self.assertNotIn("conventions.md", out.split("## Changes")[1])  # untracked, but retro did not touch it

    def test_proposal_written_to_the_working_tree_and_logged(self):
        self.failing_runs(1, 2)
        self.reply = PROPOSAL
        code, out = self.go(since=0)
        self.assertEqual(code, 0)
        (text, kw), = self.calls
        self.assertIn("- [test] E assert N == N", text)
        self.assertIn("- Keep the store file sorted by id (todo/**)", text)  # known rules go along
        self.assertIn("- Tests share one temp file (#3)", text)
        self.assertEqual({k: kw[k] for k in ("step", "model", "lean", "schema", "tools", "budget", "cost_log")},
                         {"step": "retro", "model": "sonnet", "lean": True, "schema": SCHEMA, "tools": "",
                          "budget": 0.5, "cost_log": self.runs / "retro-cost.jsonl"})
        self.assertIn("implement.allow_bash", kw["system"])
        new = self.repo / ".knowledge" / "rules" / "retro-duplicate-checks-ignore-case.md"
        self.assertEqual(parse_rule(new.read_text(), "x")["paths"], ["todo/**"])
        self.assertFalse((self.repo / ".knowledge" / "rules" / "retro-keep-the-store-file-sorted-by-id.md").exists())
        gotchas = (self.repo / ".knowledge" / "gotchas.md").read_text()
        self.assertIn("- Run the tests with python3 -m unittest (retro)", gotchas)
        self.assertEqual(gotchas.count("temp file"), 1)
        self.assertIn("wrote .knowledge/rules/retro-duplicate-checks-ignore-case.md", out)
        self.assertIn("skipped Keep the store file sorted by id: like store.md", out)
        self.assertIn("gotchas: 1 added, 1 already known", out)
        self.assertIn('- implement.allow_bash = ["npm run *"]: denied in 2 runs', out)
        self.assertIn(".knowledge/gotchas.md | 1 +", out)
        self.assertIn("untracked: .knowledge/rules/retro-duplicate-checks-ignore-case.md", out)
        self.assertEqual(self.retro_rows()[-1], {**self.retro_rows()[-1], "mode": "llm", "since": 0, "usd": 0.012,
                                                 "rules": ["retro-duplicate-checks-ignore-case"], "rules_skipped": 1,
                                                 "gotchas": 1, "config": 1})
        kb.update(self.cfg, self.repo, use_llm=False)
        self.assertIn("- Duplicate checks ignore case (todo/**)", (self.repo / ".knowledge" / "rules-digest.md").read_text())

    def test_llm_error_is_logged(self):
        self.failing_runs(1, 2)
        def broken(text, **kw):
            raise LLMError("retro: budget exceeded")
        code, out = retro_run(self.cfg, self.repo, claude=broken)
        self.assertEqual(code, 1)
        self.assertIn("retro: budget exceeded", out)
        self.assertEqual(self.retro_rows()[-1]["mode"], "error")


class RetroCommandTest(RetroRepo):
    def setUp(self):
        super().setUp()
        self.cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.cwd)
        super().tearDown()

    def command(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["retro", *args])
        return code, buf.getvalue()

    def test_no_llm_with_since(self):
        self.failing_runs(1, 2)
        code, out = self.command("--no-llm", "--since", "100")
        self.assertEqual(code, 0)
        self.assertIn("- [test] E assert N == N: 2 runs (#1, #2), 2 times", out)
        self.assertEqual(self.retro_rows()[-1]["since"], 100)
        self.assertEqual(self.command("--no-llm", "--since", "101")[1].splitlines()[0], "retro: no runs")


if __name__ == "__main__":
    unittest.main()
