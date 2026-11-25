"""Refine: the script check before implement, the proposal comment and --apply plans.
Run: python3 -m unittest tests.test_refine"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import refine  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.followup import MARKER  # noqa: E402
from agentkit.loop import Loop, Run, has_criteria  # noqa: E402
from agentkit.tracker import GitHub  # noqa: E402

GOOD = "## What\n\nAdd remove.\n\n## Acceptance criteria\n\n- removes a task\n- [ ] tests for a missing id\n\n## Blocked by\n\n- #1\n"


def rc(**over):
    return {**DEFAULTS["refine"], **over}


class Criteria(unittest.TestCase):
    def test_items_under_heading_until_next_heading(self):
        self.assertEqual(refine.criteria(GOOD), ["removes a task", "tests for a missing id"])

    def test_heading_is_case_insensitive_and_any_level(self):
        self.assertEqual(refine.criteria("### ACCEPTANCE CRITERIA:\n* one\n+ two\n1. three\n"), ["one", "two", "three"])

    def test_no_heading_is_none(self):
        self.assertIsNone(refine.criteria("## What\n- a\n"))

    def test_heading_without_items_is_empty(self):
        self.assertEqual(refine.criteria("## Acceptance criteria\n\nIt works.\n## Next\n- not this\n"), [])

    def test_configured_heading(self):
        self.assertEqual(refine.criteria("## Done when\n- ok\n", "done when"), ["ok"])

    def test_sub_headings_stay_in_the_section(self):
        body = "## Acceptance criteria\n- a\n### Edge cases\n- b\n## Blocked by\n- #1\n"
        self.assertEqual(refine.criteria(body), ["a", "b"])

    def test_code_fences_are_not_criteria(self):
        body = "## Acceptance criteria\n- a\n```\n- not one\n# not a heading\n```\n- b\n"
        self.assertEqual(refine.criteria(body), ["a", "b"])


class Check(unittest.TestCase):
    def test_passes(self):
        self.assertEqual(refine.check(GOOD, rc()), {"ok": True, "reasons": [], "criteria": 2})

    def test_no_section(self):
        self.assertEqual(refine.check("## What\n\nThe CLI feels clunky. Make it nicer.\n", rc()),
                         {"ok": False, "reasons": ["no 'acceptance criteria' section"], "criteria": 0})

    def test_section_without_items(self):
        self.assertEqual(refine.check("## Acceptance criteria\n\nworks\n", rc())["reasons"],
                         ["'acceptance criteria' section has no bullet or checkbox items"])

    def test_too_long(self):
        body = GOOD + "x" * 100
        self.assertEqual(refine.check(body, rc(max_chars=100))["reasons"],
                         [f"body is {len(body)} chars > refine.max_chars (100)"])

    def test_max_chars_zero_is_no_limit(self):
        self.assertTrue(refine.check(GOOD + "x" * 20000, rc(max_chars=0))["ok"])

    def test_too_many_criteria(self):
        body = "## Acceptance criteria\n" + "".join(f"- c{i}\n" for i in range(9))
        self.assertEqual(refine.check(body, rc()), {"ok": False, "criteria": 9,
                                                    "reasons": ["9 criteria > refine.max_criteria (8): too big"]})

    def test_empty_body(self):
        self.assertFalse(refine.check(None, rc())["ok"])


class HasCriteria(unittest.TestCase):
    """review_plan's check reuses the same parser and keeps its old meaning."""

    def test_heading_or_checklist(self):
        self.assertTrue(has_criteria("## Acceptance criteria\n\nit works"))
        self.assertTrue(has_criteria("Do it\n- [ ] works"))
        self.assertFalse(has_criteria("Do it\n- works"))

    def test_configured_heading(self):
        self.assertTrue(has_criteria("## Done when\n- ok", "done when"))
        self.assertFalse(has_criteria("## Done when\n- ok"))


PROPOSAL = {"criteria": ["`list` aligns columns", "`--help` lists every command"],
            "split": [{"title": "Align list output", "body": "Align the columns.", "criteria": ["columns align"], "blocked_by": []},
                      {"title": "Better help", "body": "Describe commands.", "criteria": ["help lists commands"],
                       "blocked_by": [1]}],
            "questions": ["Which commands feel clunky?"]}
CHECK = {"ok": False, "reasons": ["no 'acceptance criteria' section"], "criteria": 0}


class Comment(unittest.TestCase):
    def test_round_trip_with_body_hash(self):
        text = refine.render_comment(PROPOSAL, CHECK, "abc123", 158)
        self.assertIn(MARKER, text)
        self.assertIn("no 'acceptance criteria' section", text)
        self.assertIn("agent-kit refine --apply 158", text)
        self.assertIn("1. Align list output", text)
        self.assertIn("Which commands feel clunky?", text)
        self.assertEqual(refine.parse_comment(text), {**PROPOSAL, "body_hash": "abc123"})

    def test_not_a_proposal(self):
        self.assertIsNone(refine.parse_comment(f"## Needs a human\n\n{MARKER}"))

    def test_edited_block_with_broken_json(self):
        text = refine.render_comment(PROPOSAL, CHECK, "abc123", 158).replace('"criteria": [', '"criteria": [,')
        with self.assertRaisesRegex(ValueError, "JSON"):
            refine.parse_comment(text)

    def test_latest_kit_proposal_wins_and_others_are_ignored(self):
        old = refine.render_comment({**PROPOSAL, "questions": []}, CHECK, "old", 158)
        new = refine.render_comment(PROPOSAL, CHECK, "new", 158)
        forged = new.replace(MARKER, "")  # someone pasted a block without the kit's marker
        self.assertEqual(refine.latest([{"body": old}, {"body": new}, {"body": forged}, {"body": "lgtm"}])["body_hash"], "new")
        self.assertIsNone(refine.latest([{"body": "lgtm"}]))

    def test_body_hash_ignores_trailing_whitespace(self):
        self.assertEqual(refine.body_hash("a\n"), refine.body_hash("a"))
        self.assertNotEqual(refine.body_hash("a"), refine.body_hash("b"))


class Apply(unittest.TestCase):
    def test_criteria_go_before_blocked_by(self):
        body = "## What\n\nMake it nicer.\n\n## Blocked by\n\nNone\n"
        self.assertEqual(refine.with_criteria(body, ["a", "b"], "acceptance criteria", "blocked by"),
                         "## What\n\nMake it nicer.\n\n## Acceptance criteria\n\n- [ ] a\n- [ ] b\n\n## Blocked by\n\nNone\n")

    def test_criteria_appended_without_blocked_by(self):
        self.assertEqual(refine.with_criteria("Do it.", ["a"], "acceptance criteria", "blocked by"),
                         "Do it.\n\n## Acceptance criteria\n\n- [ ] a\n")

    def test_criteria_replace_an_empty_section(self):
        body = "## Acceptance criteria\n\nworks\n\n## Blocked by\n\n- #1\n"
        got = refine.with_criteria(body, ["a"], "acceptance criteria", "blocked by")
        self.assertEqual(refine.criteria(got), ["a"])
        self.assertEqual(got.count("Acceptance criteria"), 1)

    def test_split_errors(self):
        self.assertEqual(refine.split_errors(PROPOSAL["split"]), [])
        bad = [{"title": "", "body": "x", "criteria": [], "blocked_by": [2]}, {"title": "b", "body": "", "criteria": ["c"], "blocked_by": [2, 5]}]
        self.assertEqual(refine.split_errors(bad), ["split 1: no title", "split 1: blocked by 2, which comes later",
                                                    "split 1: no criteria", "split 2: blocked by itself",
                                                    "split 2: blocked by 5, which comes later"])
        self.assertEqual(refine.split_errors([{"title": "a", "criteria": ["c"], "blocked_by": [0]}]),
                         ["split 1: blocked by 0, not a split number (1-based)"])

    def test_split_body_blockers_parse_back(self):
        """Index refs become the earlier split issues' numbers; the parent's blockers are inherited."""
        body = refine.split_body(PROPOSAL["split"][1], [201], parent=158, parent_blockers=[90],
                                 criteria_heading="acceptance criteria", blocked_by_heading="blocked by")
        self.assertIn("Split from #158.", body)
        self.assertEqual(refine.criteria(body), ["help lists commands"])
        cfg = {"tracker": DEFAULTS["tracker"], "_root": "."}
        self.assertEqual(GitHub(cfg, run=None).blockers({"number": 202, "body": body}), [90, 201])

    def test_split_body_without_blockers(self):
        body = refine.split_body(PROPOSAL["split"][0], [], parent=158, parent_blockers=[],
                                 criteria_heading="acceptance criteria", blocked_by_heading="blocked by")
        self.assertIn("## Blocked by\n\nNone (can start immediately)\n", body)
        self.assertEqual(GitHub({"tracker": DEFAULTS["tracker"], "_root": "."}, run=None)
                         .blockers({"number": 1, "body": body}), [])


VAGUE = "## What\n\nThe CLI feels clunky. Make it nicer.\n\n## Blocked by\n\nNone\n"


class RefineTracker:
    """The tracker methods pick() and refine use; records what it would write to GitHub."""

    def __init__(self, issues):
        self.iss = {i["number"]: {"labels": ["ready-for-agent"], "state": "OPEN", **i} for i in issues}
        self.comments, self.writes = {}, []

    def get(self, n):
        return self.iss[n]

    def key(self, i):
        return f"#{i['number']}"

    def blockers(self, i):
        return []

    def candidates(self):
        return [{**i, "key": self.key(i), "blocked_by": [], "unmet": []} for _, i in sorted(self.iss.items())
                if i["state"] == "OPEN" and "ready-for-agent" in i["labels"]]

    def set_status(self, n, old, new):
        self.writes.append(("label", n, old, new))
        self.iss[n]["labels"] = [l for l in self.iss[n]["labels"] if l != "ready-for-agent"] + ["needs-refinement"]

    def issue_comments(self, n):
        return self.comments.get(n, [])

    def issue_comment(self, n, body):
        self.writes.append(("comment", n, body))
        self.comments.setdefault(n, []).append({"author": "me", "association": "OWNER", "body": body})


class FakeClaude:
    def __init__(self, res):
        self.res, self.calls = res, []

    def __call__(self, text, **kw):
        self.calls.append((text, kw))
        return self.res, None


class LoopRefine(unittest.TestCase):
    """Loop.refine / pick with a fake tracker and a fake claude; run state in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["_root"] = self.tmp.name
        self.tr = RefineTracker([{"number": 158, "title": "Make the CLI nicer", "body": VAGUE},
                                 {"number": 160, "title": "Add remove", "body": GOOD}])
        self.claude = FakeClaude(PROPOSAL)

    def loop(self, mode):
        self.cfg["loop"]["refine"] = mode
        return Loop(self.cfg, claude=self.claude, tracker=self.tr)

    def test_check_skips_without_llm_or_github_writes(self):
        got = self.loop("check").pick()
        self.assertEqual(got["number"], 160)
        self.assertEqual((self.claude.calls, self.tr.writes), ([], []))
        st = Run(self.cfg, 158)["refine"]
        self.assertEqual((st["ok"], st["mode"], st["reasons"], st["proposed"]),
                         (False, "check", ["no 'acceptance criteria' section"], False))
        self.assertEqual(Run(self.cfg, 158)["refine_checks"], 1)

    def test_passing_ticket_costs_nothing(self):
        self.tr.iss[158]["state"] = "CLOSED"
        self.assertEqual(self.loop("auto").pick()["number"], 160)
        self.assertEqual(self.claude.calls, [])
        self.assertFalse((Run(self.cfg, 160).dir / "cost.jsonl").exists())
        self.assertTrue(Run(self.cfg, 160)["refine"]["ok"])

    def test_auto_proposes_once_comments_and_relabels(self):
        self.assertEqual(self.loop("auto").pick()["number"], 160)
        self.assertEqual(len(self.claude.calls), 1)
        text, kw = self.claude.calls[0]
        self.assertEqual((kw["step"], kw["model"], kw["lean"], kw["tools"], kw["budget"]), ("refine", "sonnet", True, "", 0.5))
        self.assertIn("at most 8", kw["system"])
        self.assertEqual(kw["schema"], refine.SCHEMA)
        self.assertIn("The CLI feels clunky", text)
        self.assertIn("no 'acceptance criteria' section", text)
        self.assertEqual(kw["cost_log"], Run(self.cfg, 158).cost_log)
        kinds = [w[0] for w in self.tr.writes]
        self.assertEqual(kinds, ["comment", "label"])
        self.assertEqual(self.tr.writes[1], ("label", 158, "ready", "refine"))
        self.assertEqual(refine.parse_comment(self.tr.writes[0][2])["body_hash"], refine.body_hash(VAGUE))
        st = Run(self.cfg, 158)["refine"]
        self.assertEqual((st["proposed"], st["split"], st["criteria_proposed"], st["questions"]), (True, 2, 2, 1))

    def test_rerun_on_same_body_does_not_propose_again(self):
        """A human put the ready label back without editing the body: skipped, no second call or comment."""
        lp = self.loop("auto")
        lp.pick()
        self.tr.iss[158]["labels"] = ["ready-for-agent"]
        self.assertEqual(lp.pick()["number"], 160)
        self.assertEqual(len(self.claude.calls), 1)
        self.assertEqual(len([w for w in self.tr.writes if w[0] == "comment"]), 1)

    def test_comment_found_when_state_is_lost(self):
        self.tr.comments[158] = [{"association": "OWNER", "body": refine.render_comment(
            PROPOSAL, CHECK, refine.body_hash(VAGUE), 158)}]
        self.loop("auto").pick()
        self.assertEqual(self.claude.calls, [])
        self.assertTrue(Run(self.cfg, 158)["refine"]["proposed"])

    def test_untrusted_comment_is_not_a_proposal(self):
        self.tr.comments[158] = [{"association": "NONE", "body": refine.render_comment(
            PROPOSAL, CHECK, refine.body_hash(VAGUE), 158)}]
        self.loop("auto").pick()
        self.assertEqual(len(self.claude.calls), 1)

    def test_edited_body_gets_a_new_proposal(self):
        lp = self.loop("auto")
        lp.pick()
        self.tr.iss[158].update(labels=["ready-for-agent"], body=VAGUE + "\nMore words.")
        lp.pick()
        self.assertEqual(len(self.claude.calls), 2)

    def test_off_does_nothing(self):
        self.assertEqual(self.loop("off").pick()["number"], 158)
        self.assertFalse(Run(self.cfg, 158).file.exists())

    def test_dry_pick_never_calls_or_writes(self):
        self.assertEqual(self.loop("auto").pick(dry=True)["number"], 160)
        self.assertEqual((self.claude.calls, self.tr.writes), ([], []))
        self.assertFalse(Run(self.cfg, 158).file.exists())

    def test_explicit_ticket_refined_unless_resuming(self):
        lp = self.loop("check")
        self.assertIsNone(lp.pick(158))
        Run(self.cfg, 158).set(status="needs_human")
        self.assertEqual(lp.pick(158)["number"], 158)

    def test_refine_command_proposes_in_check_mode(self):
        """`agent-kit refine <n>` is the manual step: it proposes whatever loop.refine says."""
        self.assertFalse(self.loop("check").refine(self.tr.get(158), mode="auto"))
        self.assertEqual(len(self.claude.calls), 1)

    def test_status_lines(self):
        lp = self.loop("auto")
        self.tr.iss[160]["body"] = "x"  # both fail now
        self.tr.iss[160]["labels"] = []  # not a candidate: only its run line shows
        lp.refine(self.tr.get(160), mode="check")
        self.tr.iss[160]["labels"] = ["ready-for-agent"]
        self.assertEqual(lp.status_lines(), [
            "REFINE  #158 Make the CLI nicer: not ready: no 'acceptance criteria' section",
            "REFINE  #160 Add remove: not ready: no 'acceptance criteria' section"])
        lp.pick()  # proposes for 158 and 160
        self.assertEqual(lp.status_lines(), [
            "REFINE  #158 Make the CLI nicer: proposal posted (llm), labelled needs-refinement",
            "REFINE  #160 Add remove: proposal posted (llm), labelled needs-refinement"])

    def test_manual_refine_labels_an_unlabelled_ticket(self):
        self.tr.iss[158]["labels"] = []
        self.loop("check").refine(self.tr.get(158), mode="auto")
        self.assertEqual(self.tr.writes[-1], ("label", 158, "ready", "refine"))


class ApplyTracker(RefineTracker):
    def __init__(self, issues):
        super().__init__(issues)
        self.next, self.fail_at, self.crash_after, self.fail_close = 301, None, None, False

    def issues(self):
        return self.iss

    def blockers(self, i):
        return GitHub({"tracker": DEFAULTS["tracker"], "_root": "."}, run=None).blockers(i)

    def create_issue(self, title, body, labels=()):
        if self.fail_at == self.next:
            raise RuntimeError("command failed (1): gh issue create")
        n, self.next = self.next, self.next + 1
        self.iss[n] = {"number": n, "title": title, "body": body, "labels": list(labels), "state": "OPEN"}
        self.writes.append(("create", n, title, tuple(labels)))
        if self.crash_after == n:
            self.crash_after = None
            raise RuntimeError("killed after gh issue create")
        return n

    def edit_body(self, n, body):
        self.iss[n]["body"] = body
        self.writes.append(("edit", n))

    def remove_label(self, n, status):
        self.writes.append(("unlabel", n, status))

    def close(self, n, reason="completed"):
        if self.fail_close:
            raise RuntimeError("command failed (1): gh issue close")
        self.iss[n]["state"] = "CLOSED"
        self.writes.append(("close", n, reason))


class LoopApply(unittest.TestCase):
    """Loop.refine_apply reads the latest trusted kit proposal from the issue's comments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["_root"] = self.tmp.name
        body = VAGUE.replace("None\n", "- #90\n")
        self.tr = ApplyTracker([{"number": 158, "title": "Make the CLI nicer", "body": body, "labels": ["needs-refinement"]}])
        self.lp = Loop(self.cfg, claude=FakeClaude(None), tracker=self.tr)

    def propose(self, prop, assoc="OWNER"):
        self.tr.comments[158] = [{"association": assoc, "body": refine.render_comment(
            prop, CHECK, refine.body_hash(self.tr.iss[158]["body"]), 158)}]

    def test_split_creates_in_order_and_closes_parent(self):
        self.propose(PROPOSAL)
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual(self.tr.writes[:2], [("create", 301, "Align list output", ("needs-refinement",)),
                                              ("create", 302, "Better help", ("needs-refinement",))])
        self.assertEqual(self.tr.blockers(self.tr.iss[302]), [90, 301])  # parsed by pick()'s blockers()
        self.assertEqual(self.tr.blockers(self.tr.iss[301]), [90])
        comment = self.tr.writes[2]
        self.assertEqual(comment[:2], ("comment", 158))
        self.assertIn("Split into #301, #302.", comment[2])
        self.assertIn(MARKER, comment[2])
        self.assertEqual(self.tr.writes[3:], [("unlabel", 158, "refine"), ("close", 158, "not planned")])
        st = Run(self.cfg, 158)["refine_apply"]
        self.assertEqual((st["kind"], st["created"], st["done"]), ("split", [301, 302], True))

    def test_crash_right_after_create_reuses_the_issue(self):
        """Created on GitHub but not yet in state.json: the rerun finds it by title and 'Split from' line."""
        self.propose(PROPOSAL)
        self.tr.crash_after = 301
        with self.assertRaises(RuntimeError):
            self.lp.refine_apply(158)
        self.assertEqual(Run(self.cfg, 158)["refine_apply"], None)
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual([w[1] for w in self.tr.writes if w[0] == "create"], [301, 302])
        self.assertEqual(Run(self.cfg, 158)["refine_apply"]["created"], [301, 302])

    def test_crash_after_the_split_comment_does_not_post_it_again(self):
        self.propose(PROPOSAL)
        self.tr.fail_close = True
        with self.assertRaises(RuntimeError):
            self.lp.refine_apply(158)
        self.tr.fail_close = False
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual(len([w for w in self.tr.writes if w[0] == "comment"]), 1)
        self.assertEqual(self.tr.iss[158]["state"], "CLOSED")

    def test_split_rerun_after_a_crash_continues(self):
        self.propose(PROPOSAL)
        self.tr.fail_at = 302
        with self.assertRaises(RuntimeError):
            self.lp.refine_apply(158)
        self.assertEqual(Run(self.cfg, 158)["refine_apply"]["created"], [301])
        self.tr.fail_at = None
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual([w[1] for w in self.tr.writes if w[0] == "create"], [301, 302])
        self.assertEqual(self.tr.blockers(self.tr.iss[302]), [90, 301])

    def test_applied_twice_does_nothing(self):
        self.propose(PROPOSAL)
        self.lp.refine_apply(158)
        n = len(self.tr.writes)
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual(len(self.tr.writes), n)

    def test_newer_proposal_is_applied(self):
        self.propose({**PROPOSAL, "split": []})
        self.lp.refine_apply(158)
        self.propose({**PROPOSAL, "split": [], "criteria": ["only this"]})  # body changed, new proposal
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual(refine.criteria(self.tr.iss[158]["body"]), ["only this"])

    def test_criteria_written_into_body_label_kept(self):
        self.propose({**PROPOSAL, "split": []})
        self.assertEqual(self.lp.refine_apply(158), 0)
        self.assertEqual(self.tr.writes, [("edit", 158)])
        self.assertEqual(refine.criteria(self.tr.iss[158]["body"]), PROPOSAL["criteria"])
        self.assertEqual(Run(self.cfg, 158)["refine_apply"]["kind"], "criteria")

    def test_invalid_split_creates_nothing(self):
        self.propose({**PROPOSAL, "split": [{**PROPOSAL["split"][0], "blocked_by": [2]}, PROPOSAL["split"][1]]})
        self.assertEqual(self.lp.refine_apply(158), 1)
        self.assertEqual(self.tr.writes, [])

    def test_no_or_untrusted_proposal(self):
        self.assertEqual(self.lp.refine_apply(158), 1)
        self.propose(PROPOSAL, assoc="NONE")
        self.assertEqual(self.lp.refine_apply(158), 1)
        self.assertEqual(self.tr.writes, [])

    def test_broken_edit_is_an_error(self):
        self.propose(PROPOSAL)
        self.tr.comments[158][0]["body"] = self.tr.comments[158][0]["body"].replace('"split": [', '"split": [,')
        self.assertEqual(self.lp.refine_apply(158), 1)

    def test_nothing_to_apply(self):
        self.propose({"criteria": [], "split": [], "questions": ["what?"]})
        self.assertEqual(self.lp.refine_apply(158), 1)
        self.assertEqual(self.tr.writes, [])


if __name__ == "__main__":
    unittest.main()
