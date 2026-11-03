"""Task storage: load and save tasks as a JSON list in one file."""
import json
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def save(self, tasks):
        self.path.write_text(json.dumps(tasks, indent=2))

    def add(self, title):
        tasks = self.load()
        task = {"id": max((t["id"] for t in tasks), default=0) + 1, "title": title, "done": False}
        tasks.append(task)
        self.save(tasks)
        return task

    def done(self, task_id):
        tasks = self.load()
        for t in tasks:
            if t["id"] == task_id:
                t["done"] = True
                self.save(tasks)
                return t
        raise KeyError(task_id)
