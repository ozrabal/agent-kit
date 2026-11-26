"""review_plan decides whether a review runs and with which model. Run: python3 -m unittest tests.test_review_plan"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.loop import Loop, has_criteria, prompt, review_plan, review_system  # noqa: E402

AC = "Do it.\n\n## Acceptance criteria\n- works\n"


def cfg(**review):
    return {"models": {**DEFAULTS["models"], "review": "opus", "review_light": "sonnet"}, "refine": DEFAULTS["refine"],
            "review": {**DEFAULTS["review"], **review}}


class ReviewPlan(unittest.TestCase):
    def test_docs_only_skips(self):
        self.assertEqual(review_plan(cfg(), ["README.md", "docs/a/b.txt"], 40, AC),
                         {"run": False, "model": None, "reason": "docs only", "tier": "skip"})

    def test_docs_only_skips_even_with_criteria(self):
        self.assertFalse(review_plan(cfg(), ["docs/guide.md"], 500, AC)["run"])

    def test_small_change_skips_when_opted_in(self):
        p = review_plan(cfg(skip_under_lines=10), ["src/a.py"], 10, "Fix typo in a message.")
        self.assertEqual(p, {"run": False, "model": None, "reason": "10 changed lines <= review.skip_under_lines (10)", "tier": "skip"})

    def test_size_skip_off_by_default(self):
        self.assertTrue(review_plan(cfg(), ["src/a.py"], 1, "x")["run"])

    def test_criteria_block_size_skip_for_code(self):
        p = review_plan(cfg(skip_under_lines=10), ["src/a.py"], 3, AC)
        self.assertEqual(p, {"run": True, "model": "sonnet", "reason": "3 changed lines, ordinary paths", "tier": "light"})

    def test_risky_path_uses_full_model(self):
        p = review_plan(cfg(risky_paths=["**/auth/**"]), ["auth/login.py", "README.md"], 5, AC)
        self.assertEqual(p, {"run": True, "model": "opus", "reason": "risky path auth/login.py", "tier": "full"})

    def test_risky_beats_size_skip(self):
        p = review_plan(cfg(skip_under_lines=10, risky_paths=["db/migrations/*"]), ["db/migrations/1.sql"], 2, "x")
        self.assertEqual(p["model"], "opus")

    def test_large_change_uses_full_model(self):
        p = review_plan(cfg(light_max_lines=300), ["src/a.py"], 301, AC)
        self.assertEqual(p, {"run": True, "model": "opus", "reason": "301 changed lines > review.light_max_lines (300)", "tier": "full"})

    def test_at_light_limit_uses_light_model(self):
        self.assertEqual(review_plan(cfg(light_max_lines=300), ["src/a.py"], 300, AC)["model"], "sonnet")

    def test_zero_lines_not_skipped_by_default(self):
        # binary-only, rename or mode change: 0 counted lines must not mean "small enough to skip"
        self.assertTrue(review_plan(cfg(), ["img/logo.png"], 0, "x")["run"])

    def test_after_a_review_a_later_round_never_skips(self):
        p = review_plan(cfg(), ["src/a.md"], 2, "x", reviewed="light")
        self.assertEqual(p, {"run": True, "model": "sonnet", "tier": "light",
                             "reason": "docs only; kept light: an earlier round was reviewed"})

    def test_after_a_full_review_a_later_round_stays_full(self):
        p = review_plan(cfg(), ["src/a.py"], 5, AC, reviewed="full")
        self.assertEqual((p["model"], p["tier"]), ("opus", "full"))

    def test_earlier_review_does_not_lower_the_tier(self):
        p = review_plan(cfg(light_max_lines=10), ["src/a.py"], 50, AC, reviewed="light")
        self.assertEqual(p["tier"], "full")
        self.assertNotIn("kept", p["reason"])

    def test_no_files_runs_full(self):
        # nothing to classify (e.g. everything excluded): do not skip silently
        self.assertEqual(review_plan(cfg(), [], 0, AC)["model"], "opus")


class HasCriteria(unittest.TestCase):
    def test_heading(self):
        self.assertTrue(has_criteria("## Acceptance Criteria\n- a"))
        self.assertTrue(has_criteria("### acceptance criteria:\n- a"))

    def test_checklist(self):
        self.assertTrue(has_criteria("Do it.\n- [ ] works\n"))
        self.assertTrue(has_criteria("* [x] done"))

    def test_none(self):
        self.assertFalse(has_criteria("Fix the acceptance criteria wording in README"))
        self.assertFalse(has_criteria(""))
        self.assertFalse(has_criteria(None))


def pr_body(reviews):
    lp = Loop.__new__(Loop)  # _pr_body needs only cfg
    lp.cfg = copy.deepcopy(DEFAULTS)
    issue = {"number": 7, "key": "#7", "title": "t"}
    return lp._pr_body(issue, {"summary": "s"}, {"gates": None, "reviews": reviews, "tdd": None}, None)


class PrBody(unittest.TestCase):
    def test_skipped_review_says_why(self):
        body = pr_body([{"skipped": "docs only", "pass": True, "plan": {"run": False, "model": None, "reason": "docs only"}}])
        self.assertIn("## Automated review\n\nAutomated review skipped: docs only", body)

    def test_review_names_model_and_reason(self):
        rv = {"pass": True, "criteria": [{"criterion": "works", "met": True, "evidence": "t"}], "findings": [],
              "lessons": [], "plan": {"run": True, "model": "sonnet", "reason": "12 changed lines, ordinary paths"}}
        body = pr_body([rv])
        self.assertIn("Passed after 1 round(s), reviewed by sonnet (12 changed lines, ordinary paths).", body)
        self.assertIn("- [x] works", body)


class CacheFriendlyPrompts(unittest.TestCase):
    """Stable text first (or in the system prompt), per-ticket text last, so a prefix can be cached."""

    def test_review_instructions_live_in_the_system_prompt(self):
        c = copy.deepcopy(DEFAULTS)
        c["gates"] = ["npm test", {"name": "lint", "cmd": "npm run lint"}]
        sys_text = review_system(c)
        self.assertIn("strict, fair senior code reviewer", sys_text)
        self.assertIn("`blocker`: wrong behaviour", sys_text)
        self.assertIn("All gates passed: test, lint.", sys_text)
        self.assertNotIn("$", sys_text)

    def test_review_prompt_is_variable_part_only_in_order(self):
        text = prompt("review", key="#7", title="T", body="BODY", conventions="CONV", base="origin/main",
                      stat="STAT", diff="DIFF")
        self.assertNotIn("## What to do", text)
        idx = [text.index(x) for x in ("CONV", "BODY", "STAT", "DIFF")]
        self.assertEqual(idx, sorted(idx))
        self.assertNotIn("$", text)

    def test_implement_prompt_puts_instructions_before_ticket_and_context(self):
        text = prompt("implement", key="#7", number=7, title="T", body="BODY", context="CTX",
                      gate_names="test", pr_sections="Summary", questions="")
        idx = [text.index(x) for x in ("## How to work", "## Return", "BODY", "CTX")]
        self.assertEqual(idx, sorted(idx))
        self.assertNotIn("$", text)


if __name__ == "__main__":
    unittest.main()
