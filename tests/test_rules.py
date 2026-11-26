"""kb rules: path-scoped .knowledge/rules/*.md with frontmatter, matched by glob."""
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import kb  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402

API = '---\npaths: ["src/api/**", "*.sql"]\nsummary: validate input at the edge\n---\nCheck every field.\n'
WEB = '---\npaths: ["web/**"]\nsummary: no inline styles\n---\nUse classes.\n'
SRC = '---\npaths: ["src/**"]\nsummary: log with the module logger\n---\nUse log = getLogger(__name__).\n'
GLOBAL = "---\nsummary: keep functions short\n---\nUnder 40 lines.\n"


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {**copy.deepcopy(DEFAULTS), "_root": str(self.root)}

    def tearDown(self):
        self.tmp.cleanup()

    def git_init(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def rules(self, **files):
        d = self.root / ".knowledge" / "rules"
        d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (d / f"{name}.md").write_text(text)


class ParseRule(unittest.TestCase):
    def test_frontmatter_and_body(self):
        r = kb.parse_rule(API, name="api")
        self.assertEqual(r["paths"], ["src/api/**", "*.sql"])
        self.assertEqual(r["summary"], "validate input at the edge")
        self.assertEqual(r["text"], "Check every field.")
        self.assertEqual(r["name"], "api")

    def test_no_paths_applies_everywhere(self):
        self.assertEqual(kb.parse_rule(GLOBAL, name="g")["paths"], [])

    def test_no_frontmatter_is_a_global_rule_named_by_its_first_line(self):
        r = kb.parse_rule("Never print secrets.\nMore.", name="secrets")
        self.assertEqual((r["paths"], r["summary"], r["text"]), ([], "Never print secrets.", "Never print secrets.\nMore."))

    def test_yaml_block_list(self):
        r = kb.parse_rule("---\npaths:\n  - web/**\n  - '*.css'\nsummary: s\n---\nx", name="w")
        self.assertEqual((r["paths"], r["summary"]), (["web/**", "*.css"], "s"))

    def test_unquoted_flow_list(self):
        self.assertEqual(kb.parse_rule("---\npaths: [src/**, *.sql]\n---\nx", name="u")["paths"], ["src/**", "*.sql"])

    def test_single_path_without_brackets(self):
        self.assertEqual(kb.parse_rule("---\npaths: docs/**\n---\nx", name="d")["paths"], ["docs/**"])


class Matches(unittest.TestCase):
    def test_globs(self):
        cases = [("src/**", "src/a.py", True), ("src/**", "src/api/deep/x.py", True), ("src/**", "web/a.py", False),
                 ("*.sql", "db/m/1.sql", True), ("**/conftest.py", "conftest.py", True),
                 ("**/conftest.py", "tests/conftest.py", True), ("src/api/**", "src/apix/a.py", False),
                 ("todo/**/*.py", "todo/store.py", True), ("todo/**/*.py", "todo/sub/x.py", True),
                 ("todo/**/*.py", "todox/store.py", False), ("a/**/b/**/c.py", "a/b/c.py", True)]
        for glob, path, want in cases:
            self.assertEqual(kb.matches(path, [glob]), want, (glob, path))

    def test_no_globs_match_everything(self):
        self.assertTrue(kb.matches("any/file.py", []))


class RulesFor(RulesTest):
    def test_only_rules_whose_paths_match(self):
        self.rules(api=API, web=WEB, src=SRC, glob=GLOBAL)
        names = [r["name"] for r in kb.rules_for(self.cfg, self.root, ["src/a.py"])]
        self.assertEqual(names, ["glob", "src"])

    def test_no_rules_folder(self):
        self.assertEqual(kb.rules_for(self.cfg, self.root, ["src/a.py"]), [])

    def test_scoped_rules_need_a_matching_path(self):
        self.rules(src=SRC, glob=GLOBAL)
        self.assertEqual([r["name"] for r in kb.rules_for(self.cfg, self.root, [])], ["glob"])


class Digest(RulesTest):
    def test_one_line_per_rule(self):
        self.rules(api=API, glob=GLOBAL)
        self.assertEqual(kb.write_digest(self.cfg, self.root).read_text().splitlines()[-2:],
                         ["- validate input at the edge (src/api/**, *.sql)", "- keep functions short (everywhere)"])

    def test_update_writes_the_digest(self):
        self.git_init()
        self.rules(web=WEB)
        kb.update(self.cfg, self.root, use_llm=False)
        self.assertIn("- no inline styles (web/**)", (self.root / ".knowledge" / "rules-digest.md").read_text())


class InPrompts(RulesTest):
    """A ticket touching src/a.py gets the src/** rule, not the web/** one."""

    def setUp(self):
        super().setUp()
        self.git_init()
        for p in ("src/a.py", "web/b.py"):
            (self.root / p).parent.mkdir(exist_ok=True)
            (self.root / p).write_text('"""A module for the task list."""\n')
        self.rules(src=SRC, web=WEB, glob=GLOBAL)
        kb.update(self.cfg, self.root, use_llm=False)

    def test_context_pack_has_rules_for_the_picked_files(self):
        pack = kb.context(self.cfg, self.root, "Change `src/a.py`")
        self.assertIn("Use log = getLogger(__name__).", pack)
        self.assertIn("Under 40 lines.", pack)
        self.assertNotIn("Use classes.", pack)

    def test_review_rules_are_conventions_plus_rules_for_changed_files(self):
        (self.root / ".knowledge" / "conventions.md").write_text("# Conventions\n\n- tests first\n")
        text = kb.review_rules(self.cfg, self.root, ["src/a.py"])
        self.assertIn("- tests first", text)
        self.assertIn("Use log = getLogger(__name__).", text)
        self.assertNotIn("Use classes.", text)


if __name__ == "__main__":
    unittest.main()
