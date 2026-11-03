import tempfile
import unittest
from pathlib import Path

from todo.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_add_and_load(self):
        self.store.add("buy milk")
        self.assertEqual(self.store.load(), [{"id": 1, "title": "buy milk", "done": False}])

    def test_add_twice_keeps_both(self):
        self.store.add("buy milk")
        self.store.add("buy milk")
        self.assertEqual(len(self.store.load()), 2)

    def test_done(self):
        self.store.add("a")
        self.store.done(1)
        self.assertTrue(self.store.load()[0]["done"])

    def test_done_missing(self):
        with self.assertRaises(KeyError):
            self.store.done(42)


if __name__ == "__main__":
    unittest.main()
