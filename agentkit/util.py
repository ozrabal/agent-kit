"""Small shared helpers: shell, git paths, logging, JSON files, output compaction."""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent


def log(msg):
    print(f"[agent-kit] {msg}", file=sys.stderr, flush=True)


def die(msg, code=1):
    log(msg)
    sys.exit(code)


def sh(cmd, cwd=None, check=True, env=None, input=None, timeout=None):
    """Run a command (list or shell string), capture text output."""
    r = subprocess.run(
        cmd, cwd=cwd, shell=isinstance(cmd, str), text=True,
        capture_output=True, env=env, input=input, timeout=timeout,
    )
    if check and r.returncode != 0:
        shown = cmd if isinstance(cmd, str) else shlex.join(cmd)
        raise RuntimeError(f"command failed ({r.returncode}): {shown}\n{(r.stderr or r.stdout or '')[-2000:]}")
    return r


def out(cmd, **kw):
    return sh(cmd, **kw).stdout.strip()


def repo_root(cwd=None):
    return Path(out(["git", "rev-parse", "--show-toplevel"], cwd=cwd))


def main_root(cwd=None):
    """The main checkout, also when called from inside a linked worktree."""
    common = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd))
    return common.parent


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path):
    """Rows of a JSON-lines file; [] when it does not exist. Broken lines are skipped."""
    path, rows = Path(path), []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "task"


_ERR = re.compile(
    r"error|fail|panicked|assert|expected|received|warning:|exception|traceback|✗|×|not ok|\bE\d{3,4}\b",
    re.I,
)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def compact(text, max_lines=60, tail=15):
    """Keep only the lines that explain a failure, plus the tail. Saves tokens."""
    lines = [_ANSI.sub("", l).rstrip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    keep = set()
    for i, l in enumerate(lines):
        if _ERR.search(l):
            keep.update({i - 1, i, i + 1, i + 2})
    keep.update(range(max(0, len(lines) - tail), len(lines)))
    picked, last = [], -2
    for i in sorted(k for k in keep if 0 <= k < len(lines)):
        if i != last + 1:
            picked.append("  ...")
        picked.append(lines[i][:300])
        last = i
    if len(picked) > max_lines:
        picked = picked[: max_lines - tail] + ["  ..."] + picked[-tail:]
    return "\n".join(picked)


def error_lines(text, n):
    """The first n lines that explain a failure (compact's error pattern), else the last n lines."""
    lines = [_ANSI.sub("", l).strip()[:300] for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    return ([l for l in lines if _ERR.search(l)][:n] or lines[-n:]) if lines else []


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def one_line(text):
    """Whitespace runs (newlines too) collapsed to one space, trimmed."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clip(text, n):
    text = text or ""
    return text if len(text) <= n else text[:n] + f"\n...[truncated {len(text) - n} chars]"


def env_with(extra):
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (extra or {}).items()})
    return env
