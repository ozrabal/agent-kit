"""Unit tests for the script-side logic. Run: python3 -m unittest discover -s tests"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentkit import kb  # noqa: E402
from agentkit.config import DEFAULTS, _merge  # noqa: E402
from agentkit.loop import _pathspec_excludes  # noqa: E402
from agentkit.tracker import GitHub  # noqa: E402
from agentkit.util import compact  # noqa: E402


def tracker():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_root"] = "."
    return GitHub(cfg)


class Blockers(unittest.TestCase):
    def test_parses_section_only(self):
        body = "## What\nsee #9\n\n## Blocked by\n\n- #113\n- https://github.com/o/r/issues/7\n\n## Notes\n#5"
        self.assertEqual(tracker().blockers({"number": 1, "body": body}), [7, 113])

    def test_none(self):
        body = "## Blocked by\n\nNone (can start immediately)\n"
        self.assertEqual(tracker().blockers({"number": 1, "body": body}), [])

    def test_no_section(self):
        self.assertEqual(tracker().blockers({"number": 1, "body": "depends on #4"}), [])

    def test_key(self):
        t = tracker()
        self.assertEqual(t.key({"number": 113, "title": "E6b-01: Export"}), "E6b-01")
        self.assertEqual(t.key({"number": 5, "title": "Fix login"}), "#5")


class Helpers(unittest.TestCase):
    def test_compact_keeps_errors_and_tail(self):
        text = "\n".join([f"ok {i}" for i in range(200)] + ["error: boom"] + [f"tail {i}" for i in range(30)])
        c = compact(text)
        self.assertIn("error: boom", c)
        self.assertIn("tail 29", c)
        self.assertNotIn("ok 5\n", c)

    def test_pathspec(self):
        self.assertEqual(_pathspec_excludes(["*.lock", ".knowledge/*"]),
                         [":(exclude,glob)**/*.lock", ":(exclude,glob).knowledge/**"])

    def test_merge_is_deep(self):
        base = {"a": {"b": 1, "c": 2}}
        self.assertEqual(_merge(base, {"a": {"c": 3}}), {"a": {"b": 1, "c": 3}})


class KnowledgeBase(unittest.TestCase):
    def test_doc_summary_code_and_md(self):
        self.assertEqual(kb.doc_summary("//! Rendering a document to PDF.\nuse x;", "rs"), "Rendering a document to PDF.")
        self.assertEqual(kb.doc_summary('"""Parse config files. More."""\n', "py"), "Parse config files.")
        self.assertIsNone(kb.doc_summary("// eslint-disable-next-line\nconst a = 1", "js"))
        md = "# Title\n\n**Status**: draft\n\nThis document explains the whole export pipeline in detail.\n"
        self.assertTrue(kb.doc_summary(md, "md").startswith("Title: This document explains"))

    def test_symbols(self):
        src = "export function foo() {}\nexport const Bar = 1\nfunction baz() {}\n"
        self.assertEqual(kb.symbols(src, "js"), ["foo", "Bar", "baz"])
        self.assertEqual(kb.symbols("pub fn run() {}\npub(crate) struct S;\n", "rs"), ["run", "S"])

    def test_js_imports_with_alias(self):
        files = {"src/a.ts", "src/lib/b.ts", "src/c/index.tsx"}
        als = [("@/", "src/")]
        text = "import x from './lib/b'\nimport y from '@/c'\nimport z from 'react'\n"
        self.assertEqual(kb.imports(text, "js", "src/a.ts", files, als, "."), ["src/c/index.tsx", "src/lib/b.ts"])

    def test_rust_mods(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Cargo.toml").write_text("[package]\n")
            files = {"src/lib.rs", "src/db.rs", "src/db/schema.rs"}
            got = kb.imports("mod db;\nuse crate::db::schema::X;\n", "rs", "src/lib.rs", files, [], d)
            self.assertEqual(got, ["src/db.rs", "src/db/schema.rs"])

    def test_terms_prefer_backticks_and_split_camel(self):
        t = kb.terms("Add `exportPdf` to the detail view and export it")
        self.assertEqual(t[0], "exportpdf")
        self.assertIn("pdf", t)
        self.assertNotIn("the", t)


if __name__ == "__main__":
    unittest.main()
