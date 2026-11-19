"""sandbox/bench.py compare: differences between two saved `agent-kit report --json` results."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("bench", ROOT / "sandbox" / "bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def row(title, ticket, usd, fix=0, review=1, tin=100, cache_r=0, out=10):
    return {"ticket": ticket, "title": title, "usd": usd, "calls": 2, "fix_rounds": fix, "review_rounds": review,
            "in": tin, "cache_r": cache_r, "out": out}


def rep(*rows):
    return {"rows": list(rows), "total": {k: round(sum(r.get(k, 0) for r in rows), 4) for k in bench.METRICS}}


class Compare(unittest.TestCase):
    def test_matches_by_title_not_number(self):
        before = rep(row("Add remove", 1, 1.5, fix=2, tin=400))
        after = rep(row("Add remove", 9, 1.0, fix=0, tin=300))
        [ticket, total] = bench.compare(before, after)
        self.assertEqual(ticket["title"], "Add remove")
        self.assertEqual(ticket["usd"], (1.5, 1.0, -0.5))
        self.assertEqual(ticket["fix_rounds"], (2, 0, -2))
        self.assertEqual(ticket["in"], (400, 300, -100))
        self.assertEqual(total["title"], "total")
        self.assertEqual(total["usd"], (1.5, 1.0, -0.5))

    def test_ticket_on_one_side_only(self):
        rows = bench.compare(rep(row("Old", 1, 1.0)), rep(row("New", 2, 2.0)))
        by = {r["title"]: r for r in rows}
        self.assertEqual(by["Old"]["usd"], (1.0, None, None))
        self.assertEqual(by["New"]["usd"], (None, 2.0, None))

    def test_format_shows_change(self):
        text = bench.format_compare(bench.compare(rep(row("A", 1, 2.0)), rep(row("A", 1, 1.5))))
        self.assertIn("2.00 -> 1.50 (-0.50)", text)


if __name__ == "__main__":
    unittest.main()
