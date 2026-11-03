"""Command line: python3 -m todo.cli [--file PATH] add|list|done ..."""
import argparse

from .store import Store


def main(argv=None):
    p = argparse.ArgumentParser(prog="todo")
    p.add_argument("--file", default="todo.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("title")
    sub.add_parser("list")
    d = sub.add_parser("done")
    d.add_argument("id", type=int)
    args = p.parse_args(argv)

    store = Store(args.file)
    if args.cmd == "add":
        t = store.add(args.title)
        print(f"added {t['id']}: {t['title']}")
    elif args.cmd == "list":
        for t in store.load():
            mark = "x" if t["done"] else " "
            print(f"[{mark}] {t['id']}: {t['title']}")
    elif args.cmd == "done":
        t = store.done(args.id)
        print(f"done {t['id']}: {t['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
