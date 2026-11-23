"""GitHub tracker PR methods (comments, checks, logs, replies) against a fake gh runner.
Run: python3 -m unittest tests.test_tracker_pr"""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.tracker import GitHub, pr_number  # noqa: E402

INLINE = [{"id": 11, "user": {"login": "alice"}, "author_association": "OWNER", "path": "todo/store.py", "line": 7, "original_line": 6,
           "body": "rename x", "created_at": "2026-11-23T10:00:00Z", "diff_hunk": "@@ -1,3 +1,4 @@\n+x = 1",
           "in_reply_to_id": None},
          {"id": 12, "user": {"login": "bob"}, "path": "todo/cli.py", "line": None, "original_line": 3,
           "body": "outdated", "created_at": "2026-11-23T10:01:00Z", "diff_hunk": "@@", "in_reply_to_id": None}]
REVIEWS = [{"id": 21, "user": {"login": "alice"}, "body": "overall fine", "state": "COMMENTED",
            "submitted_at": "2026-11-23T10:02:00Z"}]
ISSUE = [{"id": 31, "user": {"login": "carol"}, "body": "also update README", "created_at": "2026-11-23T10:03:00Z"}]
def threads(*nodes):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": list(nodes)}}}}}


THREADS = [threads({"isResolved": False, "comments": {"nodes": [{"databaseId": 11}]}}),  # two pages
           threads({"isResolved": True, "comments": {"nodes": [{"databaseId": 12}]}})]


class FakeGh:
    """Answers gh commands from a table keyed by a substring of the joined command."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, cwd=None, check=True, **kw):
        self.calls.append(cmd)
        line = " ".join(cmd)
        for key, (code, stdout) in self.answers.items():
            if key in line:
                if check and code:
                    raise RuntimeError(f"command failed ({code})")
                return SimpleNamespace(returncode=code, stdout=stdout, stderr="")
        raise AssertionError(f"unexpected command: {line}")


def gh(answers):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_root"] = "."
    fake = FakeGh(answers)
    return GitHub(cfg, run=fake), fake


class Comments(unittest.TestCase):
    def test_merges_three_sources_with_resolution(self):
        t, _ = gh({"pulls/5/comments": (0, json.dumps([INLINE])), "pulls/5/reviews": (0, json.dumps([REVIEWS])),
                   "issues/5/comments": (0, json.dumps([ISSUE])), "graphql": (0, json.dumps(THREADS))})
        got = {c["id"]: c for c in t.pr_review_comments(5)}
        self.assertEqual(got[11], {"id": 11, "kind": "inline", "author": "alice", "path": "todo/store.py", "line": 7,
                                   "body": "rename x", "created_at": "2026-11-23T10:00:00Z", "resolved": False,
                                   "diff_hunk": "@@ -1,3 +1,4 @@\n+x = 1", "in_reply_to": None,
                                   "association": "OWNER"})
        self.assertEqual((got[12]["line"], got[12]["resolved"]), (3, True))  # outdated: original_line
        self.assertEqual((got[21]["kind"], got[21]["path"], got[21]["created_at"]), ("review", None, "2026-11-23T10:02:00Z"))
        self.assertEqual((got[31]["kind"], got[31]["author"], got[31]["resolved"]), ("issue", "carol", False))
        self.assertIsNone(got[31]["association"])  # missing: never trusted


class Checks(unittest.TestCase):
    def test_checks_with_head_sha_and_failed_run_ids(self):
        checks = [{"name": "test", "bucket": "fail", "state": "FAILURE", "link": "https://github.com/o/r/actions/runs/901/job/5"},
                  {"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": "https://github.com/o/r/actions/runs/902/job/6"},
                  {"name": "ext", "bucket": "fail", "state": "FAILURE", "link": "https://ci.example/x"}]
        t, _ = gh({"pr view 5": (0, json.dumps({"headRefOid": "abc123"})), "pr checks 5": (1, json.dumps(checks))})
        res = t.pr_checks(5)
        self.assertEqual(res["sha"], "abc123")
        self.assertEqual([c["name"] for c in res["checks"]], ["test", "lint", "ext"])
        self.assertEqual(res["failed_runs"], [901])

    def test_no_checks_reported(self):
        t, _ = gh({"pr view 5": (0, json.dumps({"headRefOid": "abc123"})), "pr checks 5": (1, "")})
        self.assertEqual(t.pr_checks(5), {"sha": "abc123", "checks": [], "failed_runs": []})

    def test_failed_log(self):
        t, fake = gh({"run view 901": (0, "test\tstep\tFAIL: x\n")})
        self.assertEqual(t.failed_log(901), "test\tstep\tFAIL: x\n")
        self.assertIn("--log-failed", fake.calls[0])


class Posting(unittest.TestCase):
    def test_reply_to_inline_comment(self):
        t, fake = gh({"pulls/5/comments/11/replies": (0, "{}")})
        t.reply(5, 11, "done")
        self.assertIn("body=done", fake.calls[0])
        self.assertIn("POST", fake.calls[0])

    def test_comment_on_pr(self):
        t, fake = gh({"pr comment 5": (0, "")})
        t.comment(5, "hello")
        self.assertEqual(fake.calls[0][-2:], ["--body", "hello"])


class PrNumber(unittest.TestCase):
    def test_from_url_or_number(self):
        self.assertEqual(pr_number("https://github.com/o/r/pull/42"), 42)
        self.assertEqual(pr_number(7), 7)


if __name__ == "__main__":
    unittest.main()
