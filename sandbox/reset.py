#!/usr/bin/env python3
"""Rebuild the agent-kit sandbox: a private GitHub repo plus a local clone, reset to a known state.

    python3 sandbox/reset.py --create --dir ~/Private   # first time: create the repo, remember the folder
    python3 sandbox/reset.py                            # reset, using the remembered folder

What it does (only to the sandbox repo):
  close open PRs and delete their branches, delete other remote branches, close open issues,
  force-push sandbox/seed as a fresh `main`, recreate labels and the tickets from issues.json,
  and rebuild the local clone at <dir>/<repo>. Worktrees go to <dir>/.agent-wt/<repo>, the
  agent-kit default; only that subfolder is deleted, never other projects' worktrees.

The folder is saved in sandbox/.local.json (not committed); pass --dir again to change it.

Refuses any repo name without "sandbox" in it, so it can't be pointed at a real project.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "seed"
ISSUES = HERE / "issues.json"
LOCAL = HERE / ".local.json"  # remembered settings for this machine, gitignored


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def gh(*args, check=True):
    return run(["gh", *args], check=check)


def render(issues, numbers):
    """Replace {#i} (1-based ticket index in issues.json) with the real issue number."""
    def sub(m):
        i = int(m.group(1))
        if i not in numbers:
            raise ValueError(f"{{#{i}}} refers to a ticket that is not created yet")
        return f"#{numbers[i]}"
    return re.sub(r"\{#(\d+)\}", sub, issues)


def check_safe_dir(path, slug):
    """Only delete a local dir that is empty or a clone of the sandbox repo."""
    if not path.exists():
        return
    if path.is_dir() and not any(path.iterdir()):
        return
    # The dir must be a repo root itself; otherwise git would answer for a parent repo.
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False).stdout.strip()
    url = run(["git", "remote", "get-url", "origin"], cwd=path, check=False).stdout.strip()
    if not top or Path(top).resolve() != path.resolve() or not url.rstrip("/").removesuffix(".git").endswith(slug):
        sys.exit(f"refusing to delete {path}: not a clone of {slug} (origin: {url or 'none'})")


def sandbox_dir(arg):
    """--dir if given (and remembered), else the remembered one."""
    saved = json.loads(LOCAL.read_text()) if LOCAL.exists() else {}
    if arg:
        d = str(Path(arg).expanduser().resolve())
        if d != saved.get("dir"):
            LOCAL.write_text(json.dumps({**saved, "dir": d}, indent=2) + "\n")
            print(f"remembered sandbox folder {d} in {LOCAL.name}")
        return Path(d)
    if saved.get("dir"):
        return Path(saved["dir"])
    sys.exit("say where to put the sandbox: --dir <folder> (e.g. --dir ~/Private); it is remembered")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="agent-kit-sandbox", help="GitHub repo name (must contain 'sandbox')")
    ap.add_argument("--dir", help="folder that holds the local clone (remembered after the first use)")
    ap.add_argument("--create", action="store_true", help="create the private GitHub repo if missing")
    a = ap.parse_args()

    if "sandbox" not in a.repo.lower():
        sys.exit("the repo name must contain 'sandbox' (guard against resetting a real project)")
    owner = gh("api", "user", "-q", ".login").stdout.strip()
    slug = f"{owner}/{a.repo}"
    root = sandbox_dir(a.dir)
    if not root.is_dir():
        sys.exit(f"{root} does not exist")
    local = root / a.repo
    wt = root / ".agent-wt" / a.repo

    exists = gh("repo", "view", slug, "--json", "name", check=False).returncode == 0
    if not exists:
        if not a.create:
            sys.exit(f"{slug} does not exist; rerun with --create to make it (private)")
        gh("repo", "create", slug, "--private", "--description", "Disposable test repo for agent-kit")
        print(f"created private repo {slug}")
    elif gh("repo", "view", slug, "--json", "visibility", "-q", ".visibility").stdout.strip() != "PRIVATE":
        sys.exit(f"{slug} is not private; refusing to use it as a sandbox")

    # Remote cleanup: PRs, branches, issues.
    prs = json.loads(gh("pr", "list", "-R", slug, "--state", "open", "--json", "number").stdout)
    for p in prs:
        gh("pr", "close", str(p["number"]), "-R", slug, "--delete-branch", check=False)
    issues = json.loads(gh("issue", "list", "-R", slug, "--state", "open", "--limit", "500", "--json", "number").stdout)
    for i in issues:
        gh("issue", "close", str(i["number"]), "-R", slug, "--reason", "not planned", check=False)
    print(f"closed {len(prs)} PRs, {len(issues)} issues")

    # Local rebuild from seed.
    check_safe_dir(local, slug)
    if wt.exists():
        shutil.rmtree(wt)
    if local.exists():
        shutil.rmtree(local)
    shutil.copytree(SEED, local, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    run(["git", "init", "-q", "-b", "main"], cwd=local)
    run(["git", "add", "-A"], cwd=local)
    run(["git", "commit", "-q", "-m", "Sandbox seed"], cwd=local)
    run(["git", "remote", "add", "origin", f"https://github.com/{slug}.git"], cwd=local)
    run(["git", "push", "-q", "--force", "-u", "origin", "main"], cwd=local)
    gh("repo", "edit", slug, "--default-branch", "main", check=False)
    run(["git", "remote", "set-head", "origin", "main"], cwd=local, check=False)
    heads = run(["git", "ls-remote", "--heads", "origin"], cwd=local).stdout.split()
    stale = [h.removeprefix("refs/heads/") for h in heads if h.startswith("refs/heads/") and h != "refs/heads/main"]
    if stale:
        run(["git", "push", "-q", "origin", "--delete", *stale], cwd=local, check=False)
    print(f"pushed seed to {slug}, deleted {len(stale)} stale branches; local clone: {local}")

    # Labels and tickets.
    labels = json.loads((SEED / ".agent" / "config.json").read_text())["tracker"]["labels"]
    for name in labels.values():
        gh("label", "create", name, "-R", slug, "--force", "--color", "ededed")
    numbers = {}
    for idx, t in enumerate(json.loads(ISSUES.read_text()), start=1):
        args = ["issue", "create", "-R", slug, "--title", t["title"], "--body", render(t["body"], numbers)]
        if t.get("ready"):
            args += ["--label", labels["ready"]]
        url = gh(*args).stdout.strip().splitlines()[-1]
        numbers[idx] = int(url.rsplit("/", 1)[-1])
        print(f"  #{numbers[idx]} {t['title']}{'' if t.get('ready') else '  (not ready)'}")

    print(f"\nnext:\n  cd {local}\n  agent-kit kb update\n  agent-kit status\n  agent-kit run --dry-run")


if __name__ == "__main__":
    main()
