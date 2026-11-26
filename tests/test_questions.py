"""Blocking questions (loop.questions): ask on the issue, wait, resume with the answer.
Run: python3 -m unittest tests.test_questions"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentkit import questions  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.followup import MARKER  # noqa: E402
from agentkit.llm import LLMError  # noqa: E402
from agentkit.loop import IMPL_Q_SCHEMA, IMPL_SCHEMA, Loop, Run  # noqa: E402

TRUSTED = DEFAULTS["pr"]["trusted_associations"]
Q = {"question": "Keep the old API?", "options": ["yes, deprecate", "no, remove"], "recommended": "a", "why": "callers"}
BODY = "Do x.\n\n## Acceptance criteria\n- x works\n"


def cm(body, association="OWNER", author="alice"):
    return {"author": author, "association": association, "body": body, "created_at": "2026-11-26T10:00:00Z"}


class Pure(unittest.TestCase):
    def test_comment_lists_options_recommendation_and_marker(self):
        text = questions.comment("#7", 7, 1, [Q])
        self.assertIn("### 1. Keep the old API?", text)
        self.assertIn("- **a)** yes, deprecate", text)
        self.assertIn("- **b)** no, remove", text)
        self.assertIn("Recommended: a (callers)", text)
        self.assertTrue(text.endswith(questions.marker(7, 1)))

    def test_answers_only_after_the_questions_by_trusted_humans(self):
        asked = cm(questions.comment("#7", 7, 1, [Q]), author="kit")
        comments = [cm("before"), asked, cm("1: b"), cm("spam", association="NONE"), cm("bot", author="ci[bot]"),
                    cm(f"kit reply {MARKER}"), cm("   "), cm("and drop the docs", association="MEMBER")]
        self.assertEqual(questions.answers(comments, 7, 1, TRUSTED, MARKER), ["1: b", "and drop the docs"])

    def test_no_answers_while_unanswered_or_for_another_round(self):
        asked = cm(questions.comment("#7", 7, 1, [Q]))
        self.assertEqual(questions.answers([asked], 7, 1, TRUSTED, MARKER), [])
        self.assertEqual(questions.answers([asked, cm("1: b")], 7, 2, TRUSTED, MARKER), [])

    def test_a_second_round_comment_is_not_an_answer_to_the_first(self):
        c1, c2 = cm(questions.comment("#7", 7, 1, [Q])), cm(questions.comment("#7", 7, 2, [Q]))
        self.assertEqual(questions.answers([c1, cm("1: a"), c2], 7, 1, TRUSTED, MARKER), ["1: a"])

    def test_qa(self):
        text = questions.qa([Q], ["1: b"])
        self.assertIn("1. Keep the old API? Options: a) yes, deprecate; b) no, remove", text)
        self.assertTrue(text.endswith("## The human's answer\n1: b"))


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class Tracker:
    def __init__(self):
        self.iss = {7: {"number": 7, "title": "Ticket 7", "body": BODY, "labels": ["ready-for-agent"], "state": "OPEN"}}
        self.labels, self.comments = [], []

    def issues(self, refresh=False):
        return self.iss

    def get(self, n):
        return self.iss[n]

    def key(self, i):
        return f"#{i['number']}"

    def blockers(self, i):
        return []

    def candidates(self):
        return [{**i, "key": f"#{n}", "blocked_by": [], "unmet": []}
                for n, i in self.iss.items() if "ready-for-agent" in i["labels"]]

    def set_status(self, n, old, new):
        self.labels.append((old, new))
        self.iss[n]["labels"] = [new]

    def issue_comment(self, n, body):
        self.comments.append(cm(body, author="kit"))

    def issue_comments(self, n):
        return self.comments


class Flow(unittest.TestCase):
    """do_ticket with loop.questions on: a real worktree, a fake claude; quality rounds and ship are stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(root / "origin.git"))
        (self.repo / ".gitignore").write_text(".agent/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "seed")
        git(self.repo, "push", "-q", "origin", "main")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(_root=str(self.repo), _configured=True, gates=[])
        self.cfg["workspace"]["dir"] = str(root / "wt")
        self.cfg["loop"]["questions"]["enabled"] = True
        self.tr, self.calls, self.shipped = Tracker(), [], []
        self.ask = {"implement"}  # steps that return questions
        self.resume_fails = False

    def claude(self, text, *, cwd, step, schema=None, resume=None, budget=None, cost_log=None, **kw):
        self.calls.append({"step": step, "text": text, "schema": schema, "resume": resume, "budget": budget})
        if resume and self.resume_fails:
            raise LLMError("No conversation found")
        res = {"summary": "s", "pr_sections": [], "lessons": [], "concerns": []}
        if "questions" in (schema or {}).get("properties", {}):
            res["questions"] = [Q] if step in self.ask else []
        if step not in self.ask:
            (Path(cwd) / "x.py").write_text("x = 1\n")
        return res, "sid1"

    def loop(self):
        lp = Loop(self.cfg, claude=self.claude, tracker=self.tr)
        lp._quality_rounds = lambda run, ws, issue: None
        lp._ship = lambda run, ws, issue, impl, failure: self.shipped.append(issue["number"]) or run.set(status="pr_open") or True
        return lp

    def issue(self):
        return {**self.tr.get(7), "key": "#7", "blocked_by": []}

    def test_questions_stop_the_ticket_and_are_posted(self):
        self.assertTrue(self.loop().do_ticket(self.issue()))
        st = Run(self.cfg, 7)
        self.assertEqual((st["status"], st["question_rounds"]), ("questions", 1))
        self.assertEqual(st["questions"][0]["items"], [Q])
        self.assertEqual(self.shipped, [])
        self.assertEqual(len(self.tr.comments), 1)
        self.assertIn(questions.marker(7, 1), self.tr.comments[0]["body"])
        self.assertEqual(self.tr.labels[-1], ("wip", "needs_human"))
        self.assertIs(self.calls[0]["schema"], IMPL_Q_SCHEMA)
        self.assertIn("return `questions`", self.calls[0]["text"])

    def test_unanswered_run_waits_without_llm(self):
        lp = self.loop()
        lp.do_ticket(self.issue())
        self.tr.comments.append(cm("I think so?", association="NONE"))
        self.assertTrue(lp.do_ticket(self.issue()))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(Run(self.cfg, 7)["status"], "questions")

    def test_answer_resumes_the_session_and_ships(self):
        lp = self.loop()
        lp.do_ticket(self.issue())
        self.tr.comments.append(cm("1: b"))
        lp.answered()
        call = self.calls[-1]
        self.assertEqual((call["step"], call["resume"], call["budget"]), ("answer:1", "sid1", 8))
        self.assertIn("## The human's answer\n1: b", call["text"])
        self.assertIn("you may ask again", call["text"])
        self.assertEqual(self.shipped, [7])
        st = Run(self.cfg, 7)
        self.assertEqual(st["questions"][0]["answers"], ["1: b"])
        self.assertEqual(self.tr.labels[-1], ("needs_human", "wip"))

    def test_failed_resume_starts_a_fresh_session_with_the_answers(self):
        lp = self.loop()
        lp.do_ticket(self.issue())
        self.tr.comments.append(cm("1: b"))
        self.resume_fails = True
        lp.answered()
        self.assertEqual([c["step"] for c in self.calls], ["implement", "answer:1", "answer-fresh:1"])
        self.assertIn("Implement the ticket", self.calls[-1]["text"])
        self.assertIn("1: b", self.calls[-1]["text"])
        self.assertEqual(self.shipped, [7])

    def test_max_rounds_then_it_must_decide(self):
        self.cfg["loop"]["questions"]["max_rounds"] = 1
        self.ask = {"implement", "answer:1"}  # would ask again
        lp = self.loop()
        lp.do_ticket(self.issue())
        self.tr.comments.append(cm("1: b"))
        lp.answered()
        self.assertIs(self.calls[-1]["schema"], IMPL_SCHEMA)
        self.assertIn("Do not ask again", self.calls[-1]["text"])
        self.assertEqual(len(self.tr.comments), 2)  # questions + answer, no second round

    def test_answered_respects_pr_slots(self):
        lp = self.loop()
        lp.do_ticket(self.issue())
        self.tr.comments.append(cm("1: b"))
        Run(self.cfg, 9).set(key="#9", status="pr_open", pr="u")
        lp.answered()
        self.assertEqual(self.shipped, [])

    def test_status_line(self):
        lp = self.loop()
        lp.do_ticket(self.issue())
        self.assertTrue(lp.status_lines()[-1].endswith("waiting for your answer on issue #7 (round 1)"))


class Off(unittest.TestCase):
    def test_off_keeps_prompt_and_schema(self):
        cfg = copy.deepcopy(DEFAULTS)
        self.assertFalse(cfg["loop"]["questions"]["enabled"])
        text = (ROOT / "prompts" / "implement.md").read_text()
        from agentkit.loop import prompt
        out = prompt("implement", questions="")
        self.assertEqual(out, prompt("implement").replace("$questions", ""))
        self.assertIn("say so in `concerns`.\n\n## Return", out)
        self.assertNotIn("questions", IMPL_SCHEMA["properties"])
        self.assertIn("$questions", text)


if __name__ == "__main__":
    unittest.main()
