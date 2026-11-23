"""PR follow-up: new review comments and failed CI. Run: python3 -m unittest tests.test_followup"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit.followup import MARKER, ci_decision, new_comments, status_note  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.llm import LLMError  # noqa: E402
from agentkit.loop import Loop, Run  # noqa: E402


TRUSTED = DEFAULTS["pr"]["trusted_associations"]


def c(id, body="please rename x", author="alice", kind="inline", resolved=False, association="OWNER"):
    return {"id": id, "kind": kind, "author": author, "association": association, "path": "a.py", "line": 3, "body": body,
            "created_at": "2026-11-23T10:00:00Z", "resolved": resolved, "diff_hunk": "@@ -1 +1 @@"}


class NewComments(unittest.TestCase):
    def test_unhandled_human_comment_is_new(self):
        self.assertEqual([x["id"] for x in new_comments([c(1)], [], TRUSTED)], [1])

    def test_handled_id_is_not_new(self):
        self.assertEqual(new_comments([c(1), c(2)], [1], TRUSTED), [c(2)])

    def test_own_marked_comment_is_skipped(self):
        self.assertEqual(new_comments([c(1, body=f"Done.\n\n{MARKER}")], [], TRUSTED), [])

    def test_bot_author_is_skipped(self):
        self.assertEqual(new_comments([c(1, author="github-actions[bot]")], [], TRUSTED), [])

    def test_resolved_thread_is_skipped(self):
        self.assertEqual(new_comments([c(1, resolved=True)], [], TRUSTED), [])

    def test_empty_review_body_is_skipped(self):
        self.assertEqual(new_comments([c(1, body="  \n", kind="review")], [], TRUSTED), [])

    def test_untrusted_author_is_skipped(self):
        self.assertEqual(new_comments([c(1, association="NONE"), c(2, association="CONTRIBUTOR")], [], TRUSTED), [])
        self.assertEqual([x["id"] for x in new_comments([c(3, association="COLLABORATOR")], [], TRUSTED)], [3])

    def test_issue_comment_counts_until_handled(self):
        self.assertEqual([x["id"] for x in new_comments([c(5, kind="issue")], [], TRUSTED)], [5])
        self.assertEqual(new_comments([c(5, kind="issue")], [5], TRUSTED), [])


def chk(name, bucket):
    return {"name": name, "bucket": bucket, "state": bucket.upper(), "link": f"https://x/actions/runs/7/job/{name}"}


class CiDecision(unittest.TestCase):
    def test_no_checks_is_nothing(self):
        self.assertEqual(ci_decision([], "abc", [], 0, 2)["action"], "none")

    def test_green_is_nothing(self):
        self.assertEqual(ci_decision([chk("test", "pass"), chk("lint", "skipping")], "abc", [], 0, 2)["action"], "none")

    def test_pending_waits_even_with_a_failure(self):
        d = ci_decision([chk("test", "fail"), chk("lint", "pending")], "abc", [], 0, 2)
        self.assertEqual(d["action"], "pending")

    def test_failure_on_new_sha_is_fixed(self):
        d = ci_decision([chk("test", "fail"), chk("lint", "pass"), chk("e2e", "cancel")], "abc", [], 1, 2)
        self.assertEqual(d["action"], "fix")
        self.assertEqual([x["name"] for x in d["failed"]], ["test"])  # cancel is not a code failure

    def test_handled_sha_is_not_fixed_again(self):
        self.assertEqual(ci_decision([chk("test", "fail")], "abc", ["abc"], 1, 2)["action"], "handled")

    def test_round_limit_reached(self):
        d = ci_decision([chk("test", "fail")], "def", ["abc"], 2, 2)
        self.assertEqual(d["action"], "limit")
        self.assertIn("2 of 2", d["reason"])


class StatusNote(unittest.TestCase):
    LIMITS = {"max_ci_rounds": 2, "max_comment_rounds": 2}

    def test_no_polls_yet(self):
        self.assertEqual(status_note({"status": "pr_open"}, self.LIMITS), "")

    def test_last_poll_and_rounds(self):
        state = {"status": "pr_open", "poll_count": 4, "ci_rounds": 1, "comment_rounds": 0,
                 "polls": [{"ts": 0, "comments": 2, "ci": "pending", "sha": "abc1234", "note": "CI pending on abc1234"}],
                 "triage": [{"id": 1, "action": "fix"}], "handled_comments": []}
        self.assertEqual(status_note(state, self.LIMITS),
                         "polls=4 last: 2 new comment(s), CI pending on abc1234; 1 fix comment(s) pending; "
                         "rounds ci 1/2 comments 0/2")

    def test_needs_human_reason(self):
        state = {"status": "needs_human", "polls": [{"ts": 0, "error": "HTTP 502"}],
                 "needs_human": {"ts": 0, "reason": "CI rounds used: 2 of 2"}}
        self.assertEqual(status_note(state, self.LIMITS),
                         "polls=1 last: error: HTTP 502; rounds ci 0/2 comments 0/2; needs a human: CI rounds used: 2 of 2")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeTracker:
    """The tracker methods the follow-up pass uses; records what it would post."""

    def __init__(self):
        self.comments, self.checks, self.logs = [], {"sha": "", "checks": [], "failed_runs": []}, {}
        self.posted, self.labels, self.seen, self.broken = [], [], [], set()

    def pr_review_comments(self, pr):
        self.seen.append(pr)
        if pr in self.broken:
            raise RuntimeError("command failed (1): gh api ... HTTP 502")
        return self.comments

    def pr_checks(self, pr):
        return self.checks

    def failed_log(self, run_id):
        return self.logs.get(run_id, "")

    def reply(self, pr, comment_id, body):
        self.posted.append(("reply", comment_id, body))

    def comment(self, pr, body):
        self.posted.append(("comment", None, body))

    def set_status(self, n, old, new):
        self.labels.append((n, old, new))

    def get(self, n):
        return {"number": n, "title": "t", "body": "Make x two.", "labels": [], "state": "OPEN"}


class Follow(unittest.TestCase):
    """Loop.address: one pass over an open PR with a fake tracker, a fake claude and a repo with a bare origin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        self.write({"app.py": "x = 1\n", ".gitignore": ".agent/\n"})
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "switch", "-q", "-c", "work")
        self.write({"app.py": "x = 2\n"})
        git(self.repo, "push", "-q", "-u", "origin", "work")
        self.calls, self.actions, self.answers = [], {}, {}
        self.fail_resume, self.calls_raise = False, None
        self.tr = FakeTracker()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, files, commit=True):
        for p, text in files.items():
            (self.repo / p).write_text(text)
        if commit:
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", "c")

    def claude(self, text, *, cwd, step, model=None, cost_log=None, **kw):
        self.calls.append({"step": step, "model": model, "text": text, **kw})
        if step == self.calls_raise:
            raise LLMError(f"{step}: budget exceeded")
        if self.fail_resume and kw.get("resume"):
            raise LLMError(f"{step}: No conversation found with session ID: {kw['resume']}")
        with open(cost_log, "a") as f:
            f.write(json.dumps({"step": step, "model": model, "usd": 0.01}) + "\n")
        self.write(self.actions.get(step, {}), commit=False)
        if step in self.answers:
            return self.answers[step], None
        return {"summary": "s", "pr_sections": [], "lessons": [], "concerns": []}, "sid2"

    def cfg(self, **over):
        c = copy.deepcopy(DEFAULTS)
        c.update(_root=str(self.repo), gates=[])
        for section, vals in over.items():
            c[section] = {**c[section], **vals} if isinstance(vals, dict) else vals
        return c

    def start(self, **state):
        c = self.cfg()
        r = Run(c, 7)
        r.set(**{"key": "#7", "title": "t", "status": "pr_open", "pr": "https://github.com/o/r/pull/5",
                 "branch": "work", "ws": str(self.repo), "session": "sid1", **state})
        return c, r

    def address(self, c=None, **kw):
        c = c or self.cfg()
        lp = Loop(c, claude=self.claude, tracker=self.tr)
        lp.address(Run(c, 7), **kw)
        return Run(c, 7)

    def test_nothing_new_makes_no_llm_call(self):
        self.tr.comments = [c(1, body="old", kind="issue")]
        self.tr.checks = {"sha": "abc1234", "checks": [{"name": "test", "bucket": "pass"}], "failed_runs": []}
        self.start(handled_comments=[1])
        run = self.address()
        self.assertEqual(self.calls, [])
        self.assertEqual(run["status"], "pr_open")
        self.assertEqual(run["polls"][-1]["comments"], 0)
        self.assertEqual(run["polls"][-1]["ci"], "none")
        self.assertEqual(run["polls"][-1]["note"], "nothing new")


    def triage(self, *decisions):
        self.answers["triage"] = {"comments": [dict(zip(("id", "action", "reason", "reply_draft"), d)) for d in decisions]}

    def test_triage_is_one_lean_cheap_call_and_drafts_are_not_posted(self):
        self.tr.comments = [c(1, body="why x?"), c(2, body="thanks", kind="issue")]
        self.triage((1, "reply", "question", "Because y."), (2, "ignore", "thanks only", ""))
        self.start()
        run = self.address()
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual((call["step"], call["model"], call["lean"]), ("triage", "haiku", True))
        self.assertIn("@@ -1 +1 @@", call["text"])  # the diff hunk around the line
        self.assertIn("why x?", call["text"])
        self.assertTrue(call["system"])
        self.assertEqual(sorted(run["handled_comments"]), [1, 2])
        self.assertEqual([(t["id"], t["action"]) for t in run["triage"]], [(1, "reply"), (2, "ignore")])
        self.assertIn("Because y.", (run.dir / "replies.md").read_text())
        self.assertEqual(self.tr.posted, [])
        self.assertEqual(run["polls"][-1]["comments"], 2)

    def test_replies_are_posted_with_the_marker_when_enabled(self):
        self.tr.comments = [c(1, body="why x?"), c(3, body="and README?", kind="issue")]
        self.triage((1, "reply", "q", "Because y."), (3, "reply", "q", "Will do later."))
        self.start()
        self.address(self.cfg(pr={"post_replies": True}))
        self.assertEqual([(k, i) for k, i, _ in self.tr.posted], [("reply", 1), ("comment", None)])
        self.assertTrue(all(MARKER in b for _, _, b in self.tr.posted))
        self.assertIn("and README?", self.tr.posted[1][2])  # quotes what it answers

    def test_comment_left_out_by_triage_is_handled_as_ignore(self):
        self.tr.comments = [c(1), c(2)]
        self.triage((1, "ignore", "noise", ""))
        self.start()
        run = self.address()
        self.assertEqual(sorted(run["handled_comments"]), [1, 2])
        self.assertEqual(run["triage"][-1], {"id": 2, "action": "ignore", "reason": "no decision from triage",
                                             "reply_draft": "", "ts": run["triage"][-1]["ts"]})

    def test_handled_comments_survive_a_restart(self):
        self.tr.comments = [c(1, body="why x?")]
        self.triage((1, "reply", "q", "Because y."))
        self.start()
        self.address()
        self.address()  # a new Loop reads state.json again
        self.assertEqual([x["step"] for x in self.calls], ["triage"])


    def pushed(self):
        return git(self.repo, "log", "-1", "--format=%s", "origin/work")

    def test_fix_comment_resumes_session_commits_and_pushes(self):
        self.tr.comments = [c(1, body="use 3 instead of 2")]
        self.triage((1, "fix", "asks for a change", ""))
        self.actions["fix-comments:1"] = {"app.py": "x = 3\n"}
        self.start()
        run = self.address()
        self.assertEqual([x["step"] for x in self.calls], ["triage", "fix-comments:1"])
        fix = self.calls[1]
        self.assertEqual(fix["resume"], "sid1")
        self.assertIn("use 3 instead of 2", fix["text"])
        self.assertIn("a.py:3", fix["text"])
        self.assertEqual(self.pushed(), "#7: address review")
        self.assertEqual(git(self.repo, "show", "origin/work:app.py"), "x = 3")
        self.assertEqual(run["handled_comments"], [1])
        self.assertEqual((run["status"], run["comment_rounds"]), ("pr_open", 1))
        self.assertEqual({k: run["follow_ups"][-1][k] for k in ("kind", "step", "result", "ids")},
                         {"kind": "comments", "step": "fix-comments:1", "result": "pushed", "ids": [1]})

    def test_last_comment_round_escalates(self):
        self.tr.comments = [c(1)]
        self.triage((1, "fix", "change", ""))
        self.start(comment_rounds=1)
        self.address(self.cfg(models={"escalate": "opus"}))
        self.assertEqual(self.calls[1]["model"], "opus")
        self.assertEqual(self.calls[1]["step"], "fix-comments:2")

    def test_resume_failure_falls_back_to_a_fresh_session(self):
        self.tr.comments = [c(1, body="use 3")]
        self.triage((1, "fix", "change", ""))
        self.actions["fix-comments:1"] = {"app.py": "x = 3\n"}
        self.fail_resume = True
        self.start()
        run = self.address()
        fresh = self.calls[-1]
        self.assertEqual([x["step"] for x in self.calls], ["triage", "fix-comments:1", "fix-comments:1"])
        self.assertIsNone(fresh["resume"])
        self.assertIn("Make x two.", fresh["text"])  # the ticket body: a fresh session has no history
        self.assertEqual(run["follow_ups"][-1]["fresh_session"], True)
        self.assertEqual(self.pushed(), "#7: address review")

    def test_gates_failing_after_fix_push_nothing(self):
        self.tr.comments = [c(1)]
        self.triage((1, "fix", "change", ""))
        self.actions["fix-comments:1"] = {"app.py": "x = 3\n"}
        self.start()
        before = self.pushed()
        run = self.address(self.cfg(gates=[{"name": "never", "cmd": "false"}], loop={"max_fix_rounds": 1}))
        self.assertEqual([x["step"] for x in self.calls], ["triage", "fix-comments:1", "fix-gates:1"])
        self.assertEqual(self.pushed(), before)
        self.assertEqual(run["follow_ups"][-1]["result"], "gates failing")


    def failing_ci(self, sha="abc1234", bucket="fail"):
        self.tr.checks = {"sha": sha, "checks": [{"name": "test", "bucket": bucket, "link": ".../actions/runs/901/job/1"},
                                                 {"name": "lint", "bucket": "pass", "link": ""}],
                          "failed_runs": [901] if bucket == "fail" else []}
        self.tr.logs[901] = "\n".join(f"test\tsetup\tline {i}" for i in range(200)) + "\ntest\trun\tAssertionError: 2 != 3\n"

    def test_failed_ci_on_new_sha_is_fixed_once(self):
        self.failing_ci()
        self.actions["fix-ci:1"] = {"app.py": "x = 3\n"}
        self.start()
        run = self.address()
        self.assertEqual([x["step"] for x in self.calls], ["fix-ci:1"])
        text = self.calls[0]["text"]
        self.assertIn("CI fails", text)
        self.assertIn("AssertionError: 2 != 3", text)
        self.assertNotIn("line 5\n", text)  # compacted
        self.assertEqual(self.pushed(), "#7: fix CI")
        self.assertEqual((run["handled_ci"], run["ci_rounds"]), (["abc1234"], 1))
        self.assertEqual({k: run["follow_ups"][-1][k] for k in ("kind", "sha", "result")},
                         {"kind": "ci", "sha": "abc1234", "result": "pushed"})
        self.address()  # restart, same sha still red: no second fix
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(Run(self.cfg(), 7)["polls"][-1]["ci"], "handled")

    def test_pending_ci_waits(self):
        self.failing_ci(bucket="pending")
        self.start()
        run = self.address()
        self.assertEqual(self.calls, [])
        self.assertEqual(run["polls"][-1]["note"], "CI pending on abc1234")

    def test_ci_waits_for_the_new_head_after_a_comment_fix(self):
        self.failing_ci()
        self.tr.comments = [c(1)]
        self.triage((1, "fix", "change", ""))
        self.actions["fix-comments:1"] = {"app.py": "x = 3\n"}
        self.start()
        run = self.address()
        self.assertEqual([x["step"] for x in self.calls], ["triage", "fix-comments:1"])
        self.assertEqual(run["handled_ci"], None)


    def assert_needs_human(self, run, why):
        self.assertEqual(run["status"], "needs_human")
        self.assertIn(why, run["needs_human"]["reason"])
        self.assertEqual(self.tr.labels, [(7, None, "needs_human")])
        kind, _, body = self.tr.posted[-1]
        self.assertEqual(kind, "comment")
        self.assertIn("Needs a human", body)
        self.assertIn(why, body)
        self.assertIn(MARKER, body)

    def test_comment_round_limit_needs_a_human(self):
        self.tr.comments = [c(1, body="rename x")]
        self.triage((1, "fix", "change", ""))
        self.start(comment_rounds=2)
        run = self.address()
        self.assertEqual([x["step"] for x in self.calls], ["triage"])
        self.assert_needs_human(run, "comment fix rounds used: 2 of 2")
        self.assertIn("rename x", self.tr.posted[-1][2])  # what is left for the human

    def test_ci_round_limit_needs_a_human(self):
        self.failing_ci(sha="def5678")
        self.start(ci_rounds=2, handled_ci=["a", "b"])
        run = self.address()
        self.assertEqual(self.calls, [])
        self.assert_needs_human(run, "CI rounds used: 2 of 2")
        self.assertEqual(run["polls"][-1]["ci"], "limit")

    def test_gates_failing_after_a_fix_needs_a_human(self):
        self.failing_ci()
        self.start()
        run = self.address(self.cfg(gates=[{"name": "never", "cmd": "false"}], loop={"max_fix_rounds": 0}))
        self.assert_needs_human(run, "gates still failing after fix-ci:1")


    def other(self, n, **state):
        r = Run(self.cfg(), n)
        r.set(**{"key": f"#{n}", "title": "t", "pr": f"https://github.com/o/r/pull/{n}", "branch": "work",
                 "ws": str(self.repo), "session": "s", **state})

    def test_follow_up_polls_open_prs_only_and_survives_an_error(self):
        self.start()
        self.other(8, status="needs_human")
        self.other(9, status="pr_open")
        self.other(10, status="done")
        self.tr.broken = {"https://github.com/o/r/pull/5"}
        c = self.cfg()
        Loop(c, claude=self.claude, tracker=self.tr).follow_up()
        self.assertEqual(self.tr.seen, ["https://github.com/o/r/pull/5", "https://github.com/o/r/pull/9"])
        self.assertIn("HTTP 502", Run(c, 7)["polls"][-1]["error"])
        self.assertEqual(Run(c, 9)["polls"][-1]["note"], "nothing new")

    def test_a_fix_that_crashes_needs_a_human(self):
        self.failing_ci()
        self.start(session=None)
        self.fail_resume = True
        self.calls_raise = "fix-ci:1"
        Loop(self.cfg(), claude=self.claude, tracker=self.tr).follow_up()
        run = Run(self.cfg(), 7)
        self.assertEqual(run["status"], "needs_human")
        self.assertIn("follow-up failed: fix-ci:1: budget", run["needs_human"]["reason"])

    def test_address_now_overrides_needs_human_for_one_pass(self):
        self.failing_ci(sha="new9999")
        self.actions["fix-ci:1"] = {"app.py": "x = 3\n"}
        self.start(status="needs_human", ci_rounds=2, comment_rounds=2, handled_ci=["a", "b"])
        c = self.cfg()
        code = Loop(c, claude=self.claude, tracker=self.tr).address_now(7)
        run = Run(c, 7)
        self.assertEqual(code, 0)
        self.assertEqual(self.tr.labels[0], (7, "needs_human", "wip"))
        self.assertEqual([x["step"] for x in self.calls], ["fix-ci:1"])
        self.assertEqual((run["status"], run["ci_rounds"], run["overrides"][-1]["was"]), ("pr_open", 1, "needs_human"))

    def test_address_now_refuses_a_run_without_an_open_pr(self):
        self.start(status="implementing")
        self.assertEqual(Loop(self.cfg(), claude=self.claude, tracker=self.tr).address_now(7), 1)
        self.assertEqual(Loop(self.cfg(), claude=self.claude, tracker=self.tr).address_now(42), 1)
        self.assertEqual(self.calls, [])


    def test_ci_limit_waits_for_the_new_head_after_a_comment_fix(self):
        self.failing_ci(sha="def5678")
        self.tr.comments = [c(1)]
        self.triage((1, "fix", "change", ""))
        self.actions["fix-comments:1"] = {"app.py": "x = 3\n"}
        self.start(ci_rounds=2, handled_ci=["a", "b"])
        run = self.address()
        self.assertEqual(run["status"], "pr_open")
        self.assertEqual(self.tr.labels, [])

    def test_fix_without_changes_needs_a_human(self):
        self.tr.comments = [c(1, body="rename x")]
        self.triage((1, "fix", "change", ""))
        self.start()
        before = self.pushed()
        run = self.address()
        self.assertEqual(self.pushed(), before)
        self.assertEqual(run["follow_ups"][-1]["result"], "no changes")
        self.assert_needs_human(run, "fix-comments:1 changed nothing")
        self.assertEqual(run["handled_comments"], [])

    def test_interrupted_fix_is_picked_up_again(self):
        self.failing_ci(sha="new1111")
        self.start(status="fixing-review-ci")
        self.other(8, status="fixing-review-1")  # a review fix before the PR: not a follow-up
        Loop(self.cfg(), claude=self.claude, tracker=self.tr).follow_up()
        self.assertEqual(self.tr.seen, ["https://github.com/o/r/pull/5"])
        self.assertEqual([x["step"] for x in self.calls], ["fix-ci:1"])
        self.assertEqual(Loop(self.cfg(), claude=self.claude, tracker=self.tr).address_now(8), 1)

    def test_override_retries_ci_on_the_same_head(self):
        self.failing_ci(sha="abc1234")
        self.start(status="needs_human", ci_rounds=1, handled_ci=["abc1234"])
        Loop(self.cfg(), claude=self.claude, tracker=self.tr).address_now(7)
        self.assertEqual([x["step"] for x in self.calls], ["fix-ci:1"])


if __name__ == "__main__":
    unittest.main()
